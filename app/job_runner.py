"""
Job runner — bridges the `jobs` table to brand_pipeline.run_pipeline.

Aligned schema (matches Lovable frontend):
  jobs(id, concept_id, agent_type, status, input_payload, result,
       error_message, created_at, updated_at, started_at, finished_at)

Statuses:  pending -> running -> succeeded | failed

Usage:
  - Inline (from FastAPI BackgroundTasks):  run_job(job_id)
  - Standalone poller (systemd worker):     python job_runner.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic
from supabase import Client, create_client

from brand_pipeline import run_pipeline, PipelineInput

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BRAND_BUCKET = os.environ.get("BRAND_BUCKET", "brand-guidelines")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
POLL_INTERVAL_SECONDS = float(os.environ.get("WORKER_POLL_INTERVAL", "5"))

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_running(job_id: str) -> None:
    sb.table("jobs").update({
        "status": "running",
        "started_at": _now(),
        "updated_at": _now(),
        "error_message": None,
    }).eq("id", job_id).execute()


def _mark_succeeded(job_id: str, result: dict) -> None:
    sb.table("jobs").update({
        "status": "succeeded",
        "result": result,
        "finished_at": _now(),
        "updated_at": _now(),
        "error_message": None,
    }).eq("id", job_id).execute()


def _mark_failed(job_id: str, err: str) -> None:
    sb.table("jobs").update({
        "status": "failed",
        "error_message": err[:4000],
        "finished_at": _now(),
        "updated_at": _now(),
    }).eq("id", job_id).execute()


def _fetch(job_id: str) -> Optional[dict]:
    res = (
        sb.table("jobs")
        .select("id,concept_id,agent_type,status,input_payload")
        .eq("id", job_id)
        .single()
        .execute()
    )
    return res.data


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def run_job(job_id: str) -> dict:
    """Process a single job by id. Safe to call from FastAPI BackgroundTasks."""
    job = _fetch(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}

    if job["status"] not in ("pending", "running"):
        return {"ok": True, "skipped": True, "status": job["status"]}

    agent_type = job.get("agent_type")
    payload = job.get("input_payload") or {}
    concept_id = job["concept_id"]

    # ------------------------------------------------------------------
    # Research
    # ------------------------------------------------------------------
    if agent_type == "research":
        try:
            _mark_running(job_id)

            result = run_pipeline(
                sb=sb,
                anthropic=anthropic_client,
                bucket=BRAND_BUCKET,
                model=ANTHROPIC_MODEL,
                inp=PipelineInput(
                    concept_id=concept_id,
                    file_path=payload["file_path"],
                    file_name=payload.get("file_name", "guidelines.pdf"),
                    mime_type=payload.get("mime_type", "application/pdf"),
                ),
            )

            result_dict = {
                "brand_context_id": result.brand_context_id,
                "version": result.version,
                "chars_extracted": result.chars_extracted,
            }
            _mark_succeeded(job_id, result_dict)
            return {"ok": True, "job_id": job_id, "result": result_dict}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
            try:
                _mark_failed(job_id, err)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "job_id": job_id, "error": err}

    # ------------------------------------------------------------------
    # Ideation
    # ------------------------------------------------------------------
    if agent_type == "ideation":
        from ideation_agent import run_ideation  # noqa: PLC0415
        from notification_service import notify_ideation_complete  # noqa: PLC0415

        try:
            _mark_running(job_id)

            saved = run_ideation(
                sb=sb,
                anthropic=anthropic_client,
                job_id=job_id,
                brief_id=payload["brief_id"],
                concept_id=concept_id,
                n=payload.get("n", 5),
            )
            result_dict = {
                "n_ideas": len(saved),
                "item_ids": [s["id"] for s in saved],
            }
            _mark_succeeded(job_id, result_dict)
            try:
                notify_ideation_complete(sb, payload["brief_id"], len(saved))
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "job_id": job_id, "result": result_dict}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
            try:
                _mark_failed(job_id, err)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "job_id": job_id, "error": err}

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------
    if agent_type == "production":
        from production_agent import run_production  # noqa: PLC0415
        from notification_service import notify_production_complete  # noqa: PLC0415

        try:
            _mark_running(job_id)

            saved = run_production(
                sb=sb,
                anthropic=anthropic_client,
                job_id=job_id,
                content_item_id=payload["content_item_id"],
                concept_id=concept_id,
            )
            result_dict = {"final_item_id": saved["id"]}
            _mark_succeeded(job_id, result_dict)
            try:
                notify_production_complete(sb, saved.get("brief_id", ""))
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "job_id": job_id, "result": result_dict}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
            try:
                _mark_failed(job_id, err)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "job_id": job_id, "error": err}

    # ------------------------------------------------------------------
    # Market Research
    # ------------------------------------------------------------------
    if agent_type == "market_research":
        from research_agent import run_research  # noqa: PLC0415
        from notification_service import send_notification  # noqa: PLC0415

        try:
            _mark_running(job_id)

            brief = run_research(
                sb=sb,
                anthropic=anthropic_client,
                job_id=job_id,
                concept_id=concept_id,
            )
            result_dict = {
                "research_brief_id": brief["id"],
                "n_opportunities": len(brief.get("opportunities", [])),
            }
            _mark_succeeded(job_id, result_dict)
            try:
                opportunities = brief.get("opportunities", [])
                summary = brief.get("summary", "")
                n = len(opportunities)

                opp_rows = "".join(
                    f"""<tr style="border-bottom:1px solid #eee">
                      <td style="padding:10px 12px;font-size:13px;color:#1a1a1a">
                        <strong>{o.get('opportunity','')}</strong><br>
                        <span style="color:#666;font-size:12px">{o.get('hook','')}</span>
                      </td>
                      <td style="padding:10px 12px;font-size:12px;color:#444;white-space:nowrap;vertical-align:top">
                        {o.get('platform','').replace('_',' ')}<br>
                        <span style="background:{'#fee2e2' if o.get('priority')=='high' else '#fef9c3' if o.get('priority')=='medium' else '#f0fdf4'};color:{'#991b1b' if o.get('priority')=='high' else '#854d0e' if o.get('priority')=='medium' else '#166534'};padding:1px 6px;border-radius:4px;font-size:11px">{o.get('priority','')}</span>
                      </td>
                    </tr>"""
                    for o in opportunities
                )

                html = f"""
                <p>Hi,</p>
                <p>The Research Agent has analysed your platform data and identified <strong>{n} content opportunities</strong>.</p>
                {'<p style="color:#555;font-size:13px;border-left:3px solid #d1d5db;padding-left:12px;margin:16px 0">' + summary + '</p>' if summary else ''}
                <table style="width:100%;border-collapse:collapse;margin:16px 0;font-family:sans-serif">
                  <thead>
                    <tr style="background:#f9fafb">
                      <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280">Opportunity</th>
                      <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280">Platform / Priority</th>
                    </tr>
                  </thead>
                  <tbody>{opp_rows}</tbody>
                </table>
                <p><a href="{os.environ.get('DASHBOARD_URL','https://marketing.artcaffe.co.ke')}/briefs" style="background:#1a1a1a;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none">Open Briefs → Create from opportunities</a></p>
                <p style="color:#999;font-size:12px">— Artcaffe AI</p>"""

                send_notification(
                    sb,
                    type="research_complete",
                    subject=f"Artcaffe AI — {n} content opportunities ready",
                    html=html,
                    payload={"research_brief_id": brief["id"], "concept_id": concept_id},
                )
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "job_id": job_id, "result": result_dict}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
            try:
                _mark_failed(job_id, err)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "job_id": job_id, "error": err}

    # ------------------------------------------------------------------
    # Scheduled Publish
    # ------------------------------------------------------------------
    if agent_type == "scheduled_publish":
        from publishing_routes import _execute_publish  # noqa: PLC0415
        from notification_service import send_notification  # noqa: PLC0415

        try:
            _mark_running(job_id)

            content_item_id = payload.get("content_item_id")
            platforms = payload.get("platforms") or []
            brief_id = payload.get("brief_id")

            if not content_item_id:
                raise RuntimeError("No content_item_id in job payload")
            if not platforms:
                raise RuntimeError("No platforms in job payload")

            result = _execute_publish(sb, content_item_id, platforms)

            # Mark brief as approved if not already
            if brief_id:
                brief_res = sb.table("content_briefs").select("approval_status").eq("id", brief_id).maybe_single().execute()
                if brief_res.data and brief_res.data.get("approval_status") != "approved":
                    sb.table("content_briefs").update({
                        "approval_status": "approved",
                        "approved_at": _now(),
                        "content_status": "approved",
                        "updated_at": _now(),
                    }).eq("id", brief_id).execute()

            result_dict = {
                "content_item_id": content_item_id,
                "platforms": platforms,
                "publish_results": result,
            }
            _mark_succeeded(job_id, result_dict)

            try:
                ok_platforms = [p for p, v in result.get("results", {}).items() if v.get("ok")]
                fail_platforms = [p for p, v in result.get("results", {}).items() if not v.get("ok")]
                status_line = f"Published to: {', '.join(ok_platforms)}" if ok_platforms else "No platforms published successfully"
                if fail_platforms:
                    status_line += f" | Failed: {', '.join(fail_platforms)}"
                send_notification(
                    sb,
                    type="publish_complete",
                    subject=f"Artcaffe AI — Content published ({', '.join(ok_platforms or platforms)})",
                    html=f"<p>Content item <code>{content_item_id}</code> has been published.</p><p>{status_line}</p>",
                    payload=result_dict,
                )
            except Exception:  # noqa: BLE001
                pass

            return {"ok": True, "job_id": job_id, "result": result_dict}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"[job_runner] FAILED {job_id}: {err}", file=sys.stderr, flush=True)
            try:
                _mark_failed(job_id, err)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "job_id": job_id, "error": err}

    # Unknown agent type — skip
    return {"ok": True, "skipped": True, "reason": f"unknown_agent_type:{agent_type}"}


# ---------------------------------------------------------------------------
# Standalone poller (systemd worker mode)
# ---------------------------------------------------------------------------
def _claim_next_pending() -> Optional[dict]:
    now_iso = _now()
    res = (
        sb.table("jobs")
        .select("id,agent_type,input_payload")
        .eq("status", "pending")
        .in_("agent_type", ["research", "ideation", "production", "market_research", "scheduled_publish"])
        .order("created_at", desc=False)
        .limit(20)
        .execute()
    )
    for row in (res.data or []):
        if row.get("agent_type") == "scheduled_publish":
            publish_at = (row.get("input_payload") or {}).get("publish_at")
            if publish_at and publish_at > now_iso:
                continue  # not yet due
        return {"id": row["id"]}
    return None


def main() -> None:
    print(f"[job_runner] poller started (interval={POLL_INTERVAL_SECONDS}s)",
          flush=True)
    while True:
        try:
            job = _claim_next_pending()
            if job:
                print(f"[job_runner] picking up {job['id']}", flush=True)
                run_job(job["id"])
            else:
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("[job_runner] stopping", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[job_runner] poll error: {e}", file=sys.stderr, flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # one-shot: `python job_runner.py <job_id>`
        print(run_job(sys.argv[1]))
    else:
        main()
