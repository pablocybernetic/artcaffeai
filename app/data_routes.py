"""
Data Agent routes for Artcaffe FastAPI service.

Endpoints:
    POST /data/snapshot      -> pull last 7d metrics from BigQuery (GA4 + Google Ads),
                                upsert into public.platform_data_snapshots
    POST /data/chat          -> answer questions about snapshots using Claude Haiku 4.5

Env vars required:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (already set)
    FASTAPI_API_KEY                            (already set)
    ANTHROPIC_API_KEY                          (already set)
    BQ_SERVICE_ACCOUNT_JSON                    (full service account JSON string)
    BQ_PROJECT_ID                              (default: my-first-project-416407)
    BQ_GA4_DATASET                             (default: analytics_293507702)
    BQ_GADS_DATASET                            (default: artcaffe_artcaffemarket_co_ke)
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

from ai_error_log import record_ai_error, clear_ai_error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_KEY = os.environ.get("FASTAPI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

BQ_PROJECT_ID = os.environ.get("BQ_PROJECT_ID", "my-first-project-416407")
BQ_GA4_DATASET = os.environ.get("BQ_GA4_DATASET", "analytics_293507702")
BQ_GADS_DATASET = os.environ.get("BQ_GADS_DATASET", "artcaffe_artcaffemarket_co_ke")
BQ_SERVICE_ACCOUNT_JSON = os.environ.get("BQ_SERVICE_ACCOUNT_JSON")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/data", dependencies=[Depends(require_api_key)])

DATA_AGENT_MODEL = "claude-sonnet-4-6"
DATA_AGENT_SYSTEM_PROMPT = (
    "You are the Artcaffe Data Agent. Answer questions about platform performance "
    "using ONLY the snapshot data provided below. Be concise and direct. "
    "If the data doesn't contain the answer, say so plainly.\n\n"
    "CURRENCY RULE — all monetary values in the data are in Kenyan Shillings. "
    "Always display currency amounts with the 'KES' prefix (e.g. KES 70,535). "
    "Never use '$', 'USD', or any other currency symbol.\n\n"
    "CHART RULES — follow exactly:\n"
    "- When a chart would help, place it on its own line starting with CHART: followed by a single-line JSON object.\n"
    "- The JSON MUST be on ONE line — no newlines inside it.\n"
    "- Required format: CHART: {\"type\":\"bar\",\"title\":\"My Title\",\"data\":[{\"name\":\"Label\",\"value\":123}]}\n"
    "- Each data item is a flat object with a \"name\" string key and one or more numeric value keys.\n"
    "- Multi-series example: CHART: {\"type\":\"bar\",\"title\":\"Sessions vs Users\",\"data\":[{\"name\":\"Mon\",\"sessions\":120,\"users\":80}]}\n"
    "- NEVER use Chart.js format (labels/datasets arrays). ONLY the flat array format above.\n"
    "- Output any CHART: line AFTER your text answer, not before.\n\n"
    "[SNAPSHOTS injected at runtime]"
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SnapshotRequest(BaseModel):
    concept_id: Optional[str] = None


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    concept_id: Optional[str] = None
    messages: list[ChatMessage]


# ---------------------------------------------------------------------------
# BigQuery client
# ---------------------------------------------------------------------------
def _bq_client():
    from google.cloud import bigquery  # type: ignore
    from google.oauth2 import service_account  # type: ignore

    if BQ_SERVICE_ACCOUNT_JSON:
        info = json.loads(BQ_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info)
        return bigquery.Client(project=BQ_PROJECT_ID, credentials=creds)
    # Fall back to Application Default Credentials (e.g. Cloud Run service account)
    return bigquery.Client(project=BQ_PROJECT_ID)


# ---------------------------------------------------------------------------
# BigQuery — GA4 pull
# ---------------------------------------------------------------------------
def _pull_ga4(project: str, dataset: str) -> dict[str, Any]:
    client = _bq_client()

    end_dt = date.today() - timedelta(days=1)   # yesterday (today is incomplete)
    start_dt = end_dt - timedelta(days=6)        # 7-day window
    start_sfx = start_dt.strftime("%Y%m%d")
    end_sfx = end_dt.strftime("%Y%m%d")
    tbl = f"`{project}.{dataset}.events_*`"

    # --- daily sessions / users / pageviews ---
    daily_sql = f"""
    SELECT
      FORMAT_DATE('%Y-%m-%d', PARSE_DATE('%Y%m%d', event_date)) AS date,
      COUNTIF(event_name = 'session_start')  AS sessions,
      COUNT(DISTINCT user_pseudo_id)         AS active_users,
      COUNTIF(event_name = 'page_view')      AS pageviews
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
    GROUP BY date
    ORDER BY date
    """

    # --- traffic channel breakdown ---
    channel_sql = f"""
    SELECT
      IFNULL(traffic_source.medium, '(none)')    AS medium,
      IFNULL(traffic_source.source, '(direct)')  AS source,
      COUNTIF(event_name = 'session_start')      AS sessions,
      COUNT(DISTINCT user_pseudo_id)             AS users
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
    GROUP BY medium, source
    ORDER BY sessions DESC
    LIMIT 10
    """

    # --- top pages ---
    pages_sql = f"""
    SELECT
      (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location' LIMIT 1) AS page_url,
      (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_title'    LIMIT 1) AS page_title,
      COUNT(*)                       AS pageviews,
      COUNT(DISTINCT user_pseudo_id) AS unique_users
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
      AND event_name = 'page_view'
    GROUP BY page_url, page_title
    ORDER BY pageviews DESC
    LIMIT 20
    """

    daily    = [dict(r) for r in client.query(daily_sql).result()]
    channels = [dict(r) for r in client.query(channel_sql).result()]
    pages    = [dict(r) for r in client.query(pages_sql).result()]

    totals = {
        "sessions":    sum(r["sessions"]    for r in daily),
        "active_users": sum(r["active_users"] for r in daily),
        "pageviews":   sum(r["pageviews"]   for r in daily),
    }

    return {
        "source":    "bigquery_ga4",
        "dataset":   dataset,
        "range":     {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "totals":    totals,
        "daily":     daily,
        "channels":  channels,
        "top_pages": pages,
    }


# ---------------------------------------------------------------------------
# BigQuery — Paid ads + transactions pull
# Covers: Google Ads + Facebook (blended via table__blend__cost__session__master)
#         E-commerce revenue via table__transaction
# ---------------------------------------------------------------------------
def _pull_ads_and_transactions(project: str, dataset: str) -> dict[str, Any]:
    """Pull last 7d paid performance and revenue from the artcaffe data mart."""
    client = _bq_client()

    end_dt   = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=6)
    start_s  = start_dt.isoformat()
    end_s    = end_dt.isoformat()

    # --- Blended paid ads: daily rows per channel / campaign ---
    ads_daily_sql = f"""
    SELECT
      CAST(event_date AS STRING)            AS date,
      channel,
      campaign_name,
      CAST(spend AS FLOAT64)                AS spend,
      CAST(impressions AS FLOAT64)          AS impressions,
      CAST(clicks AS FLOAT64)               AS clicks,
      CAST(budget AS FLOAT64)               AS budget,
      CAST(ctr AS FLOAT64)                  AS ctr,
      IFNULL(last_visit.sessions, 0)        AS sessions,
      IFNULL(last_visit.total_users, 0)     AS total_users
    FROM `{project}.{dataset}.table__blend__cost__session__master`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
    ORDER BY event_date DESC, spend DESC
    LIMIT 500
    """

    # --- Channel-level totals (Google vs Facebook vs other) ---
    ads_channel_sql = f"""
    SELECT
      channel,
      ROUND(SUM(CAST(spend AS FLOAT64)), 2)             AS total_spend,
      CAST(SUM(CAST(impressions AS FLOAT64)) AS INT64)  AS total_impressions,
      CAST(SUM(CAST(clicks AS FLOAT64)) AS INT64)       AS total_clicks,
      SUM(IFNULL(last_visit.sessions, 0))               AS total_sessions,
      SUM(IFNULL(last_visit.total_users, 0))            AS total_users
    FROM `{project}.{dataset}.table__blend__cost__session__master`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
    GROUP BY channel
    ORDER BY total_spend DESC
    """

    # --- Transactions: daily revenue by attribution channel ---
    txn_daily_sql = f"""
    SELECT
      CAST(event_date AS STRING)                          AS date,
      IFNULL(traffic_source.medium, '(none)')             AS medium,
      IFNULL(traffic_source.source, '(direct)')           AS source,
      COUNT(DISTINCT transaction_id)                      AS transactions,
      ROUND(SUM(ecommerce.purchase_revenue), 2)           AS revenue,
      IFNULL(SUM(ecommerce.total_item_quantity), 0)       AS items_sold,
      COUNT(DISTINCT user_pseudo_id)                      AS customers
    FROM `{project}.{dataset}.table__transaction`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
      AND is_synthetics = FALSE
    GROUP BY date, medium, source
    ORDER BY date DESC, revenue DESC
    LIMIT 200
    """

    # --- Revenue totals for the period ---
    txn_totals_sql = f"""
    SELECT
      COUNT(DISTINCT transaction_id)                  AS total_transactions,
      ROUND(SUM(ecommerce.purchase_revenue), 2)       AS total_revenue,
      IFNULL(SUM(ecommerce.total_item_quantity), 0)   AS total_items_sold,
      COUNT(DISTINCT user_pseudo_id)                  AS total_customers
    FROM `{project}.{dataset}.table__transaction`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
      AND is_synthetics = FALSE
    """

    ads_daily    = [dict(r) for r in client.query(ads_daily_sql).result()]
    ads_channels = [dict(r) for r in client.query(ads_channel_sql).result()]
    txn_daily    = [dict(r) for r in client.query(txn_daily_sql).result()]
    txn_totals_r = list(client.query(txn_totals_sql).result())
    txn_totals   = dict(txn_totals_r[0]) if txn_totals_r else {}

    total_spend       = sum(float(r.get("spend") or 0) for r in ads_daily)
    total_impressions = sum(float(r.get("impressions") or 0) for r in ads_daily)
    total_clicks      = sum(float(r.get("clicks") or 0) for r in ads_daily)

    return {
        "source":  "bigquery_artcaffe",
        "dataset": dataset,
        "range":   {"start": start_s, "end": end_s},
        "paid_ads": {
            "totals": {
                "spend":       round(total_spend, 2),
                "impressions": int(total_impressions),
                "clicks":      int(total_clicks),
                "ctr":         round(total_clicks / total_impressions, 4) if total_impressions else 0,
            },
            "by_channel": ads_channels,
            "daily":      ads_daily,
        },
        "transactions": {
            "totals":    txn_totals,
            "by_source": txn_daily,
        },
    }


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------
def _upsert_snapshot(concept_id: str, platform: str, summary: dict) -> dict:
    today = date.today().isoformat()
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id":    concept_id,
            "platform":      platform,
            "snapshot_date": today,
            "summary_json":  summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()
    return {"concept_id": concept_id, "platform": platform, "snapshot_date": today}


def _snapshot_concept(concept_id: str) -> dict:
    results: dict[str, Any] = {"concept_id": concept_id}

    # GA4
    try:
        ga4_data = _pull_ga4(BQ_PROJECT_ID, BQ_GA4_DATASET)
        results["ga4"] = _upsert_snapshot(concept_id, "ga4", ga4_data)
    except Exception as e:  # noqa: BLE001
        results["ga4_error"] = str(e)

    # Paid ads + transactions
    try:
        ads_data = _pull_ads_and_transactions(BQ_PROJECT_ID, BQ_GADS_DATASET)
        results["paid_ads"] = _upsert_snapshot(concept_id, "paid_ads", ads_data)
    except Exception as e:  # noqa: BLE001
        results["paid_ads_error"] = str(e)

    return results


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/snapshot")
def snapshot(req: SnapshotRequest):
    """Pull BigQuery data and upsert snapshots for one or all concepts."""
    if not BQ_SERVICE_ACCOUNT_JSON:
        raise HTTPException(500, "BQ_SERVICE_ACCOUNT_JSON not configured")

    if req.concept_id:
        return {"ok": True, "result": _snapshot_concept(req.concept_id)}

    concepts = sb.table("concepts").select("id,key").execute().data or []
    if not concepts:
        raise HTTPException(404, "No concepts found in database")

    results = []
    for c in concepts:
        try:
            results.append(_snapshot_concept(c["id"]))
        except Exception as e:  # noqa: BLE001
            results.append({"concept_id": c["id"], "error": str(e)})

    return {"ok": True, "snapshots": results}


# Keep old route as alias so the existing frontend trigger still works
@router.post("/ga4/snapshot")
def ga4_snapshot_alias(req: SnapshotRequest):
    return snapshot(req)


def _slim(snapshot: dict) -> dict:
    """Strip bulky daily-row arrays from summary_json so 52 weeks fit in the prompt.

    Paid-ads snapshots store up to 200 daily campaign rows per week (~30 KB each).
    We keep totals + channel breakdown — enough for trend analysis.
    """
    s = dict(snapshot)
    summary = s.get("summary_json") or {}
    platform = s.get("platform", "")

    if platform == "ga4":
        # Keep totals, channels (top 5), top_pages (top 5) — drop 'daily'
        s["summary_json"] = {
            "source":    summary.get("source"),
            "range":     summary.get("range"),
            "totals":    summary.get("totals"),
            "channels":  (summary.get("channels") or [])[:5],
            "top_pages": (summary.get("top_pages") or [])[:5],
        }
    elif "paid" in platform:
        paid = summary.get("paid_ads") or {}
        txn  = summary.get("transactions") or {}
        s["summary_json"] = {
            "source":  summary.get("source"),
            "range":   summary.get("range"),
            "paid_ads": {
                "totals":     paid.get("totals"),
                "by_channel": (paid.get("by_channel") or [])[:5],
                # drop paid.daily — it's the expensive part
            },
            "transactions": {
                "totals":    txn.get("totals"),
                "by_source": (txn.get("by_source") or [])[:5],
            },
        }

    return s


@router.post("/chat")
def chat(req: ChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    cutoff = (date.today() - timedelta(days=365)).isoformat()
    q = (
        sb.table("platform_data_snapshots")
        .select("platform,snapshot_date,summary_json")
        .gte("snapshot_date", cutoff)
        .order("snapshot_date", desc=True)
        .limit(120)
    )
    if req.concept_id:
        q = q.eq("concept_id", req.concept_id)
    snapshots = q.execute().data or []

    slimmed = [_slim(s) for s in snapshots]
    context_blob = json.dumps(slimmed, default=str)[:200_000]
    system = DATA_AGENT_SYSTEM_PROMPT.replace("[SNAPSHOTS injected at runtime]", f"SNAPSHOTS:\n{context_blob}")

    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=25.0)
    try:
        resp = client.messages.create(
            model=DATA_AGENT_MODEL,
            max_tokens=1500,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
        )
    except anthropic.APIStatusError as e:
        msg = str(getattr(e, "message", "") or e)
        if "credit balance" in msg.lower():
            detail = "Anthropic API credit balance is too low — top up billing at console.anthropic.com to restore this feature."
        elif e.status_code == 429:
            detail = "Anthropic API rate limit reached — try again in a moment."
        elif e.status_code == 401:
            detail = "Anthropic API key is invalid or missing — check the backend's ANTHROPIC_API_KEY."
        else:
            detail = f"Claude API error ({e.status_code}): {msg[:300]}"
        record_ai_error(sb, "anthropic", detail)
        raise HTTPException(status_code=502, detail=detail)
    except anthropic.APIConnectionError:
        detail = "Could not reach the Anthropic API — network issue, try again shortly."
        record_ai_error(sb, "anthropic", detail)
        raise HTTPException(status_code=502, detail=detail)

    clear_ai_error(sb, "anthropic")
    text = "".join(getattr(b, "text", "") for b in resp.content)
    chart = None
    if "CHART:" in text:
        head, _, tail = text.partition("CHART:")
        try:
            decoder = json.JSONDecoder()
            chart, _ = decoder.raw_decode(tail.strip())
            text = head.strip()
        except Exception:  # noqa: BLE001
            pass

    return {"answer": text, "chart": chart}
