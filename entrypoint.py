#!/usr/bin/env python3
"""Production entrypoint for the separately approved Career-Ops V2 release.

Hermes only executes cron pre-run scripts located below HERMES_HOME/scripts.
During development this file remains inert because config/runtime.json is
locked to dry-run. A release copy must receive the separately reviewed apply
runtime before this entrypoint can pass the orchestrator's second guard.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
import json
import os
import sys
import tempfile
from pathlib import Path


RELEASE_ROOT = Path(__file__).resolve().parent
ORCHESTRATOR = RELEASE_ROOT / "src" / "career_ops_daily_v2.py"
RUNTIME_CONFIG = Path(
    os.environ.get("CAREER_OPS_V2_RUNTIME_CONFIG")
    or RELEASE_ROOT / "config" / "runtime.json"
)
LINKEDIN_CONFIG = RELEASE_ROOT / "config" / "linkedin_queries.json"
ARTIFACT_DIR = RELEASE_ROOT / "artifacts"
RECOMMENDATION_HISTORY_SCHEMA = "career-ops-v2.recommendation-history.v1"
sys.path.insert(0, str(RELEASE_ROOT / "src"))

import career_ops_daily_v2 as orchestrator  # noqa: E402
from slack_report import deliver_slack_bundle, render_slack_bundle  # noqa: E402


def _run_orchestrator() -> dict:
    output = io.StringIO()
    argv = [
        "--mode",
        "apply",
        "--runtime-config",
        str(RUNTIME_CONFIG),
        "--linkedin-config",
        str(LINKEDIN_CONFIG),
        "--profile-evidence",
        str(RELEASE_ROOT / "config" / "profile_evidence.json"),
        "--artifact-dir",
        str(ARTIFACT_DIR),
    ]
    mail_payload = os.environ.get("CAREER_OPS_V2_MAIL_PAYLOAD", "").strip()
    if mail_payload:
        argv.extend(["--mail-payload", mail_payload])
    with redirect_stdout(output):
        return_code = orchestrator.main(argv)
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    if return_code != 0 or not lines:
        raise RuntimeError(f"V2 orchestrator failed with exit code {return_code}")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("V2 orchestrator returned invalid compact JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("V2 orchestrator returned a non-object payload")
    return payload


def _load_existing_artifact(path: Path) -> dict:
    resolved = path.resolve()
    if not resolved.is_relative_to(RELEASE_ROOT.resolve()):
        raise RuntimeError("Existing artifact must be inside the isolated V2 copy")
    diagnostic = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(diagnostic, dict):
        raise RuntimeError("Existing V2 artifact must be a JSON object")
    file_audit = diagnostic.get("file_audit") or {}
    if (
        diagnostic.get("mode") != "apply"
        or file_audit.get("integrity_verified") is not True
        or file_audit.get("verification", {}).get("status") != "ok"
    ):
        raise RuntimeError("Existing V2 artifact is not a verified apply result")
    payload = diagnostic.get("compact_payload")
    if not isinstance(payload, dict) or payload.get("mode") != "apply":
        raise RuntimeError("Existing V2 artifact has no valid compact apply payload")
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    mail = payload.get("mail_audit") if isinstance(payload.get("mail_audit"), dict) else {}
    decisions = diagnostic.get("mail_decisions")
    if isinstance(decisions, list):
        mail["message_count"] = len(decisions)
    usage = diagnostic.get("mail_usage")
    if isinstance(usage, dict):
        mail["usage"] = usage
    payload["mail_audit"] = mail
    return payload


def _recommendation_history_path(runtime: dict) -> Path:
    return orchestrator._recommendation_history_path(runtime)


def _valid_recommendation_url(value: object) -> str | None:
    return orchestrator.canonical_recommendation_url(value)


def _clean_recommendation_items(raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict[str, object]] = {}
    for raw_url, raw_item in raw.items():
        url = _valid_recommendation_url(raw_url)
        if url is None or not isinstance(raw_item, dict):
            continue
        last_recommended_at = raw_item.get("last_recommended_at")
        recommendation_count = raw_item.get("recommendation_count")
        if not isinstance(last_recommended_at, str) or not last_recommended_at.strip():
            continue
        if (
            not isinstance(recommendation_count, int)
            or isinstance(recommendation_count, bool)
            or recommendation_count < 1
        ):
            continue
        previous = cleaned.get(url) or {}
        previous_at = orchestrator._parse_utc_datetime(previous.get("last_recommended_at"))
        candidate_at = orchestrator._parse_utc_datetime(last_recommended_at)
        if previous_at is not None and (candidate_at is None or previous_at >= candidate_at):
            continue
        cleaned[url] = {
            "last_recommended_at": last_recommended_at,
            "recommendation_count": recommendation_count,
        }
    return cleaned


def _record_recommendations(path: Path, rendered_candidate_urls: object) -> None:
    urls: list[str] = []
    seen: set[str] = set()
    if isinstance(rendered_candidate_urls, list):
        for raw_url in rendered_candidate_urls:
            url = _valid_recommendation_url(raw_url)
            if url is not None and url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        return

    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    items = _clean_recommendation_items(
        current.get("items") if isinstance(current, dict) else None
    )
    recorded_at = datetime.now(UTC).isoformat(timespec="seconds")
    for url in urls:
        previous = items.get(url) or {}
        items[url] = {
            "last_recommended_at": recorded_at,
            "recommendation_count": int(previous.get("recommendation_count") or 0) + 1,
        }
    document = {
        "schema_version": RECOMMENDATION_HISTORY_SCHEMA,
        "updated_at": recorded_at,
        "items": items,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    os.environ["CAREER_OPS_V2_ENABLE_APPLY"] = "1"
    runtime = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    slack_delivery = runtime.get("slack_delivery") or {}
    if slack_delivery.get("enabled") is not True:
        raise SystemExit("V2 Slack bundle delivery is not enabled in the release runtime")

    existing_artifact = os.environ.get("CAREER_OPS_V2_EXISTING_ARTIFACT", "").strip()
    payload = (
        _load_existing_artifact(Path(existing_artifact))
        if existing_artifact
        else _run_orchestrator()
    )
    bundle = render_slack_bundle(
        payload,
        root_active_limit=int(slack_delivery.get("root_active_preview_limit") or 5),
        root_max_chars=int(slack_delivery.get("root_max_chars") or 2400),
        prefix=os.environ.get("CAREER_OPS_V2_SLACK_PREFIX") or None,
    )
    delivery = deliver_slack_bundle(
        bundle,
        hermes_bin=Path(slack_delivery["hermes_bin"]),
        target=str(slack_delivery["target"]),
    )
    root_confirmed = bool(delivery.get("message_id")) and delivery.get("status") in {
        "ok",
        "partial",
    }
    if not root_confirmed:
        raise RuntimeError("V2 Slack root delivery was not confirmed")
    _record_recommendations(
        _recommendation_history_path(runtime),
        bundle.get("rendered_candidate_urls"),
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (ARTIFACT_DIR / f"slack-delivery-{stamp}.json").write_text(
        json.dumps(
            {
                "schema_version": "career-ops-v2.slack-delivery.v1",
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bundle_lengths": bundle["lengths"],
                "source_artifact": existing_artifact or None,
                "delivery": delivery,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if delivery.get("status") != "ok":
        raise RuntimeError("V2 Slack root was delivered but one or more replies failed")
    # The V2 script already delivered the root and its replies. no_agent cron
    # treats this exact sentinel as success without a duplicate auto-delivery.
    print("[SILENT]")


if __name__ == "__main__":
    main()
