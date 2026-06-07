"""
backfill_weekly_snapshots.py
-----------------------------
One-off script: pulls 52 weeks of weekly summary data from BigQuery
and upserts into platform_data_snapshots in Supabase.

Run on the VM:
  cd /opt/artcaffe
  source .env  (or it reads /opt/artcaffe/.env automatically)
  /opt/artcaffe/venv/bin/python /opt/artcaffe/scripts/backfill_weekly_snapshots.py

Env vars required (same as the API):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  BQ_SERVICE_ACCOUNT_JSON
  BQ_PROJECT_ID         (default: my-first-project-416407)
  BQ_GA4_DATASET        (default: analytics_293507702)
  BQ_GADS_DATASET       (default: artcaffe_artcaffemarket_co_ke)
  CONCEPT_ID            (optional — if set, only backfills this concept)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Load .env from parent dir if env vars not already set
# ---------------------------------------------------------------------------
def _load_env():
    for candidate in [
        Path(__file__).parent.parent / ".env",
        Path("/opt/artcaffe/.env"),
    ]:
        if not candidate.exists():
            continue
        with candidate.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        break

_load_env()

SUPABASE_URL             = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BQ_PROJECT_ID            = os.environ.get("BQ_PROJECT_ID",    "my-first-project-416407")
BQ_GA4_DATASET           = os.environ.get("BQ_GA4_DATASET",   "analytics_293507702")
BQ_GADS_DATASET          = os.environ.get("BQ_GADS_DATASET",  "artcaffe_artcaffemarket_co_ke")
BQ_SERVICE_ACCOUNT_JSON  = os.environ.get("BQ_SERVICE_ACCOUNT_JSON")
CONCEPT_ID               = os.environ.get("CONCEPT_ID")

WEEKS_BACK = 52   # how many weeks of history to backfill
SLEEP_BETWEEN_WEEKS = 0.5  # seconds — avoids hammering BQ quota


# ---------------------------------------------------------------------------
# BigQuery client
# ---------------------------------------------------------------------------
def _bq_client():
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if BQ_SERVICE_ACCOUNT_JSON:
        info = json.loads(BQ_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info)
        return bigquery.Client(project=BQ_PROJECT_ID, credentials=creds)
    return bigquery.Client(project=BQ_PROJECT_ID)


# ---------------------------------------------------------------------------
# GA4 weekly pull
# ---------------------------------------------------------------------------
def _pull_ga4_week(client, start_dt: date, end_dt: date) -> dict:
    project = BQ_PROJECT_ID
    dataset = BQ_GA4_DATASET
    tbl = f"`{project}.{dataset}.events_*`"
    start_sfx = start_dt.strftime("%Y%m%d")
    end_sfx   = end_dt.strftime("%Y%m%d")

    daily_sql = f"""
    SELECT
      FORMAT_DATE('%Y-%m-%d', PARSE_DATE('%Y%m%d', event_date)) AS date,
      COUNTIF(event_name = 'session_start')  AS sessions,
      COUNT(DISTINCT user_pseudo_id)         AS active_users,
      COUNTIF(event_name = 'page_view')      AS pageviews
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
    GROUP BY date ORDER BY date
    """

    channel_sql = f"""
    SELECT
      IFNULL(traffic_source.medium, '(none)')   AS medium,
      IFNULL(traffic_source.source, '(direct)') AS source,
      COUNTIF(event_name = 'session_start')     AS sessions,
      COUNT(DISTINCT user_pseudo_id)            AS users
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
    GROUP BY medium, source ORDER BY sessions DESC LIMIT 10
    """

    pages_sql = f"""
    SELECT
      (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location' LIMIT 1) AS page_url,
      (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_title'    LIMIT 1) AS page_title,
      COUNT(*)                       AS pageviews,
      COUNT(DISTINCT user_pseudo_id) AS unique_users
    FROM {tbl}
    WHERE _TABLE_SUFFIX BETWEEN '{start_sfx}' AND '{end_sfx}'
      AND event_name = 'page_view'
    GROUP BY page_url, page_title ORDER BY pageviews DESC LIMIT 20
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
# Paid ads + transactions weekly pull
# ---------------------------------------------------------------------------
def _pull_ads_week(client, start_dt: date, end_dt: date) -> dict:
    project = BQ_PROJECT_ID
    dataset = BQ_GADS_DATASET
    start_s = start_dt.isoformat()
    end_s   = end_dt.isoformat()

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
    GROUP BY channel ORDER BY total_spend DESC
    """

    ads_daily_sql = f"""
    SELECT
      CAST(event_date AS STRING)   AS date,
      channel,
      campaign_name,
      CAST(spend AS FLOAT64)       AS spend,
      CAST(impressions AS FLOAT64) AS impressions,
      CAST(clicks AS FLOAT64)      AS clicks,
      CAST(ctr AS FLOAT64)         AS ctr
    FROM `{project}.{dataset}.table__blend__cost__session__master`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
    ORDER BY event_date DESC, spend DESC LIMIT 200
    """

    txn_totals_sql = f"""
    SELECT
      COUNT(DISTINCT transaction_id)             AS total_transactions,
      ROUND(SUM(ecommerce.purchase_revenue), 2)  AS total_revenue,
      IFNULL(SUM(ecommerce.total_item_quantity), 0) AS total_items_sold,
      COUNT(DISTINCT user_pseudo_id)             AS total_customers
    FROM `{project}.{dataset}.table__transaction`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
      AND is_synthetics = FALSE
    """

    txn_by_source_sql = f"""
    SELECT
      IFNULL(traffic_source.medium, '(none)')   AS medium,
      IFNULL(traffic_source.source, '(direct)') AS source,
      COUNT(DISTINCT transaction_id)            AS transactions,
      ROUND(SUM(ecommerce.purchase_revenue), 2) AS revenue
    FROM `{project}.{dataset}.table__transaction`
    WHERE event_date BETWEEN DATE('{start_s}') AND DATE('{end_s}')
      AND is_synthetics = FALSE
    GROUP BY medium, source ORDER BY revenue DESC LIMIT 10
    """

    ads_channels = [dict(r) for r in client.query(ads_channel_sql).result()]
    ads_daily    = [dict(r) for r in client.query(ads_daily_sql).result()]
    txn_r        = list(client.query(txn_totals_sql).result())
    txn_totals   = dict(txn_r[0]) if txn_r else {}
    txn_by_src   = [dict(r) for r in client.query(txn_by_source_sql).result()]

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
            "by_source": txn_by_src,
        },
    }


# ---------------------------------------------------------------------------
# Supabase upsert
# ---------------------------------------------------------------------------
def _upsert(sb, concept_id: str, platform: str, week_start: date, summary: dict) -> None:
    sb.table("platform_data_snapshots").upsert(
        {
            "concept_id":    concept_id,
            "platform":      platform,
            "snapshot_date": week_start.isoformat(),
            "summary_json":  summary,
        },
        on_conflict="concept_id,platform,snapshot_date",
    ).execute()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    from supabase import create_client

    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    # Resolve concept IDs
    if CONCEPT_ID:
        concept_ids = [CONCEPT_ID]
        print(f"[backfill] targeting concept_id={CONCEPT_ID}")
    else:
        rows = sb.table("concepts").select("id,key").execute().data or []
        if not rows:
            print("[backfill] ERROR: no concepts found in Supabase", file=sys.stderr)
            sys.exit(1)
        concept_ids = [r["id"] for r in rows]
        print(f"[backfill] found {len(concept_ids)} concept(s): {[r['key'] for r in rows]}")

    # Build list of week start dates (Monday), going back WEEKS_BACK weeks
    today = date.today()
    # Start from the most recent completed Monday
    most_recent_monday = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(1, WEEKS_BACK + 1):
        week_start = most_recent_monday - timedelta(weeks=i)
        week_end   = week_start + timedelta(days=6)
        # Don't go into the future
        if week_end >= today:
            week_end = today - timedelta(days=1)
        weeks.append((week_start, week_end))

    print(f"[backfill] will backfill {len(weeks)} weeks: {weeks[-1][0]} → {weeks[0][0]}")

    if not BQ_SERVICE_ACCOUNT_JSON:
        print("[backfill] ERROR: BQ_SERVICE_ACCOUNT_JSON not set", file=sys.stderr)
        sys.exit(1)

    bq = _bq_client()
    total_ok = 0
    total_err = 0

    for concept_id in concept_ids:
        print(f"\n[backfill] === concept {concept_id} ===")
        for week_start, week_end in weeks:
            label = f"{week_start} → {week_end}"

            # GA4
            try:
                data = _pull_ga4_week(bq, week_start, week_end)
                _upsert(sb, concept_id, "ga4", week_start, data)
                sessions = data["totals"]["sessions"]
                print(f"  [ga4]  {label}  sessions={sessions}  ✓")
                total_ok += 1
            except Exception as e:
                print(f"  [ga4]  {label}  ERROR: {e}")
                total_err += 1

            # Paid ads + transactions
            try:
                data = _pull_ads_week(bq, week_start, week_end)
                _upsert(sb, concept_id, "paid_ads", week_start, data)
                spend = float(data["paid_ads"]["totals"].get("spend") or 0)
                rev   = float(data["transactions"]["totals"].get("total_revenue") or 0)
                print(f"  [ads]  {label}  spend=KES{spend:,.0f}  revenue=KES{rev:,.0f}  ✓")
                total_ok += 1
            except Exception as e:
                print(f"  [ads]  {label}  ERROR: {e}")
                total_err += 1

            time.sleep(SLEEP_BETWEEN_WEEKS)

    print(f"\n[backfill] done — {total_ok} upserted, {total_err} errors")


if __name__ == "__main__":
    main()
