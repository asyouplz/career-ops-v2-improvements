#!/usr/bin/env python3
"""Reconstruct dashboard data without running collectors, mail, or Slack.

Default: read existing evidence and print JSON. --verify-max N optionally makes
at most N public job-page liveness checks; --output is the only write operation.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys

import career_ops_daily_v2 as pipeline
from dashboard_pipeline import build_export, canonical_job_url, decision_path, filter_decisions, load_decisions, write_export


def recent(value: object, now: datetime, max_hours: int) -> bool:
    parsed = pipeline._parse_utc_datetime(value)
    return parsed is not None and timedelta(0) <= now - parsed <= timedelta(hours=max_hours)


def reconstruct(runtime: dict, *, artifact_dir: Path, config: dict, profile: dict, verify_max: int = 0, max_hours: int = 72) -> dict:
    now = datetime.now(timezone.utc)
    root = Path(runtime["production_project_root"])
    paths = sorted(artifact_dir.glob("apply-*.json"), reverse=True)
    artifact = {}
    artifact_path = None
    for path in paths:
        try:
            item = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("schema_version") == "career-ops-v2.diagnostic.v1":
            artifact, artifact_path = item, path
            break
    run_at = artifact.get("started_at") or ""
    raw_pipeline = pipeline.parse_pipeline(root / "data/pipeline.md")
    tracker = pipeline.parse_application_tracker(root / "data/applications.md")
    pending = raw_pipeline.get("pending") or []
    processed = raw_pipeline.get("processed_candidates") or []
    direct = [dict(item) for item in (artifact.get("direct") or {}).get("candidates") or [] if isinstance(item, dict)]
    for item in direct:
        if not recent(item.get("verified_at") or run_at, now, max_hours):
            item["direct_verified"] = False
            item["liveness"] = "not_checked"
    groups = [direct, pending, processed]
    pipeline.annotate_candidate_freshness(groups, pipeline.load_scan_history_metadata(root / "data/scan-history.tsv"))
    identity = pipeline.annotate_identity_records(runtime, tracker, {}, groups)
    if identity.get("status") != "ok":
        raise RuntimeError("Identity resolution unavailable for dashboard bootstrap")
    decisions = load_decisions(decision_path(runtime))
    raw = [*direct, *pending, *processed]
    candidates = pipeline.select_candidates([], raw, config, limit=None, tracker=tracker, profile=profile)
    instances, clusters, selected = set(), set(), []
    for item in candidates:
        instance = canonical_job_url(item.get("url")) or item.get("listing_instance_id") or item["url"]
        cluster = item.get("posting_cluster_id") or instance
        if instance in instances:
            continue
        instances.add(instance)
        clusters.add(cluster)
        selected.append(item)
    # Reuse only timestamped recent evidence, never an undated active flag.
    mapping, details, sources, timestamps = {}, {}, {}, {}
    state = pipeline.load_liveness_state(pipeline._liveness_state_path(runtime))
    for url, item in (state.get("items") or {}).items():
        if isinstance(item, dict) and recent(item.get("checked_at"), now, max_hours):
            mapping[url] = item.get("status")
            details[url] = {"code": item.get("evidence") or "recent-liveness-state"}
            sources[url] = item.get("source") or "career-ops-liveness-state"
            timestamps[url] = item.get("checked_at")
    if recent(run_at, now, max_hours):
        evidence = artifact.get("liveness") or {}
        for url, status in (evidence.get("results") or {}).items():
            if not timestamps.get(url) or pipeline._parse_utc_datetime(run_at) >= pipeline._parse_utc_datetime(timestamps[url]):
                mapping[url] = status
                details[url] = (evidence.get("details") or {}).get(url, {})
                sources[url] = (evidence.get("checker_sources") or {}).get(url, "career-ops-liveness-artifact")
                timestamps[url] = run_at
    for item in selected:
        if item.get("url") in mapping:
            item["liveness"] = mapping[item["url"]]
            item["verified_at"] = timestamps[item["url"]]
        elif item.get("direct_verified") and item.get("liveness") == "active":
            item["verified_at"] = item.get("verified_at") or run_at
    checked = 0
    if verify_max:
        check_pool = filter_decisions(selected, load_decisions(decision_path(runtime)))
        checks = pipeline.run_liveness_precheck(runtime, pipeline._source_round_robin(check_pool, len(check_pool)), max_checks=min(verify_max, 100), skip_network=False)
        checked = checks.get("checked", 0)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mapping.update(checks.get("results") or {})
        details.update(checks.get("details") or {})
        sources.update(checks.get("checker_sources") or {})
        for item in selected:
            if item["url"] in (checks.get("results") or {}):
                item["verified_at"] = checked_at
    liveness = {"results": mapping, "details": details, "checker_sources": sources}
    verified = pipeline.apply_liveness_results(selected, liveness, limit=None, include_cooldown=True)
    urls = {item["url"] for item in verified}
    reserve = [{**item, "liveness": mapping.get(item["url"], "not_checked"), "liveness_checked": item["url"] in mapping, "dashboard_eligible": False} for item in selected if item["url"] not in urls]
    result = build_export(verified, tracker, raw_pipeline, source_run_at=run_at, source_artifact=str(artifact_path) if artifact_path else None, mode="bootstrap", reserve=reserve)
    result["freshness_max_hours"] = max_hours
    result["stats"]["bootstrap_live_checks"] = checked
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=pipeline.DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-max", type=int, default=0)
    parser.add_argument("--max-hours", type=int, default=72)
    args = parser.parse_args()
    runtime = json.loads(args.runtime.read_text())
    script_root = args.runtime.parent.parent
    result = reconstruct(runtime, artifact_dir=script_root / "artifacts", config=json.loads((script_root / "config/linkedin_queries.json").read_text()), profile=pipeline.load_profile_evidence(script_root / "config/profile_evidence.json"), verify_max=max(0, min(args.verify_max, 100)), max_hours=max(1, args.max_hours))
    if args.output:
        write_export(args.output, result)
        print(json.dumps({"output": str(args.output), "stats": result["stats"]}))
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()


if __name__ == "__main__":
    main()
