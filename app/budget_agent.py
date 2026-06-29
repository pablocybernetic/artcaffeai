"""
budget_agent.py
---------------
Analyses paid media budget pacing per concept and produces reallocation
recommendations using Claude Haiku.

Workflow:
  1. Sync spent_usd from latest BigQuery paid_ads snapshot (channel → platform mapping)
  2. Read current period budget_allocations for the concept(s)
  3. Compute pacing metrics (expected spend % vs actual spend %)
  4. Claude Haiku → structured recommendations JSON
  5. Write per-allocation alerts to budget_alerts
  6. Save full result to budget_recommendations
  7. Return structured result to caller

Thresholds:
  actual / expected < 0.75  → underspending
  actual / expected > 1.25  → overspending
  actual / expected > 1.50  → critical overspend
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from supabase import Client

MODEL = "claude-haiku-4-5-20251001"

# Map BigQuery channel names (case-insensitive) → budget_allocations.platform values
_CHANNEL_TO_PLATFORM: dict[str, str] = {
    "google":           "google_ads",
    "google ads":       "google_ads",
    "google_ads":       "google_ads",
    "google/cpc":       "google_ads",
    "google display":   "google_ads",
    "facebook":         "meta_ads",
    "facebook ads":     "meta_ads",
    "meta":             "meta_ads",
    "meta ads":         "meta_ads",
    "meta_ads":         "meta_ads",
    "instagram":        "instagram_organic",
    "instagram ads":    "meta_ads",
    "linkedin":         "linkedin_organic",
    "linkedin ads":     "linkedin_organic",
    "tiktok":           "tiktok",
}

_PLATFORM_LABEL: dict[str, str] = {
    "meta_ads":           "Meta Ads",
    "instagram_organic":  "Instagram",
    "google_ads":         "Google Ads",
    "linkedin_organic":   "LinkedIn",
    "tiktok":             "TikTok",
}

SYSTEM_PROMPT = """\
You are the Artcaffe Budget Agent. You analyse paid media budget pacing and produce
concrete reallocation recommendations for a premium café brand in Nairobi, Kenya.

You will receive budget allocation data per platform (allocated, spent, days elapsed)
and, where available, performance metrics (CTR, spend by channel from BigQuery).

Rules:
- Be specific: name exact dollar amounts to move, not vague percentages
- Always justify with data (e.g. "Meta CTR is 3.2× LinkedIn — concentrate spend there")
- If a platform is under-pacing AND has poor performance metrics, recommend pausing it
- If a platform is over-pacing but high-CTR, flag it but do not cut it aggressively
- Maximum 5 recommendations, ordered by dollar impact (largest first)
- If all platforms are on-track and performing well, say so — do not fabricate issues

Pacing reference:
  actual_spend_pct / expected_spend_pct < 0.75  → underspending
  actual_spend_pct / expected_spend_pct > 1.25  → overspending
  actual_spend_pct / expected_spend_pct > 1.50  → critical

Output ONLY a JSON object — no markdown, no extra prose:
{
  "pacing_status": "on-track" | "under" | "over" | "critical",
  "alert_level":   "none" | "info" | "warning" | "critical",
  "summary": "2-3 sentence plain-English summary of the budget situation",
  "recommendations": [
    {
      "platform":            "meta_ads",
      "action":              "increase" | "decrease" | "pause" | "maintain",
      "reason":              "one clear sentence",
      "suggested_change_usd": 150,
      "priority":            "high" | "medium" | "low"
    }
  ]
}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_fraction(period_start: str, period_end: str) -> float:
    """Return the fraction of the budget period that has elapsed (0.0–1.0)."""
    try:
        start = date.fromisoformat(period_start)
        end   = date.fromisoformat(period_end)
        today = date.today()
        total = max((end - start).days, 1)
        gone  = max(min((today - start).days, total), 0)
        return gone / total
    except Exception:
        return 0.5


def _load_allocations(sb: Client, concept_id: Optional[str]) -> list[dict]:
    q = sb.table("budget_allocations").select("*")
    if concept_id:
        q = q.eq("concept_id", concept_id)
    return q.execute().data or []


def _load_performance(sb: Client, concept_id: str) -> Optional[dict]:
    """Latest paid_ads BigQuery snapshot for a concept, if available."""
    res = (
        sb.table("platform_data_snapshots")
        .select("summary_json,snapshot_date")
        .eq("concept_id", concept_id)
        .eq("platform", "paid_ads")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0]["summary_json"] if res.data else None


def sync_spent_from_snapshot(sb: Client, concept_id: str, allocations: list[dict]) -> None:
    """
    Sync spent_usd in budget_allocations from the latest BigQuery paid_ads snapshot.
    Maps BigQuery channel names (e.g. 'Google', 'Facebook') to platform values.
    Mutates allocations in-place so subsequent pacing calc uses fresh values.
    """
    perf = _load_performance(sb, concept_id)
    if not perf:
        return

    by_channel = perf.get("paid_ads", {}).get("by_channel", [])
    if not by_channel:
        return

    # Aggregate spend per platform from BigQuery channels
    platform_spend: dict[str, float] = {}
    for ch in by_channel:
        raw = (ch.get("channel") or "").lower().strip()
        platform = _CHANNEL_TO_PLATFORM.get(raw)
        if platform:
            platform_spend[platform] = (
                platform_spend.get(platform, 0) + float(ch.get("total_spend") or 0)
            )

    if not platform_spend:
        return

    for a in allocations:
        platform = a.get("platform", "")
        if platform not in platform_spend:
            continue
        new_spent = round(platform_spend[platform], 2)
        try:
            sb.table("budget_allocations").update({
                "spent_usd": new_spent,
                "updated_at": _now(),
            }).eq("id", a["id"]).execute()
            a["spent_usd"] = new_spent  # keep in-memory in sync
            print(f"[budget_agent] synced {platform} spent_usd={new_spent}", flush=True)
        except Exception as e:
            print(f"[budget_agent] could not sync spent for {a.get('id')}: {e}", flush=True)


def _build_user_prompt(
    concept_id: str,
    allocations: list[dict],
    perf: Optional[dict],
    brand_name: str,
) -> str:
    parts: list[str] = [f"CONCEPT: {brand_name} ({concept_id})"]

    parts.append("\nBUDGET ALLOCATIONS (current period):")
    for a in allocations:
        elapsed  = _elapsed_fraction(a.get("period_start", ""), a.get("period_end", ""))
        alloc    = float(a.get("allocated_usd") or 0)
        spent    = float(a.get("spent_usd") or 0)
        expected = alloc * elapsed
        pacing_ratio = (spent / expected) if expected > 0 else 1.0
        plat_label   = _PLATFORM_LABEL.get(a.get("platform", ""), a.get("platform", "unknown"))

        parts.append(
            f"  {plat_label}:"
            f" allocated=${alloc:,.0f}"
            f" | spent=${spent:,.0f} ({int(spent/alloc*100) if alloc else 0}%)"
            f" | period {elapsed*100:.0f}% elapsed"
            f" | expected=${expected:,.0f}"
            f" | pacing_ratio={pacing_ratio:.2f}"
            f" | {a.get('period_start','')} → {a.get('period_end','')}"
        )

    if perf:
        paid   = perf.get("paid_ads", {})
        totals = paid.get("totals", {})
        by_ch  = paid.get("by_channel", [])
        if totals or by_ch:
            parts.append("\nPERFORMANCE DATA (last 7 days — BigQuery):")
            if totals:
                parts.append(
                    f"  Overall: spend=${float(totals.get('spend') or 0):,.2f}"
                    f"  impressions={int(totals.get('impressions') or 0):,}"
                    f"  clicks={int(totals.get('clicks') or 0):,}"
                    f"  CTR={float(totals.get('ctr') or 0):.2%}"
                )
            for ch in by_ch[:5]:
                imp = int(ch.get("total_impressions") or 0)
                clk = int(ch.get("total_clicks") or 0)
                ctr = clk / imp if imp > 0 else 0
                parts.append(
                    f"  {ch.get('channel','?')}:"
                    f" spend=${float(ch.get('total_spend') or 0):,.2f}"
                    f"  CTR={ctr:.2%}"
                    f"  impressions={imp:,}"
                    f"  clicks={clk:,}"
                )

    parts.append("\nGenerate budget recommendations for this concept.")
    return "\n".join(parts)


def _write_alerts(sb: Client, allocations: list[dict], concept_result: dict) -> None:
    """
    Write per-allocation alert rows for any platforms that are off-pace.
    Resolves previous unresolved alerts for the same allocation first.
    """
    for a in allocations:
        alloc   = float(a.get("allocated_usd") or 0)
        spent   = float(a.get("spent_usd") or 0)
        elapsed = _elapsed_fraction(a.get("period_start", ""), a.get("period_end", ""))
        expected = alloc * elapsed
        ratio    = (spent / expected) if expected > 0 else 1.0

        if ratio < 0.75:
            alert_type = "underspend"
            msg = (
                f"{_PLATFORM_LABEL.get(a['platform'], a['platform'])}: "
                f"spent ${spent:,.0f} of expected ${expected:,.0f} "
                f"({ratio*100:.0f}% of pace) — underspending."
            )
        elif ratio > 1.50:
            alert_type = "critical_overspend"
            msg = (
                f"{_PLATFORM_LABEL.get(a['platform'], a['platform'])}: "
                f"spent ${spent:,.0f}, expected ${expected:,.0f} "
                f"({ratio*100:.0f}% of pace) — critical overspend."
            )
        elif ratio > 1.25:
            alert_type = "overspend"
            msg = (
                f"{_PLATFORM_LABEL.get(a['platform'], a['platform'])}: "
                f"spent ${spent:,.0f}, expected ${expected:,.0f} "
                f"({ratio*100:.0f}% of pace) — overspending."
            )
        else:
            continue

        try:
            sb.table("budget_alerts").update({"is_resolved": True}).eq(
                "allocation_id", a["id"]
            ).eq("is_resolved", False).execute()
        except Exception:
            pass

        try:
            sb.table("budget_alerts").insert({
                "id":            str(uuid.uuid4()),
                "allocation_id": a["id"],
                "alert_type":    alert_type,
                "threshold_pct": float(ratio * 100),
                "message":       msg,
                "is_resolved":   False,
                "created_at":    _now(),
            }).execute()
        except Exception as exc:
            print(f"[budget_agent] could not write alert for {a.get('id')}: {exc}", flush=True)


def _save_recommendation(
    sb: Client,
    concept_id: str,
    result: dict,
    allocations: list[dict],
) -> dict:
    row = {
        "concept_id":      concept_id,
        "period_start":    allocations[0].get("period_start") if allocations else None,
        "period_end":      allocations[0].get("period_end") if allocations else None,
        "pacing_status":   result.get("pacing_status", "on-track"),
        "alert_level":     result.get("alert_level", "none"),
        "recommendations": result.get("recommendations", []),
        "summary":         result.get("summary", ""),
        "model":           MODEL,
        "created_at":      _now(),
    }
    try:
        res = sb.table("budget_recommendations").insert(row).execute()
        return res.data[0] if res.data else row
    except Exception as e:
        print(
            f"[budget_agent] could not save recommendation "
            f"(run migration 009_budget_agent.sql if table missing): {e}",
            flush=True,
        )
        return row


def analyze_concept(*, sb: Client, anthropic: Any, concept_id: str) -> dict:
    """Run budget analysis for a single concept. Returns the recommendation dict."""
    allocations = _load_allocations(sb, concept_id)
    if not allocations:
        return {
            "concept_id": concept_id,
            "ok": False,
            "error": f"No budget allocations found for concept {concept_id}",
        }

    # Sync spent_usd from BigQuery before analysing
    sync_spent_from_snapshot(sb, concept_id, allocations)

    perf = _load_performance(sb, concept_id)

    from brand_context import get_active  # noqa: PLC0415
    ctx = get_active(sb, concept_id)
    brand_ctx  = ctx.get("context_json") if ctx else {}
    brand_name = (
        (brand_ctx or {}).get("brand_name")
        or (brand_ctx or {}).get("concept_name")
        or concept_id[:8]
    )

    user_msg = _build_user_prompt(concept_id, allocations, perf, brand_name)

    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=900,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        timeout=30.0,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:raw.rfind("```")]
    result = json.loads(raw.strip())

    _write_alerts(sb, allocations, result)
    saved = _save_recommendation(sb, concept_id, result, allocations)

    print(
        f"[budget_agent] concept={concept_id[:8]} "
        f"pacing={result.get('pacing_status')} "
        f"alert={result.get('alert_level')} "
        f"recs={len(result.get('recommendations', []))}",
        flush=True,
    )

    return {
        "concept_id":      concept_id,
        "brand_name":      brand_name,
        "ok":              True,
        "pacing_status":   result.get("pacing_status"),
        "alert_level":     result.get("alert_level"),
        "summary":         result.get("summary"),
        "recommendations": result.get("recommendations", []),
        "id":              saved.get("id"),
    }


def run_budget_analysis(
    *,
    sb: Client,
    anthropic: Any,
    concept_id: Optional[str] = None,
) -> dict:
    """
    Analyze budgets. Pass concept_id for a single concept, or None for all.
    Returns {"results": [...], "analyzed": N}.
    """
    if concept_id:
        return {
            "results":  [analyze_concept(sb=sb, anthropic=anthropic, concept_id=concept_id)],
            "analyzed": 1,
        }

    all_allocs  = _load_allocations(sb, None)
    concept_ids = list({a["concept_id"] for a in all_allocs if a.get("concept_id")})

    results: list[dict] = []
    for cid in concept_ids:
        try:
            results.append(analyze_concept(sb=sb, anthropic=anthropic, concept_id=cid))
        except Exception as e:
            results.append({"concept_id": cid, "ok": False, "error": str(e)})

    return {"results": results, "analyzed": len(results)}
