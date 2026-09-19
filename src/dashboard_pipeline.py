"""Shared dashboard decisions and bounded, public-job-only candidate export.

This module never edits application history or sends a message. Decisions are
written by the authenticated dashboard; both the cron and bootstrap read them.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BLOCKED_DECISIONS = frozenset({"pending", "excluded", "applied"})
TERMINAL_TRACKER = frozenset({"applied", "responded", "interview", "offer", "rejected", "discarded", "skip", "hired"})


def canonical_job_url(value: Any) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower().rstrip(".")
        path = re.sub(r"/+$", "", parsed.path) or "/"
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if (host == "saramin.co.kr" or host.endswith(".saramin.co.kr")) and str(query.get("rec_idx") or "").isdigit():
            return "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=" + query["rec_idx"]
        portal_patterns = (
            ("linkedin.com", r"/jobs/view/(?:[^/]*-)?(\d+)", "www.linkedin.com", "/jobs/view/"),
            ("wanted.co.kr", r"/wd/(\d+)", "www.wanted.co.kr", "/wd/"),
            ("jobkorea.co.kr", r"/(?:Recruit/)?GI_Read/(\d+)", "www.jobkorea.co.kr", "/Recruit/GI_Read/"),
            ("rememberapp.co.kr", r"/job(?:-posting|/posting)/(\d+)", "career.rememberapp.co.kr", "/job/posting/"),
        )
        for suffix, pattern, canonical_host, prefix in portal_patterns:
            if host == suffix or host.endswith("." + suffix):
                match = re.search(pattern, path, re.IGNORECASE)
                if match:
                    return "https://" + canonical_host + prefix + match.group(1)
        params = [(key, val) for key, val in query.items() if not key.lower().startswith("utm_") and key.lower() not in {"trk", "trackingid", "refid", "ref", "source", "from", "gclid", "fbclid"}]
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(sorted(params)), ""))
    except (ValueError, TypeError):
        return None


def candidate_aliases(candidate: dict[str, Any]) -> list[str]:
    identity = candidate.get("_identity_decision") or {}
    values: list[Any] = [candidate.get("id"), candidate.get("posting_cluster_id"), candidate.get("listing_instance_id"), identity.get("posting_cluster_id"), identity.get("listing_instance_id"), identity.get("normalized_url"), candidate.get("url")]
    values.extend(candidate.get("aliases") or [])
    values.extend(canonical_job_url(value) for value in list(values) if str(value or "").startswith(("https://", "http://")))
    concrete_identity = bool(candidate.get("listing_instance_id") or identity.get("listing_instance_id") or canonical_job_url(candidate.get("url")) or any(str(value or "").startswith(("li_", "https://", "http://")) for value in values))
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip() and not (concrete_identity and str(value).startswith("pc_"))))


def candidate_id(candidate: dict[str, Any]) -> str:
    identity = candidate.get("_identity_decision") or {}
    concrete_url = canonical_job_url(candidate.get("url"))
    return str(candidate.get("listing_instance_id") or identity.get("listing_instance_id") or ("url_" + sha256(concrete_url.encode()).hexdigest()[:20] if concrete_url else None) or candidate.get("posting_cluster_id") or identity.get("posting_cluster_id") or candidate.get("id") or "unidentified")


def decision_path(runtime: dict[str, Any]) -> Path:
    return Path(runtime.get("dashboard_decisions_path") or Path(runtime["production_project_root"]) / "data/dashboard-decisions.json")


def export_path(runtime: dict[str, Any]) -> Path:
    return Path(runtime.get("dashboard_candidates_path") or Path(runtime["production_project_root"]) / "data/dashboard-candidates.json")


def load_decisions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    # Fail closed if a previously saved decision store is malformed: silently
    # dropping user decisions would bring dismissed postings back into Slack.
    payload = json.loads(path.read_text(encoding="utf-8"))
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("Dashboard decision store must contain a decisions object")
    return {str(key): value for key, value in decisions.items() if isinstance(value, dict)}


def decision_index(decisions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for key, value in sorted(decisions.items(), key=lambda item: str(item[1].get("updated_at") or "")):
        if value.get("status") not in BLOCKED_DECISIONS | {"new"}:
            raise ValueError("Invalid dashboard decision status")
        for alias in candidate_aliases({**(value.get("job") or {}), "id": key, "aliases": [key, *(value.get("aliases") or [])]}):
            indexed[alias] = value
    return indexed


def filter_decisions(candidates: list[dict[str, Any]], decisions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = decision_index(decisions)
    remaining = []
    for candidate in candidates:
        matches = [indexed[alias] for alias in candidate_aliases(candidate) if alias in indexed]
        latest = max(matches, key=lambda item: str(item.get("updated_at") or ""), default={})
        if latest.get("status") not in BLOCKED_DECISIONS:
            remaining.append(candidate)
    return remaining


def dashboard_liveness(candidates: list[dict[str, Any]], current: dict[str, Any], state: dict[str, Any], *, checked_at: str, max_hours: int = 72) -> dict[str, Any]:
    """Keep recent verified reserve for the dashboard without widening Slack.

    Only a saved timestamped result qualifies. Current-run evidence wins over
    the saved result, including an expiry discovered during this run.
    """
    now = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    mapping, details, sources, timestamps = {}, {}, {}, {}
    for url, item in (state.get("items") or {}).items():
        if not isinstance(item, dict) or item.get("status") not in {"active", "expired", "uncertain"}:
            continue
        try:
            at = datetime.fromisoformat(str(item.get("checked_at") or "").replace("Z", "+00:00"))
            if at.tzinfo is None or not timedelta(0) <= now - at <= timedelta(hours=max_hours):
                continue
        except (ValueError, TypeError):
            continue
        mapping[url] = item["status"]
        details[url] = {"code": item.get("evidence") or "recent-liveness-state"}
        sources[url] = item.get("source") or "career-ops-liveness-state"
        timestamps[url] = item["checked_at"]
    mapping.update(current.get("results") or {})
    details.update(current.get("details") or {})
    sources.update(current.get("checker_sources") or {})
    timestamps.update({url: checked_at for url in current.get("results") or {}})
    for candidate in candidates:
        if candidate.get("url") in timestamps:
            candidate["verified_at"] = timestamps[candidate["url"]]
    return {"results": mapping, "details": details, "checker_sources": sources}


def _job(candidate: dict[str, Any], checked_at: str, tracker_entries: list[dict[str, Any]]) -> dict[str, Any]:
    # Explicit allowlist: diagnostic files also contain mail/profile data which
    # must never be copied to the dashboard export.
    fields = ("url", "company", "title", "location", "source_id", "source_label", "source_host", "description", "profile_fit_score", "profile_fit_reasons", "profile_review_required", "evaluation_id", "evaluation_score", "evaluation_score_text", "posting_date", "first_seen", "candidate_origin", "posting_cluster_id", "listing_instance_id", "company_key", "role_key", "liveness", "liveness_checked", "liveness_source", "liveness_evidence", "verification_method", "recommendation_eligible", "dashboard_eligible", "recommendation_cooldown", "recommendation_cooldown_until", "history_gate", "history_reason", "history_tracker_matches", "tracker_matches", "actionability", "actionability_score", "actionability_reasons")
    job = {key: candidate.get(key) for key in fields if key in candidate}
    # role_match means fuzzy text similarity in the legacy resolver; it is not
    # proof that two concrete requisitions are the same application row.
    evaluation_id = str(candidate.get("evaluation_id") or "")
    exact = next((item for item in tracker_entries if evaluation_id and str(item.get("id")) == evaluation_id and str(item.get("company") or "").casefold() == str(candidate.get("company") or "").casefold() and str(item.get("role") or "").casefold() == str(candidate.get("title") or "").casefold()), None)
    job.update(id=candidate_id(candidate), aliases=candidate_aliases(candidate), source=candidate.get("source_id"), site=candidate.get("source_label"), score=candidate.get("profile_fit_score"), reason=" · ".join(str(reason) for reason in candidate.get("profile_fit_reasons") or []), verified_at=candidate.get("verified_at") or (checked_at if candidate.get("liveness_checked") else None), tracker_id=exact.get("id") if exact else None, canonical_status=exact.get("status") if exact else None)
    return job


def build_export(candidates: list[dict[str, Any]], tracker: dict[str, Any], pipeline: dict[str, Any], *, source_run_at: str, source_artifact: str | None = None, mode: str = "apply", reserve: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    jobs = [_job(candidate, source_run_at, tracker.get("entries") or []) for candidate in [*candidates, *(reserve or [])]]
    sources = pipeline.get("source_inventory") or []
    return {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source_run_at": source_run_at, "source_artifact": source_artifact, "mode": mode, "freshness_max_hours": 72, "jobs": jobs, "tracker": {"entries": tracker.get("entries") or []}, "sources": sources, "stats": {"verified_count": len(candidates), "reserve_count": len(reserve or []), "pipeline_pending_count": pipeline.get("pending_count", 0), "pipeline_processed_count": pipeline.get("processed_count", 0)}}


def write_export(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dashboard-candidates-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
