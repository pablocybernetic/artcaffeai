"""
master_agent.py
---------------
Master Agent — Claude Sonnet 4.6 orchestration intelligence layer.

Responsibilities:
  1. Scan     — full snapshot of all pipeline state across every concept
  2. Recover  — mark stuck jobs (running >15 min) as failed so they can be retried
  3. Analyse  — Claude Sonnet interprets the state and produces prioritised actions
  4. Report   — structured JSON returned to caller / stored in DB

Pipeline stages monitored:
  market_research → [human reviews] → ideation → [human approves] → production → publishing

The Master Agent does NOT trigger agents directly.  It surfaces what needs to happen
and flags quality issues — humans and the job_runner execute from there.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from supabase import Client

MODEL = "claude-sonnet-4-6"
STUCK_THRESHOLD_MINUTES = 15   # jobs running longer than this are considered stuck

MASTER_SYSTEM = """\
You are the Master Agent for the Artcaffe AI Marketing System — a premium café brand in Nairobi, Kenya
with three product concepts: Artcaffe Market, Artcaffe Kenya (Restaurant), and Artcaffe Gastro Bar.

You receive a JSON pipeline snapshot and must do three things:

1. HEALTH — assess overall system state as "ok", "warning", or "critical":
   - "ok":       pipeline flowing normally, no blockers
   - "warning":  some items stalled or need human attention, but not urgent
   - "critical": failed jobs, empty pipeline for 7+ days, or publishing errors

2. SUMMARY — write 2-3 sentences describing the current state and the single most important thing to do next.

3. ACTIONS — list up to 8 prioritised actions, each with:
   - priority: "high" | "medium" | "low"
   - type: one of:
       "retry_job"        — a failed job should be retried
       "human_review"     — content / brief / research is waiting for a human
       "quality_flag"     — output looks weak or incomplete (low word count, generic copy, etc.)
       "pipeline_advance" — a stage is complete and the next one hasn't been queued
       "configure"        — a missing credential or setting is blocking work
   - concept: concept name (or "all")
   - action: 1 sentence description of what to do
   - entity_id: the relevant job_id, brief_id, or content_item_id (or null)

Output ONLY valid JSON — no markdown, no prose:
{
  "health": "ok" | "warning" | "critical",
  "summary": "...",
  "actions": [
    {
      "priority": "high",
      "type": "retry_job",
      "concept": "Artcaffe Market",
      "action": "Retry the failed ideation job — it timed out due to a network error",
      "entity_id": "uuid-or-null"
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        if "+" in s:
            s = s.split("+")[0]
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _minutes_ago(dt: Optional[datetime]) -> Optional[float]:
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


# ---------------------------------------------------------------------------
# Step 1: Scan pipeline state
# ---------------------------------------------------------------------------

def _scan_pipeline(sb: Client) -> dict:
    """
    Build a lightweight snapshot of the full pipeline state.
    Returns a dict suitable for sending to Claude.
    """
    now = _now()

    # --- Concepts ---
    concepts_res = sb.table("concepts").select("id,name,key").execute()
    concepts = {c["id"]: c["name"] for c in (concepts_res.data or [])}

    # --- Jobs (last 7 days, all non-succeeded/non-skipped) ---
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    jobs_res = (
        sb.table("jobs")
        .select("id,concept_id,agent_type,status,error_message,created_at,started_at,updated_at")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    jobs = jobs_res.data or []

    # --- Content briefs (pending, in_review stages) ---
    briefs_res = (
        sb.table("content_briefs")
        .select("id,concept_id,stage,approval_status,content_status,updated_at")
        .not_.in_("stage", ["published", "archived"])
        .order("updated_at", desc=True)
        .limit(50)
        .execute()
    )
    briefs = briefs_res.data or []

    # --- Content items awaiting production or review ---
    items_res = (
        sb.table("content_items")
        .select("id,brief_id,status,platform,updated_at")
        .in_("status", ["draft", "pending_review", "in_production"])
        .order("updated_at", desc=True)
        .limit(50)
        .execute()
    )
    items = items_res.data or []

    # --- Platform credentials status ---
    creds_res = (
        sb.table("platform_credentials")
        .select("platform,is_active,account_name")
        .eq("is_active", True)
        .execute()
    )
    cred_platforms = [c["platform"] for c in (creds_res.data or [])]

    # --- Summarise jobs by concept + type + status ---
    job_summary: list[dict] = []
    for j in jobs:
        concept_name = concepts.get(j.get("concept_id", ""), "Unknown")
        entry: dict[str, Any] = {
            "id": j["id"],
            "concept": concept_name,
            "type": j["agent_type"],
            "status": j["status"],
            "age_minutes": _minutes_ago(_parse_dt(j.get("created_at"))),
        }
        if j["status"] == "failed" and j.get("error_message"):
            entry["error"] = j["error_message"][:120]
        if j["status"] == "running":
            entry["running_minutes"] = _minutes_ago(_parse_dt(j.get("started_at") or j.get("created_at")))
        job_summary.append(entry)

    # --- Summarise briefs by concept + stage ---
    brief_summary: list[dict] = []
    for b in briefs:
        concept_name = concepts.get(b.get("concept_id", ""), "Unknown")
        brief_summary.append({
            "id": b["id"],
            "concept": concept_name,
            "stage": b.get("stage"),
            "approval_status": b.get("approval_status"),
            "age_minutes": _minutes_ago(_parse_dt(b.get("updated_at"))),
        })

    # --- Summarise content items ---
    item_summary: list[dict] = []
    for it in items:
        item_summary.append({
            "id": it["id"],
            "status": it.get("status"),
            "platform": it.get("platform"),
            "age_minutes": _minutes_ago(_parse_dt(it.get("updated_at"))),
        })

    return {
        "snapshot_at": now,
        "concepts": list(concepts.values()),
        "connected_platforms": cred_platforms,
        "jobs": job_summary,
        "briefs": brief_summary,
        "content_items_in_progress": item_summary,
        "counts": {
            "total_jobs_7d": len(jobs),
            "failed_jobs": sum(1 for j in jobs if j["status"] == "failed"),
            "running_jobs": sum(1 for j in jobs if j["status"] == "running"),
            "pending_jobs": sum(1 for j in jobs if j["status"] == "pending"),
            "briefs_awaiting_review": sum(1 for b in briefs if b.get("approval_status") in ("pending", "pending_review")),
            "items_in_production": sum(1 for it in items if it.get("status") == "in_production"),
        },
    }


# ---------------------------------------------------------------------------
# Step 2: Recover stuck jobs
# ---------------------------------------------------------------------------

def _recover_stuck_jobs(sb: Client) -> list[str]:
    """
    Mark jobs that have been running for > STUCK_THRESHOLD_MINUTES as failed.
    Returns list of recovered job IDs.
    """
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_THRESHOLD_MINUTES)).isoformat()
    stuck_res = (
        sb.table("jobs")
        .select("id,agent_type,concept_id,started_at")
        .eq("status", "running")
        .lt("started_at", threshold)
        .execute()
    )
    stuck = stuck_res.data or []
    recovered = []
    for job in stuck:
        minutes = _minutes_ago(_parse_dt(job.get("started_at"))) or 0
        sb.table("jobs").update({
            "status": "failed",
            "error_message": f"[master_agent] Job stuck in running state for {minutes:.0f} min — marked failed for retry",
            "finished_at": _now(),
            "updated_at": _now(),
        }).eq("id", job["id"]).execute()
        recovered.append(job["id"])
        print(f"[master_agent] recovered stuck job {job['id']} ({job['agent_type']}, {minutes:.0f}min)", flush=True)
    return recovered


# ---------------------------------------------------------------------------
# Step 3: Claude analysis
# ---------------------------------------------------------------------------

def _claude_analysis(snapshot: dict, anthropic: Any) -> dict:
    """
    Send the pipeline snapshot to Claude Sonnet and get structured recommendations.
    Falls back to a rule-based summary if Claude is unavailable.
    """
    try:
        user_msg = f"Pipeline snapshot:\n{json.dumps(snapshot, indent=2, default=str)}"
        resp = anthropic.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=MASTER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            timeout=45.0,
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"[master_agent] claude_analysis failed: {e}", flush=True)
        # Rule-based fallback
        counts = snapshot.get("counts", {})
        failed = counts.get("failed_jobs", 0)
        pending = counts.get("pending_jobs", 0)
        review = counts.get("briefs_awaiting_review", 0)
        health = "critical" if failed > 2 else "warning" if failed > 0 or review > 5 else "ok"
        return {
            "health": health,
            "summary": (
                f"{failed} failed job(s), {pending} pending job(s), "
                f"{review} brief(s) awaiting human review."
                " Claude analysis unavailable — rule-based summary used."
            ),
            "actions": [],
        }


# ---------------------------------------------------------------------------
# Step 4: Store result in DB
# ---------------------------------------------------------------------------

def _store_result(sb: Client, result: dict) -> str:
    """Upsert the master agent result into a dedicated row (concept_id=NULL, agent_type='master')."""
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    sb.table("jobs").insert({
        "id": job_id,
        "concept_id": None,
        "agent_type": "master",
        "status": "succeeded",
        "input_payload": {"triggered_by": "api"},
        "result": result,
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": _now(),
        "finished_at": _now(),
    }).execute()
    return job_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_master_agent(sb: Client, anthropic: Any) -> dict:
    """
    Run the full Master Agent cycle:
      1. Recover stuck jobs
      2. Scan pipeline state
      3. Get Claude analysis
      4. Store result
      5. Return report

    Returns a dict with keys: health, summary, actions, recovered_jobs, snapshot, job_id
    """
    print("[master_agent] starting cycle", flush=True)

    # 1. Recover stuck jobs first so they show up correctly in the scan
    recovered = []
    try:
        recovered = _recover_stuck_jobs(sb)
    except Exception as e:
        print(f"[master_agent] stuck-job recovery error: {e}", flush=True)

    # 2. Scan
    snapshot = {}
    try:
        snapshot = _scan_pipeline(sb)
        print(
            f"[master_agent] scan complete — {snapshot['counts']['total_jobs_7d']} jobs, "
            f"{snapshot['counts']['failed_jobs']} failed, "
            f"{snapshot['counts']['briefs_awaiting_review']} briefs awaiting review",
            flush=True,
        )
    except Exception as e:
        print(f"[master_agent] scan error: {e}\n{traceback.format_exc()}", flush=True)
        snapshot = {"error": str(e)}

    # 3. Claude analysis
    analysis = {}
    try:
        analysis = _claude_analysis(snapshot, anthropic)
        print(f"[master_agent] analysis health={analysis.get('health','?')}, "
              f"{len(analysis.get('actions', []))} actions", flush=True)
    except Exception as e:
        print(f"[master_agent] analysis error: {e}", flush=True)
        analysis = {"health": "warning", "summary": f"Analysis error: {e}", "actions": []}

    # 4. Build final report
    report: dict[str, Any] = {
        **analysis,
        "recovered_jobs": recovered,
        "snapshot": {
            k: v for k, v in snapshot.items()
            if k in ("snapshot_at", "counts", "concepts", "connected_platforms")
        },
    }

    # 5. Store (non-fatal)
    job_id = None
    try:
        job_id = _store_result(sb, report)
        report["job_id"] = job_id
    except Exception as e:
        print(f"[master_agent] store error (non-fatal): {e}", flush=True)

    print(f"[master_agent] done — health={report.get('health')}, job_id={job_id}", flush=True)
    return report
