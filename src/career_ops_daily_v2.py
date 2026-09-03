#!/usr/bin/env python3
"""Staged Career-Ops V2 pre-run orchestrator.

Dry-run is the default and is designed to be non-mutating with respect to the
live Hermes cron definition, legacy collectors, pipeline, and applications
tracker. Apply mode is deliberately double-locked until the later activation
approval.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo


MODULE_DIR = Path(__file__).resolve().parent
STAGING_ROOT = MODULE_DIR.parent
DEFAULT_RUNTIME_CONFIG = STAGING_ROOT / "config" / "runtime.json"
DEFAULT_LINKEDIN_CONFIG = STAGING_ROOT / "config" / "linkedin_queries.json"
DEFAULT_PROFILE_EVIDENCE = STAGING_ROOT / "config" / "profile_evidence.json"
DIRECT_FETCH_SCRIPT = MODULE_DIR / "linkedin_direct_fetch.mjs"
DIRECT_INGEST_SCRIPT = MODULE_DIR / "linkedin_direct_ingest.mjs"
SOURCE_LIVENESS_SCRIPT = MODULE_DIR / "source_liveness.mjs"
MAX_ERROR_CHARS = 500
MAIL_REVIEW_QUEUE_FILENAME = "mail-review-queue.json"
MAIL_REVIEW_SLACK_LIMIT = 5
MAIL_REVIEW_UNMATCHED_ARCHIVE_DAYS = 90
MAIL_REVIEW_METADATA_RETENTION_DAYS = 180
IDENTITY_RESOLVER_FILENAME = "identity-resolver.mjs"

PIPELINE_BUCKET_LABELS = {
    "신규 지원 후보": "clean_new",
    "조건부 재지원 후보": "conditional_reapply",
    "수집 대기 후보": "collection_waiting",
}
HISTORY_HARD_BLOCK_STATUSES = {
    "applied",
    "responded",
    "interview",
    "offer",
    "rejected",
    "discarded",
}
HISTORY_COMPANY_BLOCK_STATUSES = {
    "applied",
    "responded",
    "interview",
    "offer",
    "rejected",
}
HISTORY_PENDING_RECONCILE_REASONS = {
    "same_run_mail_application",
}

PROCESSED_INACTIVE_TERMS = (
    "already applied",
    "application submitted",
    "interview",
    "offer",
    "rejected",
    "expired",
    "inactive",
    "메일 확인",
    "이미 지원",
    "지원 완료",
    "지원 비권고",
    "불합격",
    "전형 종료",
)
SEARCH_FIRM_TERMS = (
    "search firm",
    "headhunt",
    "recruiter listing",
    "유니코써치",
    "프로써치",
    "서치펌",
    "써치펌",
    "헤드헌",
)

SOURCE_DOMAIN_RULES = (
    ("linkedin.com", "linkedin", "LinkedIn"),
    ("saramin.co.kr", "saramin", "사람인"),
    ("wanted.co.kr", "wanted", "원티드"),
    ("rememberapp.co.kr", "remember", "리멤버"),
    ("jobkorea.co.kr", "jobkorea", "잡코리아"),
    ("greenhouse.io", "greenhouse", "Greenhouse"),
)

MULTI_LABEL_SUFFIXES = {
    "co.kr",
    "or.kr",
    "go.kr",
    "ne.kr",
    "ac.kr",
}


def _clean(value: Any, limit: int = 1000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _nonnegative_int(value: Any, default: int) -> int:
    """Parse a non-negative runtime integer while preserving an explicit zero."""
    candidate = default if value is None or value == "" else value
    try:
        return max(0, int(candidate))
    except (TypeError, ValueError):
        return max(0, int(default))


def classify_job_source(value: Any) -> dict[str, str]:
    """Return stable source metadata while accepting future pipeline domains."""
    raw_url = _clean(value, 1200)
    try:
        host = (urlparse(raw_url).hostname or "").casefold().rstrip(".")
    except ValueError:
        host = ""
    if not host:
        return {
            "source_id": "unknown",
            "source_label": "출처 미확인",
            "source_host": "",
        }
    for domain, source_id, label in SOURCE_DOMAIN_RULES:
        if host == domain or host.endswith(f".{domain}"):
            return {
                "source_id": source_id,
                "source_label": label,
                "source_host": host,
            }

    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        base_domain = ".".join(labels[-3:])
    elif len(labels) >= 2:
        base_domain = ".".join(labels[-2:])
    else:
        base_domain = host
    source_id = re.sub(r"[^0-9a-z]+", "-", base_domain).strip("-") or "unknown"
    return {
        "source_id": source_id,
        "source_label": base_domain,
        "source_host": host,
    }


def _json_load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_files(paths: list[str]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for raw in paths:
        path = Path(raw)
        snapshot[str(path)] = {
            "exists": path.exists(),
            "is_file": path.is_file(),
            "size": path.stat().st_size if path.is_file() else None,
            "sha256": _sha256(path),
        }
    return snapshot


def changed_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    return [path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


def load_profile_evidence(path: Path) -> dict[str, Any]:
    """Load only curated profile facts whose live source hashes still match."""
    try:
        configured = _json_load(path)
    except Exception as exc:  # noqa: BLE001 - bounded diagnostic for scheduled run
        return {
            "status": "unavailable",
            "sources_verified": False,
            "facts": [],
            "error": _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS),
        }

    source_results: list[dict[str, Any]] = []
    for source in configured.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_path = Path(str(source.get("path") or ""))
        expected = _clean(source.get("sha256"), 128)
        actual = _sha256(source_path)
        source_results.append(
            {
                "id": _clean(source.get("id"), 80),
                "status": "verified" if expected and actual == expected else "mismatch",
            }
        )

    sources_verified = bool(source_results) and all(
        item["status"] == "verified" for item in source_results
    )
    facts: list[dict[str, Any]] = []
    if sources_verified:
        for raw in (configured.get("facts") or [])[:8]:
            if not isinstance(raw, dict):
                continue
            fact_id = _clean(raw.get("id"), 80)
            evidence = _clean(raw.get("evidence"), 360)
            keywords = [
                _clean(term, 80)
                for term in (raw.get("match_terms") or [])[:12]
                if _clean(term, 80)
            ]
            if fact_id and evidence and keywords:
                facts.append(
                    {
                        "id": fact_id,
                        "evidence": evidence,
                        "match_terms": keywords,
                    }
                )

    return {
        "status": "verified" if sources_verified else "stale",
        "sources_verified": sources_verified,
        "source_checks": source_results,
        "experience_years_min": configured.get("experience_years_min") if sources_verified else None,
        "target_levels": [
            _clean(value, 80) for value in (configured.get("target_levels") or [])[:5]
        ] if sources_verified else [],
        "facts": facts,
        "error": None if sources_verified else "Curated profile evidence does not match all live sources",
    }


def attach_profile_matches(
    candidates: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    facts = profile.get("facts") if profile.get("status") == "verified" else []
    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        haystack = " ".join(
            str(item.get(key) or "") for key in ("title", "description")
        ).casefold()
        item["profile_evidence_ids"] = [
            str(fact.get("id"))
            for fact in facts or []
            if any(str(term).casefold() in haystack for term in fact.get("match_terms") or [])
        ][:3]
        matched.append(item)
    return matched


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 180,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env or os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "error",
            "exit_code": completed.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "exit_code": None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "status": "error",
            "exit_code": None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def _parse_last_json(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    if stripped:
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(value, dict):
                return value
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Command produced no JSON object")


def _command_error(result: dict[str, Any], fallback: str) -> str:
    detail = result.get("stderr") or result.get("stdout") or fallback
    return _clean(detail, MAX_ERROR_CHARS)


def runtime_subprocess_env(runtime: dict[str, Any]) -> dict[str, str]:
    """Ensure absolute runtime executables can also resolve their shebang tools."""
    env = os.environ.copy()
    node_dir = str(Path(str(runtime.get("node_bin") or "")).parent)
    current_path = env.get("PATH", "")
    path_parts = [part for part in current_path.split(os.pathsep) if part]
    if node_dir and node_dir not in path_parts:
        env["PATH"] = os.pathsep.join([node_dir, *path_parts])
    project_root_value = runtime.get("production_project_root")
    if project_root_value:
        project_root = Path(str(project_root_value)).resolve()
        env["CAREER_OPS_PROJECT_ROOT"] = str(project_root)
        env["CAREER_OPS_EXPECTED_PROJECT_ROOT"] = str(project_root)
        env["CAREER_OPS_COLLECTOR_TIMEOUT_SECONDS"] = str(
            int(runtime.get("collector_timeout_seconds") or 600)
        )
    if runtime.get("codex_bin"):
        env["CAREER_OPS_CODEX_BIN"] = str(runtime["codex_bin"])
    if runtime.get("legacy_ddgs_script"):
        env["CAREER_OPS_LEGACY_LINKEDIN_FETCH"] = str(runtime["legacy_ddgs_script"])
    run_id = _clean(runtime.get("_run_id"), 120)
    if run_id:
        env["CAREER_OPS_RUN_ID"] = run_id
    return env


def validate_collector_project_root(
    runtime: dict[str, Any],
    collector: dict[str, Any],
    project_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cross-check the collector-reported root before downstream writes."""
    expected = Path(str(runtime["production_project_root"])).resolve()
    guard = project_guard if isinstance(project_guard, dict) else {}
    summary = collector.get("summary") if isinstance(collector.get("summary"), dict) else {}
    reported = collector.get("project_root") or summary.get("project_root") or guard.get("project_root")
    actual = None
    error = None
    if reported:
        try:
            actual = Path(str(reported)).resolve(strict=True)
        except OSError as exc:
            error = f"Collector project root is unavailable: {type(exc).__name__}: {exc}"
    else:
        error = "Collector did not report its project root"
    guard_ok = guard.get("root_verified") is True
    verified = actual == expected and guard_ok
    if error is None and actual != expected:
        error = f"Collector project root mismatch: expected {expected}, got {actual}"
    elif error is None and not guard_ok:
        error = _clean(guard.get("error"), MAX_ERROR_CHARS) or "Collector root guard did not verify"
    return {
        "status": "ok" if verified else "error",
        "root_verified": verified,
        "project_root": str(actual) if actual else str(reported or ""),
        "expected_project_root": str(expected),
        "run_id": collector.get("run_id") or summary.get("run_id") or guard.get("run_id"),
        "error": error,
    }


def run_direct_linkedin(
    runtime: dict[str, Any],
    linkedin_config_path: Path,
    *,
    skip_network: bool,
    max_details: int | None,
) -> dict[str, Any]:
    if skip_network:
        return {
            "schema_version": 1,
            "status": "skipped_network",
            "source": "linkedin-direct-public",
            "fallback_required": False,
            "candidates": [],
        }
    seen_urls = collect_seen_linkedin_urls(Path(runtime["production_project_root"]))
    command = [
        str(runtime["node_bin"]),
        str(DIRECT_FETCH_SCRIPT),
        "--config",
        str(linkedin_config_path),
        "--project-root",
        str(runtime["production_project_root"]),
    ]
    with tempfile.TemporaryDirectory(prefix="career-ops-v2-seen-") as tmp:
        seen_path = Path(tmp) / "seen-linkedin-urls.json"
        seen_path.write_text(json.dumps(sorted(seen_urls)), encoding="utf-8")
        command.extend(["--seen-urls", str(seen_path)])
        if max_details is not None:
            command.extend(["--max-details", str(max_details)])
        result = _run(command, cwd=STAGING_ROOT, timeout=150)
    try:
        payload = _parse_last_json(result["stdout"])
    except Exception as exc:  # noqa: BLE001 - bounded failure payload for cron staging
        payload = {
            "schema_version": 1,
            "status": "error",
            "source": "linkedin-direct-public",
            "fallback_required": True,
            "candidates": [],
            "error": _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS),
        }
    payload["process"] = {
        "status": result["status"],
        "exit_code": result["exit_code"],
        "elapsed_seconds": result["elapsed_seconds"],
        "error": None if result["status"] == "ok" else _command_error(result, "direct LinkedIn failed"),
    }
    return payload


def _canonical_linkedin_url(value: str) -> str | None:
    match = re.search(r"https://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/[^\s|)>\]]+", value, re.IGNORECASE)
    if not match:
        return None
    segment = match.group(0).split("?", 1)[0].rstrip("/.,")
    id_match = re.search(r"(\d{6,})$", segment)
    return f"https://www.linkedin.com/jobs/view/{id_match.group(1)}" if id_match else None


def collect_seen_linkedin_urls(project_root: Path) -> set[str]:
    paths = [
        project_root / "data" / "pipeline.md",
        project_root / "data" / "scan-history.tsv",
        project_root / "data" / "applications.md",
    ]
    urls: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        for match in re.finditer(
            r"https://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/[^\s|)>\]]+",
            path.read_text(encoding="utf-8"),
            re.IGNORECASE,
        ):
            canonical = _canonical_linkedin_url(match.group(0))
            if canonical:
                urls.add(canonical)
    return urls


def _resolve_hermes_python() -> Path | None:
    home = Path.home()
    candidates = [
        home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python",
        home / "hermes-agent" / "venv" / "bin" / "python",
        home / ".local" / "share" / "hermes" / "venv" / "bin" / "python",
    ]
    return next((path for path in candidates if path.is_file()), None)


def run_ddgs_fallback(runtime: dict[str, Any]) -> dict[str, Any]:
    python = _resolve_hermes_python()
    script = Path(runtime["legacy_ddgs_script"])
    if python is None or not script.is_file():
        return {
            "status": "unavailable",
            "source": "linkedin-site-search-via-ddgs",
            "result_count": 0,
            "error": "Hermes Python or legacy DDGS script not found",
        }
    attempts: list[dict[str, Any]] = []
    total_elapsed = 0.0
    last_error = "DDGS fallback failed"
    for attempt in range(1, 3):
        result = _run(
            [str(python), str(script)],
            cwd=Path(runtime["production_project_root"]),
            timeout=120,
        )
        total_elapsed += float(result["elapsed_seconds"] or 0)
        try:
            raw = _parse_last_json(result["stdout"])
            items = raw.get("results") if isinstance(raw.get("results"), list) else []
            raw_status = str(raw.get("status") or result["status"])
            last_error = "" if raw_status == "ok" else _clean(
                raw.get("error") or _command_error(result, "DDGS fallback failed"),
                MAX_ERROR_CHARS,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "status": raw_status,
                    "result_count": len(items),
                    "elapsed_seconds": result["elapsed_seconds"],
                    "error": last_error or None,
                }
            )
            if raw_status == "ok":
                return {
                    "status": "ok",
                    "source": "linkedin-site-search-via-ddgs",
                    "query_count": raw.get("query_count", 0),
                    "result_count": len(items),
                    "elapsed_seconds": round(total_elapsed, 2),
                    "attempts": attempts,
                    "results": [
                        {
                            "title": _clean(item.get("title"), 300),
                            "url": _clean(item.get("url") or item.get("href"), 1200),
                            "description": _clean(item.get("description") or item.get("body"), 500),
                        }
                        for item in items[:10]
                        if isinstance(item, dict)
                    ],
                    "error": None,
                }
        except Exception as exc:  # noqa: BLE001
            last_error = _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS)
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "error",
                    "result_count": 0,
                    "elapsed_seconds": result["elapsed_seconds"],
                    "error": last_error,
                }
            )
        if attempt == 1:
            time.sleep(1)
    return {
        "status": "error",
        "source": "linkedin-site-search-via-ddgs",
        "query_count": 0,
        "result_count": 0,
        "elapsed_seconds": round(total_elapsed, 2),
        "attempts": attempts,
        "results": [],
        "error": last_error,
    }


def run_direct_ingest_preview(
    runtime: dict[str, Any],
    direct_payload_path: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    command = [
        str(runtime["node_bin"]),
        str(DIRECT_INGEST_SCRIPT),
        "--input",
        str(direct_payload_path),
        "--project-root",
        str(runtime["production_project_root"]),
        "--apply" if apply else "--dry-run",
    ]
    result = _run(command, cwd=STAGING_ROOT, timeout=120)
    try:
        payload = _parse_last_json(result["stdout"])
    except Exception as exc:  # noqa: BLE001
        payload = {
            "status": "error",
            "source": "linkedin-direct-public",
            "dry_run": not apply,
            "error": _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS),
        }
    payload["process"] = {
        "status": result["status"],
        "exit_code": result["exit_code"],
        "elapsed_seconds": result["elapsed_seconds"],
        "error": None if result["status"] == "ok" else _command_error(result, "direct ingest failed"),
    }
    return payload


def run_legacy_component(runtime: dict[str, Any], flag: str) -> dict[str, Any]:
    script = Path(runtime["legacy_prerun_script"])
    if not script.is_file():
        return {"status": "unavailable", "error": f"Legacy pre-run not found: {script}"}
    collector_timeout = int(runtime.get("collector_timeout_seconds") or 600)
    timeout = 480 if flag == "--mail-only" else collector_timeout + 60
    result = _run(
        [sys.executable, str(script), flag],
        cwd=Path(runtime["production_project_root"]),
        timeout=timeout,
        env=runtime_subprocess_env(runtime),
    )
    try:
        payload = _parse_last_json(result["stdout"])
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "elapsed_seconds": result["elapsed_seconds"],
            "error": _clean(
                f"{type(exc).__name__}: {exc}; "
                f"{_command_error(result, 'legacy component produced no JSON')}",
                MAX_ERROR_CHARS,
            ),
        }
    payload["process_elapsed_seconds"] = result["elapsed_seconds"]
    if result["status"] != "ok":
        payload["process_error"] = _command_error(result, f"legacy {flag} failed")
    return payload


def parse_multi_board_output(stdout: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for line in stdout.splitlines():
        if not line.startswith("SCAN_SUMMARY_JSON="):
            continue
        try:
            parsed = json.loads(line.split("=", 1)[1])
        except json.JSONDecodeError:
            break
        if isinstance(parsed, dict):
            summary.update(parsed)
        break

    patterns = {
        "companies_scanned": r"Companies scanned:\s*(\d+)",
        "job_boards_scanned": r"Job boards scanned:\s*(\d+)",
        "total_jobs_found": r"Total jobs found:\s*(\d+)",
        "filtered_by_title": r"Filtered by title:\s*(\d+)",
        "filtered_by_location": r"Filtered by location:\s*(\d+)",
        "filtered_by_salary": r"Filtered by salary:\s*(\d+)",
        "filtered_by_content": r"Filtered by content:\s*(\d+)",
        "duplicates": r"Duplicates:\s*(\d+)",
        "new_offers_found": r"New offers added:\s*(\d+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, stdout)
        if match and key not in summary:
            summary[key] = int(match.group(1))

    previews: list[dict[str, str]] = []
    reading_offers = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line == "New offers:":
            reading_offers = True
            continue
        if not reading_offers:
            continue
        if line.startswith("(dry run") or not line:
            if previews or line.startswith("(dry run"):
                break
            continue
        match = re.match(r"^\+\s+(.+?)\s+\|\s+(.+?)\s+\|\s+(.+)$", line)
        if not match:
            continue
        location = re.sub(r"\s+\[(?:Trust|BLACKLISTED).*", "", match.group(3)).strip()
        previews.append(
            {
                "company": _clean(match.group(1), 200),
                "title": _clean(match.group(2), 300),
                "location": _clean(location, 200),
            }
        )
        if len(previews) >= 10:
            break
    return {"summary": summary, "new_offer_preview": previews}


def run_multi_board_preview(runtime: dict[str, Any]) -> dict[str, Any]:
    """Validate the legacy wrapper, then run the scanner in no-write mode."""
    project_root = Path(runtime["production_project_root"])
    script = project_root / "scan.mjs"
    if not script.is_file():
        return {
            "status": "unavailable",
            "dry_run": True,
            "summary": {},
            "new_offer_preview": [],
            "error": f"Multi-board scanner not found: {script}",
        }
    root_payload = run_legacy_component(runtime, "--check-project-root")
    project_guard = (
        root_payload.get("project_guard")
        if isinstance(root_payload.get("project_guard"), dict)
        else {}
    )
    root_validation = validate_collector_project_root(
        runtime,
        {
            "project_root": project_guard.get("project_root"),
            "run_id": project_guard.get("run_id"),
            "summary": {},
        },
        project_guard,
    )
    if not root_validation["root_verified"]:
        return {
            "status": "error",
            "dry_run": True,
            "writes_enabled": False,
            "summary": {},
            "new_offer_preview": [],
            **root_validation,
        }
    result = _run(
        [str(runtime["node_bin"]), str(script), "--dry-run", "--verify"],
        cwd=project_root,
        timeout=int(runtime.get("collector_timeout_seconds") or 300),
        env=runtime_subprocess_env(runtime),
    )
    parsed = parse_multi_board_output(result.get("stdout") or "")
    summary = parsed["summary"]
    scanner_root = validate_collector_project_root(
        runtime,
        {
            "project_root": summary.get("project_root"),
            "run_id": summary.get("run_id"),
            "summary": summary,
        },
        project_guard,
    )
    return {
        "status": result["status"] if scanner_root["root_verified"] else "error",
        "dry_run": True,
        "writes_enabled": False,
        "elapsed_seconds": result["elapsed_seconds"],
        "summary": summary,
        "new_offer_preview": parsed["new_offer_preview"],
        **scanner_root,
        "error": (
            scanner_root.get("error")
            if not scanner_root["root_verified"]
            else None
            if result["status"] == "ok"
            else _command_error(result, "multi-board dry-run failed")
        ),
    }


def run_project_verification(runtime: dict[str, Any]) -> dict[str, Any]:
    """Run the production project's read-only integrity verifier once."""
    project_root = Path(runtime["production_project_root"])
    script = project_root / "verify-pipeline.mjs"
    if not script.is_file():
        return {
            "status": "unavailable",
            "exit_code": None,
            "elapsed_seconds": 0,
            "summary": "",
            "error": f"Pipeline verifier not found: {script}",
        }
    result = _run(
        [str(runtime["node_bin"]), str(script)],
        cwd=project_root,
        timeout=180,
        env=runtime_subprocess_env(runtime),
    )
    stdout = _clean(result.get("stdout"), 500)
    return {
        "status": result["status"],
        "exit_code": result.get("exit_code"),
        "elapsed_seconds": result.get("elapsed_seconds", 0),
        "summary": stdout,
        "error": None
        if result["status"] == "ok"
        else _command_error(result, "pipeline verification failed"),
    }


def build_file_audit(
    *,
    mode: str,
    protected_paths: list[str],
    changed: list[str],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Separate dry-run immutability from expected apply-mode writes."""
    if mode == "dry-run":
        return {
            "mode": mode,
            "policy": "no_writes",
            "status": "dry_run_violation" if changed else "dry_run_passed",
            "checked_files": len(protected_paths),
            "changed_count": len(changed),
            "changed": changed,
            "integrity_verified": not changed,
            "verification": {"status": "not_run_in_dry_run"},
        }

    verification = run_project_verification(runtime)
    verified = verification.get("status") == "ok"
    if verified:
        status = "apply_verified_changes" if changed else "apply_verified_no_changes"
    else:
        status = "apply_verification_failed"
    return {
        "mode": mode,
        "policy": "writes_allowed",
        "status": status,
        "checked_files": len(protected_paths),
        "changed_count": len(changed),
        "changed": changed,
        "integrity_verified": verified,
        "verification": verification,
    }


def parse_liveness_output(stdout: str) -> dict[str, str]:
    results: dict[str, str] = {}
    pattern = re.compile(
        r"^(?:✅|❌|⚠️)\s+(active|expired|uncertain)\s+(?:\(api\)\s+)?\s*(https?://\S+)",
        re.IGNORECASE,
    )
    for line in stdout.splitlines():
        match = pattern.search(line.strip())
        if match:
            results[match.group(2)] = match.group(1).lower()
    return results


def _liveness_state_path(runtime: dict[str, Any]) -> Path:
    configured = runtime.get("liveness_state_path")
    path = (
        Path(str(configured))
        if configured
        else Path(str(runtime["production_project_root"])) / "data" / "liveness-state.json"
    )
    if not path.is_absolute():
        path = Path(str(runtime["production_project_root"])) / path
    return path


def load_liveness_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": "career-ops-v2.liveness-state.v1",
            "updated_at": None,
            "items": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": "career-ops-v2.liveness-state.v1",
            "updated_at": None,
            "items": {},
        }
    items = payload.get("items") if isinstance(payload, dict) else {}
    return {
        "schema_version": "career-ops-v2.liveness-state.v1",
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
        "items": items if isinstance(items, dict) else {},
    }


def _parse_utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


RECOMMENDATION_TRACKING_PARAMS = {
    "language",
    "lang",
    "locale",
    "src",
    "source",
    "gh_src",
    "lever-origin",
    "lever-source",
    "rltr",
    "trackingid",
    "tracking_id",
    "trk",
    "ref",
    "t_ref",
    "t_ref_content",
}


def canonical_recommendation_url(value: Any) -> str | None:
    """Normalize a posting URL so tracking variants share one cooldown key."""
    try:
        parsed = urlparse(str(value).strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = (re.sub(r"/+$", "", parsed.path) or "/").casefold()
    query_items = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in RECOMMENDATION_TRACKING_PARAMS
    ]
    return urlunparse(
        (
            parsed.scheme.casefold(),
            host,
            path,
            "",
            urlencode(sorted(query_items)),
            "",
        )
    )


def annotate_liveness_recency(
    candidate_groups: list[list[dict[str, Any]]],
    state: dict[str, Any],
    *,
    max_age_days: int = 7,
    now: datetime | None = None,
) -> dict[str, int]:
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    checked_now = (now or datetime.now(UTC)).astimezone(UTC)
    ttl_days = max(0, int(max_age_days))
    stats = {"matched": 0, "cached_expired": 0, "stale_expired": 0}
    for group in candidate_groups:
        for candidate in group:
            url = str(candidate.get("url") or "")
            cached = items.get(url)
            if not isinstance(cached, dict):
                continue
            stats["matched"] += 1
            checked_at = str(cached.get("checked_at") or "")
            candidate["_last_liveness_checked_at"] = checked_at or None
            parsed = _parse_utc_datetime(checked_at)
            if parsed is not None:
                candidate["_last_liveness_checked_epoch"] = int(parsed.timestamp())
            else:
                candidate["_last_liveness_checked_epoch"] = None
            if cached.get("status") == "expired":
                cache_age = checked_now - parsed if parsed is not None else None
                cache_is_current = (
                    cache_age is not None
                    and cache_age.total_seconds() >= 0
                    and cache_age <= timedelta(days=ttl_days)
                )
                if cache_is_current:
                    candidate["_cached_liveness_expired"] = True
                    stats["cached_expired"] += 1
                else:
                    candidate["_cached_liveness_expired"] = False
                    stats["stale_expired"] += 1
    return stats


def _recommendation_history_path(runtime: dict[str, Any]) -> Path:
    project_root = Path(str(runtime["production_project_root"])).expanduser()
    configured = runtime.get("recommendation_history_path")
    path = (
        Path(str(configured)).expanduser()
        if configured
        else project_root / "data" / "recommendation-history.json"
    )
    if not path.is_absolute():
        path = project_root / path
    return path


def load_recommendation_history(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "career-ops-v2.recommendation-history.v1", "items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "career-ops-v2.recommendation-history.v1", "items": {}}
    raw_items = payload.get("items") if isinstance(payload, dict) else {}
    items: dict[str, Any] = {}
    if isinstance(raw_items, dict):
        for raw_url, raw_item in raw_items.items():
            url = canonical_recommendation_url(raw_url)
            if url is None or not isinstance(raw_item, dict):
                continue
            previous = items.get(url)
            if previous is None or (
                _parse_utc_datetime(raw_item.get("last_recommended_at"))
                or datetime.min.replace(tzinfo=UTC)
            ) > (
                _parse_utc_datetime(previous.get("last_recommended_at"))
                or datetime.min.replace(tzinfo=UTC)
            ):
                items[url] = raw_item
    return {
        "schema_version": "career-ops-v2.recommendation-history.v1",
        "items": items,
    }


def annotate_recommendation_cooldown(
    candidate_groups: list[list[dict[str, Any]]],
    history: dict[str, Any],
    *,
    cooldown_days: int = 7,
    now: datetime | None = None,
) -> dict[str, int]:
    items = history.get("items") if isinstance(history.get("items"), dict) else {}
    checked_now = (now or datetime.now(UTC)).astimezone(UTC)
    window = timedelta(days=max(0, int(cooldown_days)))
    stats = {"matched": 0, "cooling_down": 0, "expired": 0}
    for group in candidate_groups:
        for candidate in group:
            url = canonical_recommendation_url(candidate.get("url"))
            record = items.get(url) if url is not None else None
            if not isinstance(record, dict):
                continue
            stats["matched"] += 1
            last_recommended = _parse_utc_datetime(record.get("last_recommended_at"))
            if last_recommended is None:
                stats["expired"] += 1
                continue
            age = checked_now - last_recommended
            if age.total_seconds() >= 0 and age < window:
                candidate["_recommendation_cooldown"] = True
                candidate["_recommendation_cooldown_until"] = (
                    last_recommended + window
                ).isoformat(timespec="seconds")
                stats["cooling_down"] += 1
            else:
                stats["expired"] += 1
    return stats


def persist_liveness_state(
    runtime: dict[str, Any],
    liveness: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    path = _liveness_state_path(runtime)
    results = liveness.get("results") if isinstance(liveness.get("results"), dict) else {}
    details = liveness.get("details") if isinstance(liveness.get("details"), dict) else {}
    current = load_liveness_state(path)
    items = dict(current.get("items") or {})
    now = datetime.now(UTC).isoformat(timespec="seconds")
    changed = 0
    for url, status in results.items():
        if status not in {"active", "expired", "uncertain"}:
            continue
        detail = details.get(url) if isinstance(details.get(url), dict) else {}
        items[str(url)] = {
            "status": status,
            "checked_at": now,
            "source": _clean(detail.get("source"), 100) or classify_job_source(str(url))["source_id"],
            "code": _clean(detail.get("code"), 120) or None,
        }
        changed += 1
    if mode == "apply" and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "career-ops-v2.liveness-state.v1",
            "updated_at": now,
            "items": items,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
    return {
        "status": "updated" if mode == "apply" and changed else "preview" if changed else "unchanged",
        "path": str(path),
        "updated_count": changed,
        "total_count": len(items),
    }


def run_liveness_precheck(
    runtime: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    max_checks: int,
    skip_network: bool,
) -> dict[str, Any]:
    if skip_network:
        return {
            "status": "skipped_network",
            "checked": 0,
            "results": {},
            "elapsed_seconds": 0,
            "error": None,
        }
    urls = [
        str(candidate.get("url"))
        for candidate in candidates
        if candidate.get("liveness") not in {"active", "expired"}
    ][: max(0, min(max_checks, 100))]
    if not urls:
        return {
            "status": "ok",
            "checked": 0,
            "results": {},
            "elapsed_seconds": 0,
            "error": None,
        }
    parsed: dict[str, str] = {}
    details: dict[str, dict[str, str]] = {}
    checker_sources: dict[str, str] = {}
    elapsed_seconds = 0.0
    errors: list[str] = []

    if SOURCE_LIVENESS_SCRIPT.is_file():
        source_result = _run(
            [str(runtime["node_bin"]), str(SOURCE_LIVENESS_SCRIPT), *urls],
            cwd=STAGING_ROOT,
            timeout=180,
            env=runtime_subprocess_env(runtime),
        )
        elapsed_seconds += float(source_result.get("elapsed_seconds") or 0)
        try:
            source_payload = _parse_last_json(source_result.get("stdout") or "")
            source_mapping = source_payload.get("results")
            if isinstance(source_mapping, dict):
                for url, value in source_mapping.items():
                    if url in urls and value in {"active", "expired", "uncertain"}:
                        parsed[url] = value
                        checker_sources[url] = "career-ops-v2-structured-source"
            source_details = source_payload.get("details")
            if isinstance(source_details, dict):
                details.update(
                    {
                        url: {
                            "source": _clean(value.get("source"), 100),
                            "code": _clean(value.get("code"), 120),
                        }
                        for url, value in source_details.items()
                        if url in parsed and isinstance(value, dict)
                    }
                )
        except Exception as exc:  # noqa: BLE001 - fallback handles every unresolved URL
            errors.append(_clean(f"structured source check: {type(exc).__name__}: {exc}", 240))

    unresolved = [url for url in urls if url not in parsed]
    if unresolved:
        script = Path(runtime["production_project_root"]) / "check-liveness.mjs"
        result = _run(
            [str(runtime["node_bin"]), str(script), "--no-fallback", *unresolved],
            cwd=Path(runtime["production_project_root"]),
            timeout=300,
            env=runtime_subprocess_env(runtime),
        )
        elapsed_seconds += float(result.get("elapsed_seconds") or 0)
        legacy_results = parse_liveness_output(result.get("stdout") or "")
        for url, value in legacy_results.items():
            if url in unresolved:
                parsed[url] = value
                checker_sources[url] = "career-ops-check-liveness"
                details[url] = {"source": classify_job_source(url)["source_id"], "code": "legacy-check"}
        if not legacy_results:
            errors.append(_command_error(result, "liveness check produced no results"))

    source_results: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        url = str(candidate.get("url") or "")
        value = parsed.get(url)
        if value not in {"active", "expired", "uncertain"}:
            continue
        source_id = _clean(candidate.get("source_id"), 100) or classify_job_source(url)["source_id"]
        counts = source_results.setdefault(
            source_id,
            {"checked": 0, "active": 0, "expired": 0, "uncertain": 0},
        )
        counts["checked"] += 1
        counts[value] += 1

    status = "ok" if len(parsed) == len(urls) else "partial"
    return {
        "status": status,
        "checked": len(parsed),
        "requested": len(urls),
        "results": parsed,
        "details": details,
        "checker_sources": checker_sources,
        "source_results": source_results,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "error": "; ".join(errors) if len(parsed) < len(urls) and errors else None,
    }


def _actionability(candidate: dict[str, Any]) -> dict[str, Any]:
    """Rank evidence strength without claiming that an application was submitted."""
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("company", "title", "description")
    ).casefold()
    is_search_firm = any(term.casefold() in haystack for term in SEARCH_FIRM_TERMS)
    evaluation_score = float(candidate.get("evaluation_score") or 0)
    evidence = str(candidate.get("liveness_evidence") or "")

    score = 0
    reasons: list[str] = []
    if evaluation_score:
        score = max(score, 600 + round(evaluation_score * 50))
        reasons.append("prior_scored_evaluation")
    if candidate.get("direct_verified") and candidate.get("liveness") == "active":
        score = max(score, 900)
        reasons.append("linkedin_direct_public")
    elif (
        candidate.get("recommendation_eligible")
        and candidate.get("candidate_origin") == "linkedin_pending_recheck"
    ):
        score = max(score, 750)
        reasons.append("linkedin_public_liveness_recheck")
    elif candidate.get("recommendation_eligible") and evidence and evidence != "legacy-check":
        score = max(score, 600)
        reasons.append("structured_source_active")
    elif candidate.get("recommendation_eligible"):
        score = max(score, 250)
        reasons.append("page_liveness_only")

    if is_search_firm:
        score -= 600
        reasons.append("employer_identity_requires_confirmation")
    if candidate.get("profile_review_required"):
        score -= 150
        reasons.append("seniority_requires_human_review")
    else:
        score += max(0, int(candidate.get("profile_fit_score") or 0)) * 25

    if score >= 800 and not is_search_firm and not candidate.get("profile_review_required"):
        tier = "high"
    elif score >= 450 and not is_search_firm and not candidate.get("profile_review_required"):
        tier = "standard"
    else:
        tier = "conditional"
    return {
        "actionability": tier,
        "actionability_score": score,
        "actionability_reasons": reasons,
        "search_firm_or_hidden_employer": is_search_firm,
    }


def apply_liveness_results(
    candidates: list[dict[str, Any]],
    liveness: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    mapping = liveness.get("results") if isinstance(liveness.get("results"), dict) else {}
    checker_sources = (
        liveness.get("checker_sources")
        if isinstance(liveness.get("checker_sources"), dict)
        else {}
    )
    details = liveness.get("details") if isinstance(liveness.get("details"), dict) else {}
    updated: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        mapped_result = mapping.get(item.get("url"))
        if mapped_result:
            item["liveness"] = mapped_result
            item["liveness_checked"] = True
            item["liveness_source"] = checker_sources.get(
                item.get("url"), "career-ops-check-liveness"
            )
            detail = details.get(item.get("url"))
            if isinstance(detail, dict) and detail.get("code"):
                item["liveness_evidence"] = _clean(detail.get("code"), 120)
        elif item.get("direct_verified") and item.get("liveness") == "active":
            item["liveness_checked"] = True
            item["liveness_source"] = item.get("liveness_source") or "linkedin-direct-public"
        else:
            item["liveness_checked"] = False
        if item.get("liveness") == "expired":
            continue
        history_eligible = item.get("history_gate", "eligible") == "eligible"
        if (
            history_eligible
            and item.get("direct_verified")
            and item.get("liveness") == "active"
        ):
            item["verification_method"] = "linkedin-direct-public"
            item["recommendation_eligible"] = True
        elif history_eligible and mapped_result == "active":
            item["verification_method"] = (
                "linkedin-public-liveness-recheck"
                if item.get("candidate_origin") == "linkedin_pending_recheck"
                else "source-liveness-check"
            )
            item["recommendation_eligible"] = True
        else:
            item["verification_method"] = "not-verified"
            item["recommendation_eligible"] = False
        if item.get("recommendation_eligible") and item.get("recommendation_cooldown"):
            item["recommendation_eligible"] = False
            item["verification_method"] = "active-recommendation-cooldown"
        item.update(_actionability(item))
        updated.append(item)
    updated.sort(
        key=lambda item: (
            0 if item.get("recommendation_eligible") else 1,
            0 if not item.get("recommendation_cooldown") else 1,
            0 if int(item.get("freshness_ordinal") or 0) else 1,
            -int(item.get("freshness_ordinal") or 0),
            -int(item.get("profile_fit_score") or 0),
            -int(item.get("actionability_score") or 0),
            -float(item.get("evaluation_score") or 0),
            0 if item.get("direct_verified") and item.get("liveness") == "active" else 1,
            0 if item.get("liveness") == "active" else 1,
            -int(item.get("priority_score") or 0),
            str(item.get("company") or "").casefold(),
        )
    )
    # Source diversity remains useful when building the larger pre-check pool,
    # but the final bounded list must preserve evidence and actionability order.
    return updated[: max(1, min(limit, 5))]


def _source_round_robin(
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    source_order: list[str] = []
    source_buckets: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        source_id = str(candidate.get("source_id") or "unknown")
        if source_id not in source_buckets:
            source_order.append(source_id)
            source_buckets[source_id] = []
        source_buckets[source_id].append(candidate)
    depth = 0
    while len(chosen) < limit:
        added = False
        for source_id in source_order:
            bucket = source_buckets[source_id]
            if depth >= len(bucket):
                continue
            chosen.append(bucket[depth])
            added = True
            if len(chosen) >= limit:
                break
        if not added:
            break
        depth += 1
    return chosen


def _processed_location(notes: str) -> str:
    """Extract an explicitly labelled location without a personal region list."""
    match = re.search(
        r"(?:location|위치|지역)\s*[:=]\s*([^|;\n]+)",
        notes,
        re.IGNORECASE,
    )
    return _clean(match.group(1), 100) if match else ""


def _parse_active_processed_candidate(
    line: str,
    *,
    pipeline_order: int,
) -> dict[str, Any] | None:
    """Recover a previously scored candidate only from explicit current evidence.

    Historical Processed rows contain many skips, applications, rejections, and
    expired postings.  Re-entry therefore requires every positive signal below;
    a score or an old report by itself is intentionally insufficient.
    """
    row = line[len("- [x] ") :]
    parts = [part.strip() for part in row.split("|")]
    if len(parts) < 7:
        return None
    evaluation_id = re.fullmatch(r"#(\d+)", parts[0])
    score_match = re.fullmatch(r"(\d+(?:\.\d+)?)/5", parts[4], re.IGNORECASE)
    if not evaluation_id or not score_match:
        return None

    notes = _clean(" | ".join(parts[6:]), 1000)
    normalized_notes = notes.casefold()
    if "clean net-new" not in normalized_notes or not re.search(
        r"\bactive\b", normalized_notes
    ):
        return None
    if any(term.casefold() in normalized_notes for term in PROCESSED_INACTIVE_TERMS):
        return None

    url = _clean(parts[1], 1200)
    company = _clean(parts[2], 200)
    title = _clean(parts[3], 300)
    location = _processed_location(notes)
    if not url or not company or not title or not location:
        return None
    source = classify_job_source(url)
    return {
        "url": url,
        "company": company,
        "title": title,
        "location": location,
        "source": f"pipeline:{source['source_id']}",
        **source,
        "description": notes,
        "direct_verified": False,
        "liveness": "not_checked",
        "pipeline_order": pipeline_order,
        "pipeline_bucket": "processed_active",
        "candidate_origin": "processed_active",
        "evaluation_id": evaluation_id.group(1),
        "evaluation_score": float(score_match.group(1)),
        "evaluation_score_text": parts[4],
    }


def _date_text(value: Any, *, today: date | None = None) -> str | None:
    text = _clean(value, 120)
    direct = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if direct:
        try:
            return date.fromisoformat(direct.group(1)).isoformat()
        except ValueError:
            return None
    reference = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    if text in {"오늘", "방금", "today", "just now"}:
        return reference.isoformat()
    korean_relative = re.fullmatch(r"(\d{1,3})\s*(분|시간|일|주)\s*전", text)
    if korean_relative:
        amount = int(korean_relative.group(1))
        unit = korean_relative.group(2)
        days = amount * 7 if unit == "주" else amount if unit == "일" else 0
        return (reference - timedelta(days=days)).isoformat()
    english_relative = re.fullmatch(
        r"(\d{1,3})\s*(?:minutes?|hours?|days?|weeks?)\s+ago",
        text,
        re.IGNORECASE,
    )
    if english_relative:
        amount = int(english_relative.group(1))
        days = amount * 7 if "week" in text.casefold() else amount if "day" in text.casefold() else 0
        return (reference - timedelta(days=days)).isoformat()
    return None


def load_scan_history_metadata(path: Path) -> dict[str, dict[str, str | None]]:
    """Load only posting dates needed for freshness; ignore personal row fields."""
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return {}
    header = [item.strip().casefold() for item in lines[0].split("\t")]
    indexes = {name: index for index, name in enumerate(header)}

    def field(parts: list[str], *names: str) -> str:
        for name in names:
            index = indexes.get(name)
            if index is not None and index < len(parts):
                return parts[index]
        return ""

    metadata: dict[str, dict[str, str | None]] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        url = _clean(field(parts, "url"), 1200)
        if not url:
            continue
        first_seen = _date_text(field(parts, "first_seen", "firstseen"))
        posted_at = _date_text(field(parts, "posted_at", "postedat", "posted"))
        current = metadata.setdefault(url, {"first_seen": None, "posted_date": None})
        if first_seen and (current["first_seen"] is None or first_seen < current["first_seen"]):
            current["first_seen"] = first_seen
        if posted_at and (current["posted_date"] is None or posted_at > current["posted_date"]):
            current["posted_date"] = posted_at
    return metadata


def annotate_candidate_freshness(
    candidate_groups: list[list[dict[str, Any]]],
    metadata: dict[str, dict[str, str | None]],
    *,
    today: date | None = None,
) -> dict[str, int]:
    report_date = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    stats = {"with_posted_date": 0, "with_first_seen": 0, "backlog": 0, "unknown": 0}
    for group in candidate_groups:
        for candidate in group:
            saved = metadata.get(str(candidate.get("url") or ""), {})
            description = str(candidate.get("description") or "")
            posted_marker = re.search(
                r"(?:^|\|)\s*posted\s*:\s*(20\d{2}-\d{2}-\d{2})\b",
                description,
                re.IGNORECASE,
            )
            posted_date = (
                _date_text(candidate.get("posting_date"), today=report_date)
                or _date_text(candidate.get("listed_at"), today=report_date)
                or _date_text(candidate.get("posted_at"), today=report_date)
                or (
                    _date_text(posted_marker.group(1), today=report_date)
                    if posted_marker
                    else None
                )
                or saved.get("posted_date")
            )
            first_seen = _date_text(candidate.get("first_seen"), today=report_date) or saved.get("first_seen")
            if posted_date:
                candidate["posting_date"] = posted_date
                stats["with_posted_date"] += 1
            if first_seen:
                candidate["first_seen"] = first_seen
                stats["with_first_seen"] += 1
            freshness = posted_date or first_seen
            candidate["_freshness_ordinal"] = date.fromisoformat(freshness).toordinal() if freshness else 0
            if candidate.get("candidate_origin") == "pending_new" and first_seen:
                if first_seen == report_date.isoformat():
                    candidate["candidate_origin"] = "pending_new"
                else:
                    candidate["candidate_origin"] = "pending_backlog"
                    stats["backlog"] += 1
            if not freshness:
                stats["unknown"] += 1
    return stats


def parse_pipeline(path: Path) -> dict[str, Any]:
    pending: list[dict[str, Any]] = []
    processed_candidates: list[dict[str, Any]] = []
    processed = 0
    if not path.is_file():
        return {
            "status": "unavailable",
            "pending": [],
            "processed_candidates": [],
            "processed_candidate_count": 0,
            "pending_count": 0,
            "processed_count": 0,
            "pending_bucket_counts": {},
            "source_count": 0,
            "source_inventory": [],
        }
    source_inventory: dict[str, dict[str, Any]] = {}
    pending_bucket_counts: dict[str, int] = {}
    current_section = ""
    current_pending_bucket = "unclassified"

    def record_source(url: str, bucket: str) -> dict[str, str]:
        source = classify_job_source(url)
        source_id = source["source_id"]
        inventory = source_inventory.setdefault(
            source_id,
            {
                "source_id": source_id,
                "source_label": source["source_label"],
                "hosts": set(),
                "pending_count": 0,
                "processed_count": 0,
            },
        )
        if source["source_host"]:
            inventory["hosts"].add(source["source_host"])
        inventory[f"{bucket}_count"] += 1
        return source

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            current_section = _clean(line[3:], 120).casefold()
            current_pending_bucket = "unclassified"
            continue
        if line.startswith("### ") and current_section == "pending":
            heading = re.sub(r"\s*\(\d+\)\s*$", "", _clean(line[4:], 120))
            current_pending_bucket = PIPELINE_BUCKET_LABELS.get(heading, "unclassified")
            continue
        if line.startswith("- [x] "):
            processed += 1
            url_match = re.search(r"https?://[^\s|)>\]]+", line, re.IGNORECASE)
            if url_match:
                record_source(url_match.group(0).rstrip(".,"), "processed")
            candidate = (
                _parse_active_processed_candidate(
                    line,
                    pipeline_order=len(processed_candidates),
                )
                if current_section.startswith("processed")
                else None
            )
            if candidate is not None:
                processed_candidates.append(candidate)
            continue
        if not line.startswith("- [ ] "):
            continue
        row = line[len("- [ ] ") :]
        parts = [part.strip() for part in row.split("|")]
        if parts and not parts[0]:
            parts = parts[1:]
        if len(parts) < 4:
            continue
        url = _clean(parts[0], 1200)
        source = record_source(url, "pending")
        description = _clean(" | ".join(parts[4:]), 800)
        candidate_origin = (
            "linkedin_pending_recheck"
            if source["source_id"] == "linkedin"
            and "linkedin public page directly verified" in description.casefold()
            else "pending_new"
        )
        pending_bucket_counts[current_pending_bucket] = (
            pending_bucket_counts.get(current_pending_bucket, 0) + 1
        )
        pending.append(
            {
                "url": url,
                "company": _clean(parts[1], 200),
                "title": _clean(parts[2], 300),
                "location": _clean(parts[3], 200),
                "source": f"pipeline:{source['source_id']}",
                **source,
                "description": description,
                "direct_verified": False,
                "liveness": "not_checked",
                "pipeline_order": len(pending),
                "pipeline_bucket": current_pending_bucket,
                "candidate_origin": candidate_origin,
            }
        )
    inventory_rows = [
        {
            **{key: value for key, value in item.items() if key != "hosts"},
            "hosts": sorted(item["hosts"]),
            "total_count": item["pending_count"] + item["processed_count"],
        }
        for item in source_inventory.values()
    ]
    inventory_rows.sort(
        key=lambda item: (
            -int(item["pending_count"]),
            -int(item["total_count"]),
            str(item["source_id"]),
        )
    )
    return {
        "status": "ok",
        "pending": pending,
        "processed_candidates": processed_candidates,
        "processed_candidate_count": len(processed_candidates),
        "pending_count": len(pending),
        "processed_count": processed,
        "pending_bucket_counts": pending_bucket_counts,
        "source_count": len(inventory_rows),
        "source_inventory": inventory_rows,
    }


def _history_pending_reason_is_reconcilable(
    candidate: dict[str, Any],
) -> bool:
    """Return true only for durable application-history exclusions.

    Conditional and different-role reapplications remain in Pending for later
    review. This writer is limited to explicit same-run application evidence or
    the same role with a canonical hard-block status, so location/title filters
    can never remove a row from the audit trail.
    """
    if str(candidate.get("pipeline_bucket") or "") == "conditional_reapply":
        return False
    identity = candidate.get("_identity_decision")
    if not isinstance(identity, dict) or identity.get("history_gate") != "excluded":
        return False
    reason = str(identity.get("history_reason") or "")
    if reason in HISTORY_PENDING_RECONCILE_REASONS:
        return True
    if not reason.startswith("same_role_"):
        return False
    return reason.removeprefix("same_role_") in HISTORY_HARD_BLOCK_STATUSES


def reconcile_history_excluded_pending(
    path: Path,
    candidates: list[dict[str, Any]],
    *,
    mode: str,
) -> dict[str, Any]:
    """Move history-blocked Pending URLs to Processed with bounded evidence.

    The original file is preserved byte-for-byte except for removing matched
    Pending lines and inserting their Processed audit rows.  Apply mode uses an
    atomic same-directory replacement; dry-run reports intent without writing.
    """
    result: dict[str, Any] = {
        "status": "no_changes",
        "mode": mode,
        "candidate_count": 0,
        "would_move_count": 0,
        "moved_count": 0,
        "moved_urls": [],
        "by_reason": {},
        "error": None,
    }
    if not path.is_file():
        return {**result, "status": "unavailable", "error": f"Pipeline not found: {path}"}

    targets: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _history_pending_reason_is_reconcilable(candidate):
            continue
        url = _clean(candidate.get("url"), 1200)
        if url:
            targets.setdefault(url, candidate)
    result["candidate_count"] = len(targets)
    if not targets:
        return result

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    current_section = ""
    kept: list[str] = []
    moved: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            current_section = _clean(line[3:], 120).casefold()
        if current_section != "pending" or not line.startswith("- [ ] "):
            kept.append(line)
            continue
        raw_parts = [part.strip() for part in line[len("- [ ] ") :].split("|")]
        if len(raw_parts) < 4:
            kept.append(line)
            continue
        url = _clean(raw_parts[0], 1200)
        candidate = targets.get(url)
        if candidate is None:
            kept.append(line)
            continue
        identity = candidate.get("_identity_decision") or {}
        tracker_matches = identity.get("history_tracker_matches") or []
        tracker_match = next(
            (item for item in tracker_matches if isinstance(item, dict)),
            {},
        )
        reason = _clean(identity.get("history_reason"), 80)
        moved.append(
            {
                "url": url,
                "company": raw_parts[1],
                "title": raw_parts[2],
                "tracker_id": _clean(tracker_match.get("id"), 20),
                "tracker_status": _clean(tracker_match.get("status"), 40),
                "reason": reason,
            }
        )

    result["would_move_count"] = len(moved)
    result["moved_urls"] = [item["url"] for item in moved]
    result["items"] = moved[:20]
    for item in moved:
        reason = item["reason"] or "unknown"
        result["by_reason"][reason] = int(result["by_reason"].get(reason) or 0) + 1
    if not moved:
        return result
    if mode != "apply":
        result["status"] = "dry_run_changes_detected"
        return result

    processed_index = next(
        (index for index, line in enumerate(kept) if line.startswith("## Processed")),
        None,
    )
    if processed_index is None:
        return {**result, "status": "error", "error": "Processed section not found"}
    insert_at = processed_index + 1
    while insert_at < len(kept) and not kept[insert_at].strip():
        insert_at += 1
    processed_lines = []
    for item in moved:
        tracker_ref = f"#{item['tracker_id']}" if item["tracker_id"] else "#--"
        status = item["tracker_status"] or "Excluded"
        processed_lines.append(
            f"- [x] {tracker_ref} | {item['url']} | {item['company']} | "
            f"{item['title']} | {status} | 지원 이력 자동 제외: {item['reason']}\n"
        )
    updated_lines = [*kept[:insert_at], *processed_lines, *kept[insert_at:]]
    updated = "".join(updated_lines)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, path.stat().st_mode)
        os.replace(temp_path, path)
    except Exception as exc:  # noqa: BLE001 - preserve structured cron failure
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return {
            **result,
            "status": "error",
            "error": _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS),
        }
    result["status"] = "applied"
    result["moved_count"] = len(moved)
    return result


def parse_application_tracker(path: Path) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    if not path.is_file():
        return {"status": "unavailable", "count": 0, "entries": []}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        entries.append(
            {
                "id": _clean(parts[0], 20),
                "date": _clean(parts[1], 40),
                "company": _clean(parts[2], 200),
                "role": _clean(parts[3], 300),
                "status": _clean(parts[5], 40),
                "notes": _clean(parts[8], 500),
            }
        )
    return {"status": "ok", "count": len(entries), "entries": entries}


def run_identity_resolver(
    runtime: dict[str, Any],
    tracker: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Resolve all mail and job identities in one deterministic Node batch."""
    project_root = Path(runtime["production_project_root"])
    script_setting = runtime.get("identity_resolver_script")
    script = (
        Path(str(script_setting))
        if script_setting
        else project_root / IDENTITY_RESOLVER_FILENAME
    )
    if not script.is_absolute():
        script = project_root / script
    node_bin = Path(str(runtime["node_bin"]))
    if not script.is_file() or not node_bin.is_file():
        return {
            "status": "unavailable",
            "elapsed_seconds": 0,
            "record_count": len(records),
            "results": {},
            "error": f"Identity resolver unavailable: {script}",
        }
    payload = {
        "portals_path": str(project_root / "portals.yml"),
        "tracker": tracker.get("entries") or [],
        "records": records,
    }
    started = time.monotonic()
    try:
        completed = runner(
            [str(node_bin), str(script)],
            cwd=project_root,
            env=runtime_subprocess_env(runtime),
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=int(runtime.get("identity_resolver_timeout_seconds") or 10),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "record_count": len(records),
            "results": {},
            "error": "Identity resolver timed out",
        }
    elapsed = round(time.monotonic() - started, 3)
    parsed = _parse_json_stdout(completed.stdout or "")
    raw_results = parsed.get("results") if isinstance(parsed.get("results"), list) else []
    mapping = {
        str(item.get("id")): item
        for item in raw_results
        if isinstance(item, dict) and item.get("id")
    }
    if completed.returncode != 0 or len(mapping) != len(records):
        return {
            "status": "error",
            "elapsed_seconds": elapsed,
            "record_count": len(records),
            "resolved_count": len(mapping),
            "results": mapping,
            "error": _clean(
                completed.stderr
                or completed.stdout
                or f"Identity resolver returned {len(mapping)}/{len(records)} results",
                MAX_ERROR_CHARS,
            ),
        }
    return {
        "status": "ok",
        "elapsed_seconds": elapsed,
        "record_count": len(records),
        "resolved_count": len(mapping),
        "results": mapping,
        "error": None,
    }


def annotate_identity_records(
    runtime: dict[str, Any],
    tracker: dict[str, Any],
    mail_payload: dict[str, Any],
    candidate_groups: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Attach one-batch JS identity decisions to mail messages and candidates."""
    records: list[dict[str, Any]] = []
    targets: list[tuple[dict[str, Any], str, str]] = []
    audit = _mail_audit_object(mail_payload)
    for index, message in enumerate(audit.get("messages") or []):
        if not isinstance(message, dict):
            continue
        company, role = _mail_identity(message)
        record_id = f"mail:{index}"
        records.append(
            {
                "id": record_id,
                "kind": "mail",
                "company": company,
                "role": role,
                "identity_text": " ".join(
                    str(message.get(key) or "")
                    for key in ("from", "subject", "snippet", "decision_text")
                ),
            }
        )
        targets.append((message, record_id, "mail"))

    seen_objects: set[int] = set()
    candidate_index = 0
    for group in candidate_groups:
        for candidate in group:
            if not isinstance(candidate, dict) or id(candidate) in seen_objects:
                continue
            seen_objects.add(id(candidate))
            record_id = f"candidate:{candidate_index}"
            candidate_index += 1
            records.append(
                {
                    "id": record_id,
                    "kind": "candidate",
                    "company": candidate.get("company"),
                    "role": candidate.get("title"),
                    "location": candidate.get("location"),
                    "url": candidate.get("url"),
                    "source_id": candidate.get("source_id"),
                    "pipeline_bucket": candidate.get("pipeline_bucket"),
                }
            )
            targets.append((candidate, record_id, "candidate"))

    result = run_identity_resolver(runtime, tracker, records)
    mapping = result.get("results") if isinstance(result.get("results"), dict) else {}
    for target, record_id, kind in targets:
        decision = mapping.get(record_id)
        if isinstance(decision, dict):
            target["_identity_decision"] = decision
        elif kind == "candidate":
            target["_identity_decision"] = {
                "location_eligible": False,
                "history_gate": "excluded",
                "history_reason": "identity_resolver_unavailable",
                "history_tracker_matches": [],
            }
    return {key: value for key, value in result.items() if key != "results"}


def apply_same_run_mail_history_blocks(
    mail_payload: dict[str, Any],
    candidate_groups: list[list[dict[str, Any]]],
) -> None:
    """Block candidates using JS keys from same-run mail-backed applications."""
    audit = _mail_audit_object(mail_payload)
    company_keys: set[str] = set()
    for message in audit.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("reconciliation_action") not in {"add", "update", "no_change"}:
            continue
        identity = message.get("_identity_decision")
        if not isinstance(identity, dict):
            continue
        company_key = _clean(identity.get("company_key"), 200)
        if company_key:
            company_keys.add(company_key)
    if not company_keys:
        return
    for group in candidate_groups:
        for candidate in group:
            if not isinstance(candidate, dict):
                continue
            identity = candidate.get("_identity_decision")
            if not isinstance(identity, dict):
                continue
            if (
                identity.get("history_gate") == "eligible"
                and _clean(identity.get("company_key"), 200) in company_keys
            ):
                identity["history_gate"] = "excluded"
                identity["history_reason"] = "same_run_mail_application"
                identity["history_tracker_matches"] = []


ACTIVE_APPLICATION_STATUSES = {"applied", "responded", "interview", "offer"}
NO_RESPONSE_CLOSE_MARKER = "무응답 자동 종료"


def build_application_status(
    tracker: dict[str, Any], *, max_active: int = 30
) -> dict[str, Any]:
    """Build a bounded, deterministic view of the canonical application tracker.

    The Slack root uses aggregate results plus only the newest few rows.  The
    first thread reply can show the full bounded active list without loading the
    entire Markdown tracker into an agent prompt.
    """
    raw_entries = tracker.get("entries") if isinstance(tracker, dict) else []
    entries = [item for item in (raw_entries or []) if isinstance(item, dict)]
    counts = Counter(_clean(item.get("status"), 40) or "Unknown" for item in entries)
    no_response_closed_count = sum(
        1
        for item in entries
        if str(item.get("status") or "").casefold() == "discarded"
        and NO_RESPONSE_CLOSE_MARKER in str(item.get("notes") or "")
    )
    excluded_count = int(counts.get("Discarded", 0)) + int(counts.get("SKIP", 0))
    active = [
        {
            "id": _clean(item.get("id"), 20),
            "date": _clean(item.get("date"), 40),
            "company": _clean(item.get("company"), 160),
            "role": _clean(item.get("role"), 220),
            "status": _clean(item.get("status"), 40),
        }
        for item in entries
        if str(item.get("status") or "").casefold() in ACTIVE_APPLICATION_STATUSES
    ]
    active.sort(
        key=lambda item: (
            item["date"],
            int(item["id"]) if item["id"].isdigit() else -1,
        ),
        reverse=True,
    )
    bounded_limit = max(0, min(int(max_active), 50))
    return {
        "status": tracker.get("status", "unavailable"),
        "total_count": len(entries),
        "status_counts": dict(sorted(counts.items())),
        "active_count": len(active),
        "active": active[:bounded_limit],
        "active_truncated": len(active) > bounded_limit,
        "no_response_closed_count": no_response_closed_count,
        "other_excluded_count": max(0, excluded_count - no_response_closed_count),
    }


def _match_key(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\(주\)|주식회사|유한회사|㈜|co\.?\s*,?\s*ltd\.?|inc\.?", "", text)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _company_match_keys(company: str) -> list[str]:
    variants = [company]
    variants.extend(re.findall(r"\(([^)]+)\)", company))
    variants.extend(re.split(r"[/·]", company))
    without_parentheses = re.sub(r"\([^)]+\)", "", company)
    variants.append(without_parentheses)
    keys: list[str] = []
    for variant in variants:
        key = _match_key(variant)
        if len(key) >= 2 and key not in keys:
            keys.append(key)
    return keys


def _companies_match(left: Any, right: Any) -> bool:
    """Match the same named entity without treating group substrings as identity."""
    left_keys = set(_company_match_keys(str(left or "")))
    right_keys = set(_company_match_keys(str(right or "")))
    return bool(left_keys & right_keys)


def _roles_match(left: Any, right: Any) -> bool:
    left_key = _match_key(left)
    right_key = _match_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.72


def _history_decision(
    candidate: dict[str, Any],
    tracker: dict[str, Any] | None,
) -> dict[str, Any]:
    bucket = str(candidate.get("pipeline_bucket") or "")
    if bucket == "conditional_reapply":
        return {
            "history_gate": "excluded",
            "history_reason": "pipeline_conditional_reapply",
            "history_tracker_matches": [],
        }

    entries = (
        tracker.get("entries")
        if isinstance(tracker, dict) and isinstance(tracker.get("entries"), list)
        else []
    )
    company_matches = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and _companies_match(candidate.get("company"), entry.get("company"))
    ]
    same_role = [
        entry
        for entry in company_matches
        if _roles_match(candidate.get("title"), entry.get("role"))
    ]

    for entry in same_role:
        status = str(entry.get("status") or "").casefold()
        if status in HISTORY_HARD_BLOCK_STATUSES:
            return {
                "history_gate": "excluded",
                "history_reason": f"same_role_{status}",
                "history_tracker_matches": [
                    {
                        "id": _clean(entry.get("id"), 20),
                        "company": _clean(entry.get("company"), 200),
                        "role": _clean(entry.get("role"), 300),
                        "status": _clean(entry.get("status"), 40),
                    }
                ],
            }

    prior_company_history = [
        entry
        for entry in company_matches
        if str(entry.get("status") or "").casefold() in HISTORY_COMPANY_BLOCK_STATUSES
    ]
    if prior_company_history:
        return {
            "history_gate": "excluded",
            "history_reason": "same_company_prior_application",
            "history_tracker_matches": [
                {
                    "id": _clean(entry.get("id"), 20),
                    "company": _clean(entry.get("company"), 200),
                    "role": _clean(entry.get("role"), 300),
                    "status": _clean(entry.get("status"), 40),
                }
                for entry in prior_company_history[:3]
            ],
        }

    return {
        "history_gate": "eligible",
        "history_reason": "no_blocking_history",
        "history_tracker_matches": [],
    }


def _record_history_gate(
    stats: dict[str, Any] | None,
    candidate: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    if stats is None:
        return
    stats["checked"] = int(stats.get("checked") or 0) + 1
    if decision.get("history_gate") == "eligible":
        stats["eligible"] = int(stats.get("eligible") or 0) + 1
        return
    stats["excluded"] = int(stats.get("excluded") or 0) + 1
    reason = str(decision.get("history_reason") or "unknown")
    by_reason = stats.setdefault("by_reason", {})
    by_reason[reason] = int(by_reason.get(reason) or 0) + 1
    examples = stats.setdefault("examples", [])
    if len(examples) < 5:
        examples.append(
            {
                "company": _clean(candidate.get("company"), 160),
                "title": _clean(candidate.get("title"), 220),
                "reason": reason,
                "tracker_ids": [
                    _clean(item.get("id"), 20)
                    for item in (decision.get("history_tracker_matches") or [])[:3]
                    if isinstance(item, dict)
                ],
            }
        )


def _normalize_mail_text(value: Any) -> str:
    """Normalize mail evidence without broadening it into semantic inference."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    text = re.sub(r"[\s\u00a0]+", " ", text)
    return text.strip()


def _mail_search_text(message: dict[str, Any]) -> str:
    # decision_text is a bounded body excerpt produced by the read-only Gmail
    # audit. It is used at decision time but is never copied into the compact
    # payload or the persistent review queue.
    return _normalize_mail_text(
        " ".join(
            str(message.get(key) or "")
            for key in ("subject", "snippet", "decision_text", "body")
        )
    )


def _mail_evidenced_status(message: dict[str, Any]) -> str | None:
    text = _mail_search_text(message)
    subject_text = _normalize_mail_text(message.get("subject"))
    if re.search(r"에서\s*메시지를\s*보냈습니다", subject_text) and not re.search(
        r"지원|합격|불\s*합격|탈락|일정.{0,8}(?:확정|요청)", text
    ):
        # Generic recruiter/chat notifications can describe a hypothetical
        # interview process without proving that an application or interview
        # exists for this candidate.
        return None
    recruiting_context = re.search(
        r"지원|채용|포지션|직무|전형|서류|면접|인터뷰|application|candidate|position|role",
        text,
    )
    rejected_patterns = (
        r"불\s*합격",
        r"(?:이번|귀하|지원하신|지원했던|전형\s*결과|서류\s*결과).{0,45}탈락|탈락.{0,35}(?:이후|다음)\s*(?:단계|전형)",
        r"(?:해당\s*)?(?:포지션|직무|채용|전형).{0,30}탈락(?!률)|탈락(?!률)\s*(?:결과|안내|통보)",
        r"아쉬운\s*결과",
        r"합격.{0,18}못",
        r"(?:지원자|귀하|함께|모시).{0,20}못",
        r"합격\s*소식.{0,35}(?:전해|전하|전달).{0,12}못",
        r"(?:이후|다음)\s*(?:채용\s*)?(?:단계|전형).{0,30}(?:진행|함께).{0,12}(?:어렵(?!지\s*않)|불가)",
        r"(?:함께|모시).{0,20}(?:어렵|할\s*수\s*없)",
        r"(?:서류\s*)?검토\s*기간(?:이|은|가)?\s*만료(?!\s*(?:전|되지))",
        r"전형(?:이|은|을)?\s*(?:종료|마감)",
        r"(?:will|would|have|has)?\s*not\s+(?:be\s+)?mov(?:e|ing)\s+forward",
        r"decided\s+not\s+to\s+proceed",
        r"(?:pursue|pursuing|move forward with)\s+other\s+candidates",
        r"application\s+(?:was|is)\s+unsuccessful",
    )
    if recruiting_context and any(re.search(pattern, text) for pattern in rejected_patterns):
        return "Rejected"
    offer_terms = (
        "최종 합격",
        "처우 제안",
        "오퍼 레터",
        "offer letter",
        "employment offer",
    )
    if any(term in text for term in offer_terms):
        return "Offer"
    interview_terms = (
        "면접 일정",
        "면접 안내",
        "인터뷰 일정",
        "인터뷰 안내",
        "인터뷰 요청",
        "interview invitation",
        "schedule your interview",
        "assessment invitation",
    )
    interview_pattern = re.search(
        r"(?:면접|인터뷰).{0,25}(?:일정|리마인드|알림|안내|요청|확정|시작)"
        r"|서류\s*전형?\s*합격",
        text,
    )
    if any(term in text for term in interview_terms) or interview_pattern:
        return "Interview"
    responded_terms = (
        "결과 안내가 지연",
        "지원 결과가 지연",
        "채용 절차가 지연",
        "서류 검토 중",
        "전형을 진행 중",
        "검토가 진행 중",
        "application is under review",
        "still reviewing your application",
        "update on your application",
        "application status update",
    )
    if any(term in text for term in responded_terms):
        return "Responded"
    applied_terms = (
        "지원이 완료",
        "지원이 정상적으로 완료",
        "지원서 접수 완료",
        "입사지원서 접수",
        "지원 완료",
        "thank you for applying",
        "received your application",
        "application received",
    )
    applied_pattern = re.search(
        r"(?:입사)?지원서(?:가|는)?\s*(?:정상(?:적으로)?\s*)?접수",
        text,
    )
    outbound_application_reply = (
        re.search(
            r"(?:지원하고자|지원(?:한다는|하겠다는)\s*(?:의사|뜻)|지원\s*(?:희망|의사))",
            text,
        )
        and re.search(
            r"(?:이력서|resume|cv).{0,60}"
            r"(?:첨부(?:드립니다|했습니다|합니다)|제출(?:드립니다|했습니다|합니다)|송부(?:드립니다|했습니다|합니다))",
            text,
        )
    )
    if any(term in text for term in applied_terms) or applied_pattern or outbound_application_reply:
        return "Applied"
    return None


def _mail_identity(message: dict[str, Any]) -> tuple[str, str]:
    subject = _clean(message.get("subject"), 500)
    snippet = _clean(message.get("snippet"), 1000)
    decision_text = _clean(message.get("decision_text") or message.get("body"), 3000)
    combined = f"{subject} {snippet} {decision_text}"

    normalized_subject = unicodedata.normalize("NFKC", subject)
    normalized_subject = re.sub(
        r"^(?:(?:fwd?|re)\s*:\s*)+",
        "",
        normalized_subject,
        flags=re.IGNORECASE,
    ).strip()

    # Greeting-based ATS receipts often put only a portal or careers brand in
    # the subject and the real company/role in the short body evidence. Parse
    # those bounded lines before the broader fallback regex so text from the
    # subject cannot be accidentally joined into the role name.
    subject_bracket = re.match(r"^\[([^]]+)\]", normalized_subject)
    subject_company = _clean(subject_bracket.group(1), 200) if subject_bracket else ""
    subject_company = re.sub(
        r"\s+(?:careers|채용)$", "", subject_company, flags=re.IGNORECASE
    ).strip()
    for receipt_text in (snippet, decision_text):
        normalized_receipt = unicodedata.normalize("NFKC", receipt_text).strip()
        bracket_receipt = re.match(
            r"^\[([^]]+)\]\s*(.+?)\s+지원(?:이|서가)?\s+(?:정상적으로\s+)?완료",
            normalized_receipt,
            re.IGNORECASE,
        )
        if bracket_receipt:
            return (
                _clean(bracket_receipt.group(1), 200),
                _clean(bracket_receipt.group(2), 300),
            )
        if subject_company:
            company_receipt = re.match(
                rf"^{re.escape(subject_company)}\s+(.+?)\s+지원(?:이|서가)?\s+(?:정상적으로\s+)?완료",
                normalized_receipt,
                re.IGNORECASE,
            )
            if company_receipt:
                return subject_company, _clean(company_receipt.group(1), 300)

    forwarded = re.match(
        r"^\[지원\s*안내\]\s*(.+?)\s*[_｜|]\s*(.+?)\s*(?:\([^)]*\))?\s*$",
        normalized_subject,
        re.IGNORECASE,
    )
    if forwarded:
        return _clean(forwarded.group(1), 200), _clean(forwarded.group(2), 300)

    remember_subject = re.match(
        r"^지원하신\s+(.+?)\s+(.+?)의\s+(?:서류|지원|채용|전형)",
        normalized_subject,
        re.IGNORECASE,
    )
    if remember_subject:
        return (
            _clean(remember_subject.group(1), 200),
            _clean(remember_subject.group(2), 300),
        )

    wanted_subject = re.sub(r"^\[원티드\]\s*[^!]+!\s*", "", normalized_subject)
    wanted = re.search(
        r"^(.+?)의\s+(.+?)에\s+지원이\s+(?:정상적으로\s+)?완료",
        wanted_subject,
        re.IGNORECASE,
    )
    if wanted:
        company = re.sub(r"^\[[^]]+\]\s*", "", wanted.group(1)).strip()
        return _clean(company, 200), _clean(wanted.group(2), 300)

    if subject_company:
        for evidence_text in (decision_text, snippet):
            normalized_evidence = unicodedata.normalize("NFKC", evidence_text).strip()
            role_evidence = re.match(
                rf"^{re.escape(subject_company)}(?:의)?\s+(.+?)\s+"
                r"(?:지원(?:서|의|은|을|이)?|인터뷰|채용)",
                normalized_evidence,
                re.IGNORECASE,
            )
            if role_evidence:
                return subject_company, _clean(role_evidence.group(1), 300)

    remember = re.search(
        r"기업명\s+(.+?)\s+지원한 공고\s+(.+?)(?:\s+-\s+서류|\s+전형 상태|\s+(?:[가-힣]{2,10}|[A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20})?)\s*님(?:\s|,|$)|\s+안녕하세요|$)",
        snippet,
        re.IGNORECASE,
    )
    if remember:
        return _clean(remember.group(1), 200), _clean(remember.group(2), 300)

    company = ""
    bracket = re.match(r"^\[([^]]+)\]", normalized_subject)
    if bracket and bracket.group(1) not in {"원티드", "리멤버", "안내"}:
        company = _clean(bracket.group(1), 200)
    applying_to = re.search(
        r"applying to\s+(.+?)(?:$|\s*[-|])", normalized_subject, re.IGNORECASE
    )
    if applying_to:
        company = _clean(applying_to.group(1), 200)

    role = ""
    greeting_role = re.search(
        r"(?:[가-힣]{2,10}|[A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20})?)\s*님의?\s+(.+?)\s+지원이\s+(?:정상적으로\s+)?완료",
        f"{snippet} {decision_text}",
        re.IGNORECASE,
    )
    if greeting_role:
        role = _clean(greeting_role.group(1), 300)
    if not role:
        english_role = re.search(
            r"(?:application for|applying for|interest in)\s+(?:the\s+)?(.+?)(?:\s+position|\s+role|\s+at\s+|[.!]|$)",
            combined,
            re.IGNORECASE,
        )
        if english_role:
            role = _clean(english_role.group(1), 300)

    body_position = re.search(
        r"채용\s*포지션\s*[:：]\s*(.+?)(?:\n|\r|지원일|전형|결과|$)",
        decision_text,
        re.IGNORECASE,
    )
    if body_position:
        position = _clean(body_position.group(1), 500)
        split = re.split(r"\s+(?:[-–—|｜/])\s+", position, maxsplit=1)
        if len(split) == 2:
            company = company or _clean(split[0], 200)
            role = role or _clean(split[1], 300)
    return company, role


def _status_transition_allowed(current: Any, proposed: str) -> bool:
    current_status = str(current or "")
    if current_status.casefold() == proposed.casefold():
        return False
    if current_status.casefold() in {"rejected", "offer", "hired", "discarded"} and proposed in {
        "Applied",
        "Responded",
        "Interview",
    }:
        return False
    if current_status.casefold() in {"responded", "interview"} and proposed == "Applied":
        return False
    if current_status.casefold() == "interview" and proposed == "Responded":
        return False
    return True


def attach_tracker_matches(
    mail_payload: dict[str, Any],
    tracker: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(mail_payload)
    audit = (
        dict(mail_payload.get("mail_audit"))
        if isinstance(mail_payload.get("mail_audit"), dict)
        else enriched
    )
    messages = audit.get("messages") if isinstance(audit.get("messages"), list) else []
    entries = tracker.get("entries") if isinstance(tracker.get("entries"), list) else []
    enriched_messages: list[dict[str, Any]] = []
    action_candidates: list[dict[str, Any]] = []
    review_count = 0
    unchanged_count = 0
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        evidenced_status = _mail_evidenced_status(raw)
        evidence_company, evidence_role = _mail_identity(raw)
        identity = raw.get("_identity_decision") if isinstance(raw.get("_identity_decision"), dict) else None
        if identity is not None:
            matches = [
                {
                    "id": _clean(match.get("id"), 20),
                    "company": _clean(match.get("company"), 200),
                    "role": _clean(match.get("role"), 300),
                    "status": _clean(match.get("status"), 40),
                    "role_match": bool(match.get("role_match")),
                }
                for match in (identity.get("tracker_matches") or [])[:3]
                if isinstance(match, dict)
            ]
        else:
            # Emergency fallback keeps the previous conservative
            # behavior; the scheduled pipeline attaches JS decisions in one batch.
            message_key = _match_key(
                " ".join(str(raw.get(key) or "") for key in ("from", "subject", "snippet"))
            )
            ranked: list[tuple[int, dict[str, Any]]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                company_match = (
                    _companies_match(evidence_company, entry.get("company"))
                    if evidence_company
                    else any(
                        key in message_key
                        for key in _company_match_keys(str(entry.get("company") or ""))
                    )
                )
                if not company_match:
                    continue
                role_match = bool(evidence_role) and _roles_match(evidence_role, entry.get("role"))
                ranked.append((0 if role_match else 1, entry))
            ranked.sort(key=lambda pair: (pair[0], -int(str(pair[1].get("id") or "0"))))
            matches = [
                {
                    "id": _clean(entry.get("id"), 20),
                    "company": _clean(entry.get("company"), 200),
                    "role": _clean(entry.get("role"), 300),
                    "status": _clean(entry.get("status"), 40),
                    "role_match": rank == 0,
                }
                for rank, entry in ranked[:3]
            ]
        exact_role_matches = [match for match in matches if match["role_match"]]
        unique_match = exact_role_matches[0] if len(exact_role_matches) == 1 else None
        if unique_match is None and len(matches) == 1:
            unique_match = matches[0]

        action = "none"
        proposal: dict[str, Any] | None = None
        if evidenced_status and unique_match:
            if _status_transition_allowed(unique_match.get("status"), evidenced_status):
                action = "update"
                proposal = {
                    "action": action,
                    "tracker_id": unique_match["id"],
                    "date": _clean(raw.get("date"), 40),
                    "company": unique_match["company"],
                    "role": unique_match["role"],
                    "current_status": unique_match["status"],
                    "new_status": evidenced_status,
                    "evidence_subject": _clean(raw.get("subject"), 240),
                }
            else:
                action = "no_change"
                unchanged_count += 1
        elif evidenced_status and not matches and evidence_company and evidence_role:
            action = "add"
            proposal = {
                "action": action,
                "tracker_id": None,
                "date": _clean(raw.get("date"), 40),
                "company": evidence_company,
                "role": evidence_role,
                "current_status": None,
                "new_status": evidenced_status,
                "evidence_subject": _clean(raw.get("subject"), 240),
            }
        elif evidenced_status:
            action = "review"
            review_count += 1
        elif matches:
            # A recruiting message that can be tied to an existing application
            # must never disappear silently just because its wording is new.
            action = "review"
            review_count += 1

        item["evidenced_status"] = evidenced_status
        item["evidence_company"] = evidence_company or None
        item["evidence_role"] = evidence_role or None
        item["tracker_matches"] = matches
        item["reconciliation_action"] = action
        item["decision_object"] = {
            "classification": evidenced_status or "unclassified",
            "company": evidence_company or None,
            "role": evidence_role or None,
            "tracker_match_count": len(matches),
            "selected_tracker_id": unique_match.get("id") if unique_match else None,
            "action": action,
        }
        enriched_messages.append(item)
        if proposal:
            action_candidates.append(proposal)

    actions: list[dict[str, Any]] = []
    seen_actions: set[str] = set()
    for proposal in action_candidates:
        key = (
            f"tracker:{proposal['tracker_id']}"
            if proposal.get("tracker_id")
            else f"new:{_match_key(proposal.get('company'))}:{_match_key(proposal.get('role'))}"
        )
        if key in seen_actions:
            continue
        seen_actions.add(key)
        actions.append(proposal)

    audit["messages"] = enriched_messages
    audit["tracker_status"] = tracker.get("status")
    audit["tracker_count"] = tracker.get("count", 0)
    audit["reconciliation"] = {
        "action_count": len(actions),
        "update_count": sum(item["action"] == "update" for item in actions),
        "add_count": sum(item["action"] == "add" for item in actions),
        "unchanged_count": unchanged_count,
        "review_count": review_count,
        "actions": actions,
    }
    if isinstance(mail_payload.get("mail_audit"), dict):
        enriched["mail_audit"] = audit
        return enriched
    return audit


def _mail_message_key(message: dict[str, Any]) -> str:
    message_id = _clean(message.get("message_id"), 300)
    if message_id:
        return f"gmail:{message_id}"
    stable = "\n".join(
        _clean(message.get(key), 600)
        for key in ("date", "from", "subject")
    )
    return f"sha256:{hashlib.sha256(stable.encode('utf-8')).hexdigest()}"


def _parse_mail_timestamp(value: Any) -> datetime | None:
    text = _clean(value, 80).replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return parsed.astimezone(UTC)


def _load_mail_review_queue(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "career-ops-v2.mail-review.v1", "items": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("Mail review queue must be an object with an items array")
    return value


def update_mail_review_queue(
    mail_payload: dict[str, Any],
    *,
    queue_path: Path,
    mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist unresolved, tracker-related mail without storing full bodies.

    Open items linked to tracker rows do not expire automatically. Unmatched
    open items are archived after 90 days, while resolved/ignored metadata is
    retained for 180 days. Dry-run computes the same decision object but never
    writes the queue.
    """
    audit = _mail_audit_object(mail_payload)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    current_iso = current.isoformat(timespec="seconds")
    result: dict[str, Any] = {
        "status": "skipped",
        "mode": mode,
        "path": str(queue_path),
        "open_count": 0,
        "opened_count": 0,
        "resolved_count": 0,
        "archived_count": 0,
        "pruned_count": 0,
        "persisted": False,
        "open_items": [],
        "open_tracker_ids": [],
        "error": None,
    }
    if audit.get("status") != "ok" or (
        audit.get("source") or mail_payload.get("source")
    ) != "codex-gmail-readonly":
        result["status"] = "blocked_mail_audit"
        audit["review_queue"] = result
        return result

    try:
        queue = _load_mail_review_queue(queue_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error"] = _clean(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS)
        audit["review_queue"] = result
        return result

    original = json.dumps(queue, ensure_ascii=False, sort_keys=True)
    by_key = {
        str(item.get("message_key")): dict(item)
        for item in queue.get("items") or []
        if isinstance(item, dict) and item.get("message_key")
    }

    for message in audit.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_key = _mail_message_key(message)
        action = str(message.get("reconciliation_action") or "none")
        previous = by_key.get(message_key, {})
        matches = [
            {
                "id": _clean(match.get("id"), 20),
                "company": _clean(match.get("company"), 120),
                "role": _clean(match.get("role"), 160),
                "status": _clean(match.get("status"), 40),
            }
            for match in (message.get("tracker_matches") or [])[:3]
            if isinstance(match, dict) and _clean(match.get("id"), 20)
        ]
        if action == "review":
            opened = previous.get("state") != "open"
            item = {
                **previous,
                "message_key": message_key,
                "gmail_message_id": _clean(message.get("message_id"), 300) or None,
                "gmail_thread_id": _clean(message.get("thread_id"), 300) or None,
                "message_date": _clean(message.get("date"), 80) or None,
                "subject": _clean(message.get("subject"), 240),
                "state": "open",
                "reason": (
                    "unclassified_tracker_mail"
                    if not message.get("evidenced_status")
                    else "ambiguous_tracker_match"
                ),
                "evidenced_status": _clean(message.get("evidenced_status"), 40) or None,
                "evidence_company": _clean(message.get("evidence_company"), 120) or None,
                "evidence_role": _clean(message.get("evidence_role"), 160) or None,
                "tracker_matches": matches,
                "first_seen": previous.get("first_seen") or current_iso,
                "last_seen": current_iso,
            }
            item.pop("resolved_at", None)
            item.pop("archived_at", None)
            item.pop("resolution", None)
            by_key[message_key] = item
            if opened:
                result["opened_count"] += 1
        elif action in {"update", "add", "no_change"} and previous.get("state") == "open":
            previous.update(
                {
                    "state": "resolved",
                    "last_seen": current_iso,
                    "resolved_at": current_iso,
                    "resolution": action,
                }
            )
            by_key[message_key] = previous
            result["resolved_count"] += 1

    retained: list[dict[str, Any]] = []
    for item in by_key.values():
        state = str(item.get("state") or "open")
        reference = _parse_mail_timestamp(
            item.get("resolved_at")
            or item.get("ignored_at")
            or item.get("last_seen")
            or item.get("message_date")
        )
        age_days = (current - reference).days if reference else 0
        tracker_related = bool(item.get("tracker_matches"))
        if state == "open" and not tracker_related and age_days >= MAIL_REVIEW_UNMATCHED_ARCHIVE_DAYS:
            item["state"] = "archived"
            item["archived_at"] = current_iso
            item["archive_reason"] = "unmatched_open_90_days"
            state = "archived"
            result["archived_count"] += 1
        if state in {"resolved", "ignored"} and age_days >= MAIL_REVIEW_METADATA_RETENTION_DAYS:
            result["pruned_count"] += 1
            continue
        retained.append(item)

    retained.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    queue = {
        "schema_version": "career-ops-v2.mail-review.v1",
        "updated_at": current_iso,
        "items": retained,
    }
    open_items = [item for item in retained if item.get("state") == "open"]
    open_tracker_ids = sorted(
        {
            _clean(match.get("id"), 20)
            for item in open_items
            for match in (item.get("tracker_matches") or [])
            if isinstance(match, dict) and _clean(match.get("id"), 20).isdigit()
        },
        key=int,
    )
    result.update(
        status="ok",
        open_count=len(open_items),
        open_items=[
            {
                "message_key": item.get("message_key"),
                "message_date": item.get("message_date"),
                "subject": _clean(item.get("subject"), 180),
                "reason": item.get("reason"),
                "tracker_ids": [
                    _clean(match.get("id"), 20)
                    for match in (item.get("tracker_matches") or [])
                    if isinstance(match, dict)
                ][:3],
            }
            for item in open_items[:MAIL_REVIEW_SLACK_LIMIT]
        ],
        open_items_truncated=len(open_items) > MAIL_REVIEW_SLACK_LIMIT,
        open_tracker_ids=open_tracker_ids,
    )

    serialized = json.dumps(queue, ensure_ascii=False, sort_keys=True)
    if mode == "apply" and serialized != original:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=queue_path.parent,
            prefix=f".{queue_path.name}.",
            delete=False,
        ) as handle:
            handle.write(json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, queue_path)
        result["persisted"] = True

    audit["review_queue"] = result
    return result


def _mail_audit_object(mail_payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(mail_payload.get("mail_audit"), dict):
        return mail_payload["mail_audit"]
    return mail_payload


def _mail_action_date(value: Any) -> str | None:
    match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", str(value or ""))
    return match.group(1) if match else None


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def apply_mail_reconciliation_actions(
    mail_payload: dict[str, Any],
    *,
    project_root: Path,
    node_bin: Path,
    mode: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Apply exact Gmail-backed tracker actions through canonical project CLIs.

    Updates use ``set-status.mjs --row`` so row selection, locking, canonical
    state validation, atomic writes, notes, and the status ledger remain owned
    by Career-Ops. New rows use one reserved number per action and the existing
    ``merge-tracker.mjs`` path. Existing unrelated pending TSV additions make
    the add path fail closed instead of being merged incidentally.
    """
    audit = _mail_audit_object(mail_payload)
    reconciliation = (
        audit.get("reconciliation")
        if isinstance(audit.get("reconciliation"), dict)
        else {}
    )
    actions = [
        dict(item)
        for item in (reconciliation.get("actions") or [])
        if isinstance(item, dict) and item.get("action") in {"update", "add"}
    ]
    result: dict[str, Any] = {
        "status": "no_changes",
        "mode": mode,
        "requested_count": len(actions),
        "applied_count": 0,
        "update_count": 0,
        "add_count": 0,
        "failed_count": 0,
        "applied": [],
        "failures": [],
        "tracker_synced": False,
    }
    reconciliation["apply_result"] = result
    audit["reconciliation"] = reconciliation

    if mode != "apply":
        result["status"] = "dry_run" if actions else "no_changes"
        return result
    if not actions:
        return result

    node = str(node_bin)
    if not node_bin.is_file():
        result.update(
            status="error",
            failed_count=len(actions),
            failures=[{"action": "all", "error": f"Node binary unavailable: {node}"}],
        )
        return result

    update_actions = [item for item in actions if item.get("action") == "update"]
    add_actions = [item for item in actions if item.get("action") == "add"]

    def failure(action: dict[str, Any], detail: Any) -> None:
        result["failures"].append(
            {
                "action": _clean(action.get("action"), 20),
                "tracker_id": _clean(action.get("tracker_id"), 20) or None,
                "company": _clean(action.get("company"), 120),
                "role": _clean(action.get("role"), 160),
                "error": _clean(detail, MAX_ERROR_CHARS),
            }
        )

    for action in update_actions:
        tracker_id = _clean(action.get("tracker_id"), 20)
        new_status = _clean(action.get("new_status"), 40)
        if not tracker_id.isdigit() or not new_status:
            failure(action, "Exact tracker ID or new status missing")
            continue
        event_date = _mail_action_date(action.get("date"))
        evidence_subject = _clean(action.get("evidence_subject"), 180)
        note_date = event_date or datetime.now(UTC).date().isoformat()
        note = f"{note_date} 읽기 전용 Gmail 상태 확인"
        if evidence_subject:
            note += f": {evidence_subject}"
        command = [
            node,
            str(project_root / "set-status.mjs"),
            "--row",
            tracker_id,
            new_status,
            "--note",
            note,
        ]
        if event_date:
            command.extend(["--on", event_date])
        command.append("--json")
        completed = runner(
            command,
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        parsed = _parse_json_stdout(completed.stdout or "")
        if completed.returncode != 0 or parsed.get("error"):
            failure(action, parsed.get("error") or completed.stderr or completed.stdout)
            continue
        result["applied"].append(
            {
                "action": "update",
                "tracker_id": tracker_id,
                "company": _clean(action.get("company"), 120),
                "role": _clean(action.get("role"), 160),
                "old_status": _clean(parsed.get("oldStatus") or action.get("current_status"), 40),
                "new_status": _clean(parsed.get("newStatus") or new_status, 40),
                "changed": bool(parsed.get("changed", True)),
            }
        )
        result["update_count"] += 1

    reserved_numbers: list[int] = []
    addition_paths: list[Path] = []
    if add_actions:
        additions_dir = project_root / "batch" / "tracker-additions"
        additions_dir.mkdir(parents=True, exist_ok=True)
        unrelated = sorted(path.name for path in additions_dir.glob("*.tsv"))
        if unrelated:
            for action in add_actions:
                failure(
                    action,
                    "Existing pending tracker additions require review: "
                    + ", ".join(unrelated[:5]),
                )
        else:
            reserve = runner(
                [
                    node,
                    str(project_root / "reserve-report-num.mjs"),
                    "--count",
                    str(len(add_actions)),
                ],
                cwd=project_root,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            reserved_text = (reserve.stdout or "").strip()
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", reserved_text)
            if reserve.returncode != 0 or not match:
                for action in add_actions:
                    failure(action, reserve.stderr or reserve.stdout or "ID reservation failed")
            else:
                start = int(match.group(1))
                end = int(match.group(2) or match.group(1))
                reserved_numbers = list(range(start, end + 1))
                if len(reserved_numbers) != len(add_actions):
                    for action in add_actions:
                        failure(action, "Reserved ID count did not match requested additions")
                    reserved_numbers = []

        if reserved_numbers:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for number, action in zip(reserved_numbers, add_actions, strict=True):
                event_date = _mail_action_date(action.get("date")) or datetime.now(UTC).date().isoformat()
                company = _clean(action.get("company"), 200)
                role = _clean(action.get("role"), 300)
                status = _clean(action.get("new_status"), 40)
                subject = _clean(action.get("evidence_subject"), 180)
                if not company or not role or not status:
                    failure(action, "Date, company, role, or status missing for tracker add")
                    continue
                note = f"{event_date} 읽기 전용 Gmail 확인"
                if subject:
                    note += f": {subject}"
                fields = [
                    str(number),
                    event_date,
                    company,
                    role,
                    status,
                    "N/A",
                    "-",
                    "-",
                    note,
                ]
                addition_path = additions_dir / f"{number}-mail-v2-{stamp}.tsv"
                addition_path.write_text("\t".join(fields) + "\n", encoding="utf-8")
                addition_paths.append(addition_path)

            if len(addition_paths) == len(add_actions):
                merged = runner(
                    [node, str(project_root / "merge-tracker.mjs")],
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
                if merged.returncode == 0:
                    for number, action in zip(reserved_numbers, add_actions, strict=True):
                        result["applied"].append(
                            {
                                "action": "add",
                                "tracker_id": str(number),
                                "company": _clean(action.get("company"), 120),
                                "role": _clean(action.get("role"), 160),
                                "old_status": None,
                                "new_status": _clean(action.get("new_status"), 40),
                                "changed": True,
                            }
                        )
                        result["add_count"] += 1
                    released = runner(
                        [
                            node,
                            str(project_root / "reserve-report-num.mjs"),
                            "--release",
                            (
                                str(reserved_numbers[0])
                                if len(reserved_numbers) == 1
                                else f"{reserved_numbers[0]}-{reserved_numbers[-1]}"
                            ),
                        ],
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                    if released.returncode != 0:
                        result["failures"].append(
                            {
                                "action": "release_reservation",
                                "error": _clean(released.stderr or released.stdout, MAX_ERROR_CHARS),
                            }
                        )
                else:
                    for action in add_actions:
                        failure(action, merged.stderr or merged.stdout or "Tracker merge failed")

    result["applied_count"] = len(result["applied"])
    result["failed_count"] = len(result["failures"])
    if result["applied_count"]:
        synced = runner(
            [node, str(project_root / "tracker.mjs"), "sync"],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        result["tracker_synced"] = synced.returncode == 0
        if synced.returncode != 0:
            result["failures"].append(
                {
                    "action": "tracker_sync",
                    "error": _clean(synced.stderr or synced.stdout, MAX_ERROR_CHARS),
                }
            )
            result["failed_count"] = len(result["failures"])

    result["status"] = (
        "ok"
        if result["failed_count"] == 0
        else "partial"
        if result["applied_count"] > 0
        else "error"
    )
    return result


def _recent_mail_tracker_ids(
    mail_payload: dict[str, Any],
    *,
    cutoff: datetime | None = None,
) -> set[str]:
    """Return rows linked to recent mail or an unresolved review-queue item.

    A normal daily audit already covers only seven days. A one-time historical
    audit can cover a year, so its old receipt messages must not protect stale
    applications from the 60-day no-response rule. When ``cutoff`` is given,
    only messages on or after that instant are treated as recent. Open review
    items remain protected regardless of age until they are resolved.
    """
    audit = _mail_audit_object(mail_payload)
    protected: set[str] = set()
    for message in audit.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if cutoff is not None:
            message_time = _parse_mail_timestamp(message.get("date"))
            # Missing/invalid dates fail closed: an ambiguous message can still
            # protect a tracker row. Historical audits provide normalized dates,
            # allowing genuinely old messages to be excluded from protection.
            if message_time is not None and message_time < cutoff:
                continue
        for match in message.get("tracker_matches") or []:
            if not isinstance(match, dict):
                continue
            tracker_id = _clean(match.get("id"), 20)
            if tracker_id.isdigit():
                protected.add(tracker_id)
    review_queue = audit.get("review_queue") if isinstance(audit.get("review_queue"), dict) else {}
    for value in review_queue.get("open_tracker_ids") or []:
        tracker_id = _clean(value, 20)
        if tracker_id.isdigit():
            protected.add(tracker_id)
    return protected


def apply_no_response_closures(
    mail_payload: dict[str, Any],
    tracker: dict[str, Any],
    *,
    project_root: Path,
    node_bin: Path,
    mode: str,
    enabled: bool = True,
    threshold_days: int = 60,
    max_per_run: int = 20,
    today: date | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Close stale Applied rows only after a successful read-only Gmail audit.

    A no-response closure is operational housekeeping, not evidence of a
    rejection. Therefore it uses the canonical ``Discarded`` status and an
    explicit note. Any row matched to recent recruiting mail is protected,
    even when the message is ambiguous and requires manual review.
    """
    run_date = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    threshold = max(1, int(threshold_days))
    run_limit = max(1, min(int(max_per_run), 50))
    audit = _mail_audit_object(mail_payload)
    audit_source = _clean(audit.get("source") or mail_payload.get("source"), 80)
    audit_status = _clean(audit.get("status"), 40)
    result: dict[str, Any] = {
        "status": "no_changes",
        "mode": mode,
        "enabled": bool(enabled),
        "threshold_days": threshold,
        "cutoff_date": (run_date - timedelta(days=threshold)).isoformat(),
        "mail_audit_status": audit_status or "unavailable",
        "mail_lookback_days": audit.get("lookback_days"),
        "applied_status_count": 0,
        "aged_count": 0,
        "protected_recent_mail_count": 0,
        "eligible_count": 0,
        "requested_count": 0,
        "applied_count": 0,
        "failed_count": 0,
        "deferred_count": 0,
        "invalid_date_count": 0,
        "invalid_rows": [],
        "actions": [],
        "failures": [],
        "tracker_synced": False,
    }
    if not enabled:
        result["status"] = "disabled"
        return result
    if audit_status != "ok" or audit_source != "codex-gmail-readonly":
        result["status"] = "blocked_mail_audit"
        result["reason"] = (
            "A successful codex-gmail-readonly audit is required before automatic closure"
        )
        return result
    if tracker.get("status") != "ok":
        result["status"] = "error"
        result["failures"].append(
            {"action": "tracker_read", "error": "Canonical application tracker unavailable"}
        )
        result["failed_count"] = 1
        return result

    cutoff = datetime.combine(
        run_date - timedelta(days=threshold),
        datetime.min.time(),
        tzinfo=ZoneInfo("Asia/Seoul"),
    ).astimezone(UTC)
    protected_ids = _recent_mail_tracker_ids(mail_payload, cutoff=cutoff)
    eligible: list[dict[str, Any]] = []
    for raw in tracker.get("entries") or []:
        if not isinstance(raw, dict) or str(raw.get("status") or "").casefold() != "applied":
            continue
        result["applied_status_count"] += 1
        tracker_id = _clean(raw.get("id"), 20)
        application_date_text = _clean(raw.get("date"), 40)
        try:
            application_date = date.fromisoformat(application_date_text)
        except ValueError:
            result["invalid_rows"].append(
                {
                    "tracker_id": tracker_id or None,
                    "application_date": application_date_text or None,
                }
            )
            continue
        age_days = (run_date - application_date).days
        if age_days < threshold:
            continue
        result["aged_count"] += 1
        if tracker_id in protected_ids:
            result["protected_recent_mail_count"] += 1
            continue
        eligible.append(
            {
                "action": "no_response_close",
                "tracker_id": tracker_id,
                "company": _clean(raw.get("company"), 120),
                "role": _clean(raw.get("role"), 160),
                "application_date": application_date.isoformat(),
                "age_days": age_days,
                "old_status": _clean(raw.get("status"), 40),
                "new_status": "Discarded",
                "outcome": "pending",
            }
        )

    result["invalid_date_count"] = len(result["invalid_rows"])
    eligible.sort(
        key=lambda item: (
            -int(item.get("age_days") or 0),
            int(item["tracker_id"]) if str(item.get("tracker_id") or "").isdigit() else 1_000_000_000,
        )
    )
    result["eligible_count"] = len(eligible)
    selected = eligible[:run_limit]
    result["deferred_count"] = max(0, len(eligible) - len(selected))
    result["requested_count"] = len(selected)
    result["actions"] = selected
    if not selected:
        return result
    if mode != "apply":
        for action in selected:
            action["outcome"] = "preview"
        result["status"] = "dry_run"
        return result

    node = str(node_bin)
    if not node_bin.is_file():
        result["status"] = "error"
        result["failed_count"] = len(selected)
        result["failures"] = [
            {
                "action": "all",
                "error": f"Node binary unavailable: {node}",
            }
        ]
        for action in selected:
            action["outcome"] = "failed"
        return result

    for action in selected:
        tracker_id = str(action.get("tracker_id") or "")
        note = (
            f"{run_date.isoformat()} {threshold}일 무응답 자동 종료 "
            f"(명시적 불합격 아님; 지원일 {action['application_date']}; "
            f"경과 {action['age_days']}일)"
        )
        completed = runner(
            [
                node,
                str(project_root / "set-status.mjs"),
                "--row",
                tracker_id,
                "Discarded",
                "--note",
                note,
                "--json",
            ],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        parsed = _parse_json_stdout(completed.stdout or "")
        if completed.returncode != 0 or parsed.get("error"):
            action["outcome"] = "failed"
            result["failures"].append(
                {
                    "action": "no_response_close",
                    "tracker_id": tracker_id,
                    "company": action["company"],
                    "role": action["role"],
                    "error": _clean(
                        parsed.get("error") or completed.stderr or completed.stdout,
                        MAX_ERROR_CHARS,
                    ),
                }
            )
            continue
        changed = bool(parsed.get("changed", True))
        action["changed"] = changed
        action["outcome"] = "applied" if changed else "no_change"
        if changed:
            result["applied_count"] += 1

    result["failed_count"] = len(result["failures"])
    if result["applied_count"]:
        synced = runner(
            [node, str(project_root / "tracker.mjs"), "sync"],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        result["tracker_synced"] = synced.returncode == 0
        if synced.returncode != 0:
            result["failures"].append(
                {
                    "action": "tracker_sync",
                    "error": _clean(synced.stderr or synced.stdout, MAX_ERROR_CHARS),
                }
            )
            result["failed_count"] = len(result["failures"])

    result["status"] = (
        "ok"
        if result["failed_count"] == 0
        else "partial"
        if result["applied_count"] > 0
        else "error"
    )
    return result


def _contains(value: str, terms: list[str]) -> bool:
    normalized = value.casefold()
    compact = "".join(character for character in normalized if character.isalnum())
    return any(
        (term_text := str(term).casefold()) in normalized
        or (
            (term_compact := "".join(
                character for character in term_text if character.isalnum()
            ))
            and len(term_compact) >= 4
            and term_compact in compact
        )
        for term in terms
    )


def _candidate_score(candidate: dict[str, Any], config: dict[str, Any]) -> int:
    title = str(candidate.get("title") or "")
    score = 100 if candidate.get("direct_verified") is True else 0
    for index, term in enumerate(config.get("priority_terms") or []):
        if _contains(title, [str(term)]):
            score += max(1, 30 - index)
    return score


def assess_profile_fit(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Use title evidence as a soft rank signal, never as an exclusion gate."""
    title = _clean(candidate.get("title"), 300).casefold()
    policy = config.get("profile_fit") or {}
    leadership = [term for term in policy.get("preferred_title_terms", []) if _contains(title, [term])]
    senior = [term for term in policy.get("secondary_title_terms", []) if _contains(title, [term])]
    entry = [term for term in policy.get("deprioritized_title_terms", []) if _contains(title, [term])]
    score = (3 if leadership else 0) + (1 if senior else 0) - (3 if entry else 0)
    reasons: list[str] = []
    if leadership:
        reasons.append("preferred_title")
    if senior:
        reasons.append("secondary_title")
    if entry:
        reasons.append("deprioritized_title")
    review_required = bool(entry) or not bool(leadership)
    if review_required:
        reasons.append("title_only_seniority_uncertain")
    return {
        "profile_fit_score": score,
        "profile_fit_reasons": reasons,
        "profile_review_required": review_required,
    }


def verified_direct_records(candidates: Any) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("direct_verified") is True
        and candidate.get("liveness") == "active"
    ]


def select_candidates(
    direct_candidates: list[dict[str, Any]],
    pipeline_candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    limit: int,
    tracker: dict[str, Any] | None = None,
    history_gate_stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()
    for raw in [*direct_candidates, *pipeline_candidates]:
        if not isinstance(raw, dict):
            continue
        title = _clean(raw.get("title"), 300)
        company = _clean(raw.get("company"), 200)
        location = _clean(raw.get("location"), 200)
        url = _clean(raw.get("url"), 1200)
        if not title or not company or not location or not url:
            continue
        if not _contains(title, config.get("positive_title_terms") or []):
            continue
        excluded_terms = config.get("excluded_title_terms") or []
        overrideable_exclusions = config.get("scope_override_excluded_terms") or []
        overrideable_keys = {str(term).casefold() for term in overrideable_exclusions}
        hard_exclusions = [
            term for term in excluded_terms if str(term).casefold() not in overrideable_keys
        ]
        if _contains(title, hard_exclusions):
            continue
        if (
            _contains(title, overrideable_exclusions)
            and not _contains(
                title,
                config.get("scope_override_title_terms")
                or [],
            )
        ):
            continue
        identity = raw.get("_identity_decision") if isinstance(raw.get("_identity_decision"), dict) else None
        if identity is not None:
            if identity.get("location_eligible") is not True:
                continue
            role_key = (
                str(identity.get("company_key") or company.casefold()),
                str(identity.get("role_key") or title.casefold()),
            )
        else:
            if not _contains(location, config.get("allowed_locations") or []):
                continue
            role_key = (company.casefold(), title.casefold())
        if url in seen_urls or role_key in seen_roles:
            continue
        seen_urls.add(url)
        seen_roles.add(role_key)
        source = classify_job_source(url)
        try:
            evaluation_score = float(raw.get("evaluation_score") or 0)
        except (TypeError, ValueError):
            evaluation_score = 0.0
        candidate = {
            "url": url,
            "title": title,
            "company": company,
            "location": location,
            "source": _clean(raw.get("source"), 100) or f"pipeline:{source['source_id']}",
            "source_id": _clean(raw.get("source_id"), 100) or source["source_id"],
            "source_label": _clean(raw.get("source_label"), 100) or source["source_label"],
            "source_host": _clean(raw.get("source_host"), 200) or source["source_host"],
            "direct_verified": bool(raw.get("direct_verified")),
            "liveness": _clean(raw.get("liveness"), 40) or "not_checked",
            "description": _clean(raw.get("description"), 800),
            "priority_score": _candidate_score(raw, config),
            "pipeline_order": raw.get("pipeline_order"),
            "pipeline_bucket": _clean(raw.get("pipeline_bucket"), 80) or None,
            "last_liveness_checked_at": raw.get("_last_liveness_checked_at"),
            "last_liveness_checked_epoch": raw.get("_last_liveness_checked_epoch"),
            "candidate_origin": _clean(raw.get("candidate_origin"), 80)
            or ("linkedin_direct_new" if raw.get("direct_verified") else "pending_new"),
            "evaluation_id": _clean(raw.get("evaluation_id"), 20) or None,
            "evaluation_score": evaluation_score or None,
            "evaluation_score_text": _clean(raw.get("evaluation_score_text"), 20) or None,
            "posting_date": _date_text(raw.get("posting_date")),
            "first_seen": _date_text(raw.get("first_seen")),
            "freshness_ordinal": int(raw.get("_freshness_ordinal") or 0),
            "recommendation_cooldown": bool(raw.get("_recommendation_cooldown")),
            "recommendation_cooldown_until": _clean(
                raw.get("_recommendation_cooldown_until"), 80
            ) or None,
            "company_key": _clean((identity or {}).get("company_key"), 200) or None,
            "role_key": _clean((identity or {}).get("role_key"), 300) or None,
            "posting_cluster_id": _clean(
                (identity or {}).get("posting_cluster_id"), 80
            ) or None,
            "listing_instance_id": _clean(
                (identity or {}).get("listing_instance_id"), 80
            ) or None,
            "location_eligible": (
                bool(identity.get("location_eligible")) if identity is not None else True
            ),
        }
        candidate.update(assess_profile_fit(candidate, config))
        decision = (
            {
                "history_gate": identity.get("history_gate", "eligible"),
                "history_reason": identity.get("history_reason", "no_blocking_history"),
                "history_tracker_matches": identity.get("history_tracker_matches") or [],
            }
            if identity is not None
            else _history_decision(candidate, tracker)
        )
        _record_history_gate(history_gate_stats, candidate, decision)
        candidate.update(decision)
        if decision["history_gate"] != "eligible":
            continue
        selected.append(candidate)
    selected.sort(
        key=lambda item: (
            0 if not item.get("recommendation_cooldown") else 1,
            0 if int(item.get("freshness_ordinal") or 0) else 1,
            -int(item.get("freshness_ordinal") or 0),
            -int(item.get("profile_fit_score") or 0),
            0 if item.get("direct_verified") else 1,
            0 if float(item.get("evaluation_score") or 0) else 1,
            -float(item.get("evaluation_score") or 0),
            0 if item.get("last_liveness_checked_epoch") is None else 1,
            int(item.get("last_liveness_checked_epoch") or 0),
            -int(item["priority_score"]),
            int(item["pipeline_order"]) if isinstance(item.get("pipeline_order"), int) else 1_000_000_000,
            item["company"].casefold(),
            item["title"].casefold(),
        )
    )
    # The pre-verification pool may be larger than the five candidates exposed
    # to the agent. This lets a closed first result fall through to another
    # candidate from the same source without increasing prompt size.
    bounded_limit = max(1, min(limit, 100))
    return _source_round_robin(selected, bounded_limit)


def prioritize_non_cooldown(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep source order stable while deferring already-recommended postings."""
    return [item for item in candidates if not item.get("recommendation_cooldown")] + [
        item for item in candidates if item.get("recommendation_cooldown")
    ]


def _mail_usage(mail_payload: dict[str, Any]) -> dict[str, Any]:
    audit = (
        mail_payload.get("mail_audit")
        if isinstance(mail_payload.get("mail_audit"), dict)
        else mail_payload
    )
    usage = audit.get("usage") if isinstance(audit.get("usage"), dict) else {}

    def counter(name: str) -> int:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "available": bool(usage.get("available")),
        "input_tokens": counter("input_tokens"),
        "cached_input_tokens": counter("cached_input_tokens"),
        "prompt_tokens": counter("prompt_tokens"),
        "output_tokens": counter("output_tokens"),
        "reasoning_output_tokens": counter("reasoning_output_tokens"),
        "total_tokens": counter("total_tokens"),
        "reasoning_effort": _clean(usage.get("reasoning_effort"), 20) or None,
    }


def _mail_compact(mail_payload: dict[str, Any]) -> dict[str, Any]:
    audit = mail_payload.get("mail_audit") if isinstance(mail_payload.get("mail_audit"), dict) else mail_payload
    messages = audit.get("messages") if isinstance(audit.get("messages"), list) else []
    review_queue = (
        audit.get("review_queue")
        if isinstance(audit.get("review_queue"), dict)
        else {}
    )
    reconciliation = (
        audit.get("reconciliation")
        if isinstance(audit.get("reconciliation"), dict)
        else {}
    )
    return {
        "status": audit.get("status", "skipped"),
        "source": audit.get("source") or mail_payload.get("source"),
        "readonly": (audit.get("source") or mail_payload.get("source")) == "codex-gmail-readonly",
        "lookback_days": audit.get("lookback_days"),
        "elapsed_seconds": audit.get("elapsed_seconds") or mail_payload.get("process_elapsed_seconds"),
        "message_count": max(
            len(messages),
            int(audit.get("message_count", 0) or 0),
        ),
        "usage": _mail_usage(mail_payload),
        "tracker_status": audit.get("tracker_status"),
        "tracker_count": audit.get("tracker_count", 0),
        "review_queue": {
            "status": review_queue.get("status", "skipped"),
            "open_count": review_queue.get("open_count", 0),
            "opened_count": review_queue.get("opened_count", 0),
            "resolved_count": review_queue.get("resolved_count", 0),
            "archived_count": review_queue.get("archived_count", 0),
            "persisted": bool(review_queue.get("persisted")),
            "open_items": [
                {
                    "message_date": _clean(item.get("message_date"), 80) or None,
                    "subject": _clean(item.get("subject"), 180),
                    "reason": _clean(item.get("reason"), 80),
                    "tracker_ids": [
                        _clean(value, 20)
                        for value in (item.get("tracker_ids") or [])[:3]
                    ],
                }
                for item in (review_queue.get("open_items") or [])[:MAIL_REVIEW_SLACK_LIMIT]
                if isinstance(item, dict)
            ],
            "open_items_truncated": bool(review_queue.get("open_items_truncated")),
            "error": _clean(review_queue.get("error"), 240) or None,
        },
        "reconciliation": {
            "action_count": reconciliation.get("action_count", 0),
            "update_count": reconciliation.get("update_count", 0),
            "add_count": reconciliation.get("add_count", 0),
            "unchanged_count": reconciliation.get("unchanged_count", 0),
            "review_count": reconciliation.get("review_count", 0),
            "actions": [
                {
                    "action": _clean(item.get("action"), 20),
                    "tracker_id": _clean(item.get("tracker_id"), 20) or None,
                    "date": _clean(item.get("date"), 40),
                    "company": _clean(item.get("company"), 200),
                    "role": _clean(item.get("role"), 300),
                    "current_status": _clean(item.get("current_status"), 40) or None,
                    "new_status": _clean(item.get("new_status"), 40),
                    "evidence_subject": _clean(item.get("evidence_subject"), 240),
                }
                for item in (reconciliation.get("actions") or [])[:10]
                if isinstance(item, dict)
            ],
            "apply_result": {
                "status": (reconciliation.get("apply_result") or {}).get("status"),
                "mode": (reconciliation.get("apply_result") or {}).get("mode"),
                "requested_count": (reconciliation.get("apply_result") or {}).get(
                    "requested_count", 0
                ),
                "applied_count": (reconciliation.get("apply_result") or {}).get(
                    "applied_count", 0
                ),
                "update_count": (reconciliation.get("apply_result") or {}).get(
                    "update_count", 0
                ),
                "add_count": (reconciliation.get("apply_result") or {}).get(
                    "add_count", 0
                ),
                "failed_count": (reconciliation.get("apply_result") or {}).get(
                    "failed_count", 0
                ),
                "tracker_synced": bool(
                    (reconciliation.get("apply_result") or {}).get("tracker_synced")
                ),
                "applied": [
                    {
                        "action": _clean(item.get("action"), 20),
                        "tracker_id": _clean(item.get("tracker_id"), 20) or None,
                        "company": _clean(item.get("company"), 120),
                        "role": _clean(item.get("role"), 160),
                        "old_status": _clean(item.get("old_status"), 40) or None,
                        "new_status": _clean(item.get("new_status"), 40),
                        "changed": bool(item.get("changed")),
                    }
                    for item in (reconciliation.get("apply_result") or {}).get(
                        "applied", []
                    )[:10]
                    if isinstance(item, dict)
                ],
                "failures": [
                    {
                        "action": _clean(item.get("action"), 30),
                        "tracker_id": _clean(item.get("tracker_id"), 20) or None,
                        "company": _clean(item.get("company"), 120),
                        "role": _clean(item.get("role"), 160),
                        "error": _clean(item.get("error"), 240),
                    }
                    for item in (reconciliation.get("apply_result") or {}).get(
                        "failures", []
                    )[:10]
                    if isinstance(item, dict)
                ],
            },
        },
        "messages": [
            {
                "date": _clean(item.get("date"), 80),
                "from": _clean(item.get("from"), 160),
                "subject": _clean(item.get("subject"), 240),
                "snippet": _clean(item.get("snippet"), 240),
                "evidenced_status": _clean(item.get("evidenced_status"), 40) or None,
                "evidence_company": _clean(item.get("evidence_company"), 200) or None,
                "evidence_role": _clean(item.get("evidence_role"), 300) or None,
                "reconciliation_action": _clean(item.get("reconciliation_action"), 40),
                "tracker_match_count": len(item.get("tracker_matches") or []),
                "decision": {
                    "classification": _clean(
                        (item.get("decision_object") or {}).get("classification"), 40
                    ),
                    "selected_tracker_id": _clean(
                        (item.get("decision_object") or {}).get("selected_tracker_id"), 20
                    ) or None,
                    "action": _clean(
                        (item.get("decision_object") or {}).get("action"), 40
                    ),
                },
                "tracker_matches": [
                    {
                        "id": _clean(match.get("id"), 20),
                        "company": _clean(match.get("company"), 200),
                        "role": _clean(match.get("role"), 300),
                        "status": _clean(match.get("status"), 40),
                        "role_match": bool(match.get("role_match")),
                    }
                    for match in (item.get("tracker_matches") or [])[:2]
                    if isinstance(match, dict)
                ],
            }
            for item in messages[:10]
            if isinstance(item, dict)
        ],
        "error": _clean(audit.get("error"), MAX_ERROR_CHARS) or None,
    }


def _deduplicate_compact_apply_results(payload: dict[str, Any]) -> None:
    """Replace repeated successful apply details with action references."""
    mail_audit = payload.get("mail_audit")
    if not isinstance(mail_audit, dict):
        return
    reconciliation = mail_audit.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return
    actions = reconciliation.get("actions")
    apply_result = reconciliation.get("apply_result")
    if not isinstance(actions, list) or not isinstance(apply_result, dict):
        return
    applied = apply_result.get("applied")
    if not isinstance(applied, list):
        return

    used_action_indexes: set[int] = set()
    references: list[dict[str, Any]] = []
    for result in applied:
        if not isinstance(result, dict):
            continue
        result_action = _clean(result.get("action"), 20)
        result_tracker_id = _clean(result.get("tracker_id"), 20)
        match_index: int | None = None
        for index, action in enumerate(actions):
            if index in used_action_indexes or not isinstance(action, dict):
                continue
            if _clean(action.get("action"), 20) != result_action:
                continue
            if result_action == "update":
                if _clean(action.get("tracker_id"), 20) != result_tracker_id:
                    continue
            elif result_action == "add":
                if any(
                    _clean(action.get(key), limit) != _clean(result.get(key), limit)
                    for key, limit in (
                        ("company", 120),
                        ("role", 160),
                        ("new_status", 40),
                    )
                ):
                    continue
            else:
                continue
            match_index = index
            break
        if match_index is None:
            references.append(dict(result))
            continue
        used_action_indexes.add(match_index)
        references.append(
            {
                "action": result_action,
                "action_index": match_index,
                "tracker_id": result_tracker_id or None,
                "changed": bool(result.get("changed")),
            }
        )
    apply_result["applied"] = references


def _no_response_compact(result: dict[str, Any] | None) -> dict[str, Any]:
    value = result or {}
    actions = [item for item in (value.get("actions") or []) if isinstance(item, dict)]
    return {
        key: value.get(key)
        for key in (
            "status",
            "mode",
            "enabled",
            "threshold_days",
            "cutoff_date",
            "mail_audit_status",
            "mail_lookback_days",
            "applied_status_count",
            "aged_count",
            "protected_recent_mail_count",
            "eligible_count",
            "requested_count",
            "applied_count",
            "failed_count",
            "deferred_count",
            "invalid_date_count",
            "tracker_synced",
            "reason",
        )
    } | {
        "actions": [
            {
                "tracker_id": _clean(item.get("tracker_id"), 20),
                "company": _clean(item.get("company"), 120),
                "role": _clean(item.get("role"), 160),
                "age_days": item.get("age_days"),
                "outcome": _clean(item.get("outcome"), 30),
            }
            for item in actions[:20]
        ],
        "actions_truncated": len(actions) > 20,
        "failures": [
            {
                "tracker_id": _clean(item.get("tracker_id"), 20) or None,
                "error": _clean(item.get("error"), 240),
            }
            for item in (value.get("failures") or [])[:5]
            if isinstance(item, dict)
        ],
    }


def _compact_encoded_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _bound_compact_payload(payload: dict[str, Any], max_bytes: int) -> int:
    """Remove optional detail in stages while preserving decisions and counts.

    Reconciliation actions, candidate identity/URL/liveness fields, aggregate
    counts, and the history-gate decision counts are never dropped.
    Additional passes bound large mail/profile/source diagnostics without
    increasing prompt size.
    """

    def encoded_size() -> int:
        return _compact_encoded_size(payload)

    size = encoded_size()
    if size <= max_bytes:
        return size

    candidates = payload.get("candidates") or []
    mail_audit = payload.get("mail_audit") or {}
    mail_messages = mail_audit.get("messages") or []
    collector = payload.get("collector") or {}
    history_gate = payload.get("history_gate") or {}
    profile = payload.get("profile_evidence") or {}
    linkedin = payload.get("linkedin") or {}
    liveness = payload.get("liveness") or {}
    application_status = payload.get("application_status") or {}
    no_response = payload.get("no_response_closure") or {}
    file_audit = payload.get("file_audit") or {}

    for candidate in candidates:
        candidate["description"] = _clean(candidate.get("description"), 500)
    size = encoded_size()

    if size > max_bytes:
        mail_audit["messages"] = mail_messages[:7]
        mail_audit["messages_truncated"] = len(mail_messages) > 7
        for candidate in candidates:
            candidate["description"] = _clean(candidate.get("description"), 180)
        size = encoded_size()
    if size > max_bytes:
        active = application_status.get("active") or []
        application_status["active"] = active[:20]
        application_status["active_truncated"] = (
            bool(application_status.get("active_truncated")) or len(active) > 20
        )
        size = encoded_size()
    if size > max_bytes:
        mail_audit["messages"] = (mail_audit.get("messages") or [])[:5]
        size = encoded_size()
    if size > max_bytes and no_response.get("actions"):
        active = application_status.get("active") or []
        application_status["active"] = active[:10]
        application_status["active_truncated"] = (
            bool(application_status.get("active_truncated")) or len(active) > 10
        )
        size = encoded_size()
    if size > max_bytes:
        for candidate in payload.get("candidates") or []:
            candidate["description"] = _clean(candidate.get("description"), 240)
        collector["new_offer_preview"] = (collector.get("new_offer_preview") or [])[:3]
        size = encoded_size()

    # Preserve aggregate mail counts and exact reconciliation actions, but
    # reduce repeated message evidence that is not required for a write.
    if size > max_bytes:
        bounded_messages = []
        for item in (mail_audit.get("messages") or [])[:3]:
            bounded = dict(item)
            bounded["from"] = _clean(bounded.get("from"), 100)
            bounded["subject"] = _clean(bounded.get("subject"), 160)
            bounded["snippet"] = _clean(bounded.get("snippet"), 120)
            bounded["evidence_company"] = _clean(bounded.get("evidence_company"), 120) or None
            bounded["evidence_role"] = _clean(bounded.get("evidence_role"), 160) or None
            bounded["tracker_matches"] = (bounded.get("tracker_matches") or [])[:1]
            bounded_messages.append(bounded)
        mail_audit["messages"] = bounded_messages
        mail_audit["messages_truncated"] = True
        history_gate["examples"] = (history_gate.get("examples") or [])[:3]
        profile["facts"] = [
            {**item, "evidence": _clean(item.get("evidence"), 200)}
            for item in (profile.get("facts") or [])[:4]
        ]
        size = encoded_size()

    if size > max_bytes:
        for candidate in payload.get("candidates") or []:
            candidate["description"] = _clean(candidate.get("description"), 120)
        for item in application_status.get("active") or []:
            item["company"] = _clean(item.get("company"), 100)
            item["role"] = _clean(item.get("role"), 140)
        mail_audit["messages"] = (mail_audit.get("messages") or [])[:2]
        collector["new_offer_preview"] = (collector.get("new_offer_preview") or [])[:2]
        linkedin["query_results"] = (linkedin.get("query_results") or [])[:1]
        history_gate["examples"] = (history_gate.get("examples") or [])[:2]
        size = encoded_size()

    if size > max_bytes and no_response.get("actions"):
        active = application_status.get("active") or []
        application_status["active"] = active[:5]
        application_status["active_truncated"] = (
            bool(application_status.get("active_truncated")) or len(active) > 5
        )
        size = encoded_size()

    # Last-resort optional-detail removal. Core counts and every exact tracker
    # action remain, so apply-mode behavior does not become partial or ambiguous.
    if size > max_bytes:
        mail_audit["messages"] = []
        collector["new_offer_preview"] = []
        collector_summary = collector.get("summary") or {}
        collector["summary"] = {
            key: collector_summary[key]
            for key in (
                "companies_scanned",
                "job_boards_scanned",
                "total_jobs_found",
                "filtered_by_title",
                "filtered_by_location",
                "filtered_by_salary",
                "filtered_by_content",
                "duplicates",
                "new_offers_found",
                "source_results",
            )
            if key in collector_summary
        }
        linkedin["query_results"] = []
        linkedin_filter_stats = linkedin.get("filter_stats") or {}
        linkedin["filter_stats"] = {
            _clean(key, 80): value
            for key, value in list(linkedin_filter_stats.items())[:20]
            if isinstance(value, (bool, int, float))
        }
        liveness["source_results"] = {}
        (payload.get("pipeline") or {})["source_inventory"] = []
        history_gate["examples"] = []
        profile["facts"] = [
            {**item, "evidence": _clean(item.get("evidence"), 120)}
            for item in (profile.get("facts") or [])[:2]
        ]
        for candidate in payload.get("candidates") or []:
            candidate["description"] = ""
        actions = (mail_audit.get("reconciliation") or {}).get("actions") or []
        for action in actions:
            action["company"] = _clean(action.get("company"), 40)
            action["role"] = _clean(action.get("role"), 60)
            action["evidence_subject"] = _clean(action.get("evidence_subject"), 20)
        for action in no_response.get("actions") or []:
            action["company"] = _clean(action.get("company"), 80)
            action["role"] = _clean(action.get("role"), 120)
        if isinstance(file_audit, dict) and file_audit:
            changed_paths = file_audit.get("changed") or []
            file_audit["changed"] = []
            file_audit["changed_paths_truncated"] = bool(changed_paths)
            verification = file_audit.get("verification")
            if isinstance(verification, dict):
                verification["summary"] = _clean(verification.get("summary"), 200)
        size = encoded_size()

    if size > max_bytes:
        for candidate in payload.get("candidates") or []:
            candidate["description"] = ""
            candidate.pop("actionability_reasons", None)
            candidate.pop("profile_fit_reasons", None)
            candidate.pop("profile_evidence_ids", None)
        size = encoded_size()

    # Candidate identity and liveness remain until every optional detail has
    # been exhausted. Only an unusually small configured budget can reduce the
    # candidate count, one tail item at a time.
    while size > max_bytes and payload.get("candidates"):
        payload["candidates"].pop()
        size = encoded_size()

    return size


def build_compact_payload(
    *,
    mode: str,
    mail: dict[str, Any],
    direct: dict[str, Any],
    fallback: dict[str, Any] | None,
    ingest: dict[str, Any],
    liveness: dict[str, Any],
    collector: dict[str, Any],
    pipeline: dict[str, Any],
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    max_bytes: int,
    history_gate: dict[str, Any] | None = None,
    application_status: dict[str, Any] | None = None,
    no_response_closure: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    history_gate = history_gate or {}
    identity = identity or {"status": "not_run", "record_count": 0}
    application_status = application_status or {
        "status": "unavailable",
        "total_count": 0,
        "status_counts": {},
        "active_count": 0,
        "active": [],
        "active_truncated": False,
        "no_response_closed_count": 0,
        "other_excluded_count": 0,
    }
    payload: dict[str, Any] = {
        "schema_version": "career-ops-v2.compact.v2",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "overall_status": "ok",
        "mail_audit": _mail_compact(mail),
        "linkedin": {
            "status": direct.get("status"),
            "source": direct.get("source"),
            "fallback_used": fallback is not None,
            "fallback_status": fallback.get("status") if fallback else None,
            "direct_elapsed_ms": direct.get("elapsed_ms"),
            "query_results": [
                {
                    key: item.get(key)
                    for key in (
                        "query_name",
                        "http_status",
                        "elapsed_ms",
                        "record_count",
                        "login_prompt",
                        "challenge",
                        "error",
                    )
                    if key in item
                }
                for item in (direct.get("query_results") or [])[:3]
                if isinstance(item, dict)
            ],
            "filter_stats": direct.get("filter_stats"),
            "ingest_preview": {
                key: ingest.get(key)
                for key in (
                    "status",
                    "dry_run",
                    "received",
                    "filtered_title",
                    "filtered_location",
                    "duplicate_url",
                    "duplicate_role",
                    "accepted",
                    "added",
                )
                if key in ingest
            },
        },
        "collector": {
            "status": collector.get("status", "skipped_in_dry_run"),
            "dry_run": collector.get("dry_run"),
            "writes_enabled": collector.get("writes_enabled"),
            "root_verified": collector.get("root_verified"),
            "project_root": collector.get("project_root"),
            "expected_project_root": collector.get("expected_project_root"),
            "run_id": collector.get("run_id"),
            "elapsed_seconds": collector.get("elapsed_seconds"),
            "summary": collector.get("summary", {}),
            "new_offer_preview": [
                {
                    "company": _clean(item.get("company"), 200),
                    "title": _clean(item.get("title"), 300),
                    "location": _clean(item.get("location"), 200),
                }
                for item in (collector.get("new_offer_preview") or [])[:5]
                if isinstance(item, dict)
            ],
            "error": _clean(collector.get("error"), MAX_ERROR_CHARS) or None,
        },
        "liveness": {
            "status": liveness.get("status"),
            "requested": liveness.get("requested", 0),
            "checked": liveness.get("checked", 0),
            "elapsed_seconds": liveness.get("elapsed_seconds", 0),
            "active": sum(1 for value in (liveness.get("results") or {}).values() if value == "active"),
            "expired": sum(1 for value in (liveness.get("results") or {}).values() if value == "expired"),
            "uncertain": sum(1 for value in (liveness.get("results") or {}).values() if value == "uncertain"),
            "source_results": liveness.get("source_results", {}),
            "state": liveness.get("state", {}),
            "error": _clean(liveness.get("error"), MAX_ERROR_CHARS) or None,
        },
        "pipeline": {
            "status": pipeline.get("status"),
            "pending_count": pipeline.get("pending_count", 0),
            "processed_count": pipeline.get("processed_count", 0),
            "processed_active_candidate_count": pipeline.get(
                "processed_candidate_count", 0
            ),
            "pending_bucket_counts": pipeline.get("pending_bucket_counts", {}),
            "freshness": pipeline.get("freshness", {}),
            "source_count": pipeline.get("source_count", 0),
            "source_inventory": [
                {
                    "source_id": _clean(item.get("source_id"), 100),
                    "source_label": _clean(item.get("source_label"), 100),
                    "pending_count": item.get("pending_count", 0),
                    "processed_count": item.get("processed_count", 0),
                    "total_count": item.get("total_count", 0),
                }
                for item in (pipeline.get("source_inventory") or [])[:10]
                if isinstance(item, dict)
            ],
        },
        "identity": {
            "status": identity.get("status"),
            "record_count": identity.get("record_count", 0),
            "resolved_count": identity.get("resolved_count", 0),
            "elapsed_seconds": identity.get("elapsed_seconds", 0),
            "error": _clean(identity.get("error"), MAX_ERROR_CHARS) or None,
        },
        "history_gate": {
            "checked": history_gate.get("checked", 0),
            "eligible": history_gate.get("eligible", 0),
            "excluded": history_gate.get("excluded", 0),
            "by_reason": history_gate.get("by_reason", {}),
            "pending_reconciliation": history_gate.get(
                "pending_reconciliation", {}
            ),
            "recommendation_cooldown": history_gate.get(
                "recommendation_cooldown", {}
            ),
            "examples": [
                {
                    "company": _clean(item.get("company"), 100),
                    "title": _clean(item.get("title"), 140),
                    "reason": _clean(item.get("reason"), 80),
                    "tracker_ids": [
                        _clean(value, 20) for value in (item.get("tracker_ids") or [])[:3]
                    ],
                }
                for item in (history_gate.get("examples") or [])[:3]
                if isinstance(item, dict)
            ],
        },
        "profile_evidence": {
            "status": profile.get("status"),
            "sources_verified": profile.get("sources_verified", False),
            "experience_years_min": profile.get("experience_years_min"),
            "target_levels": profile.get("target_levels", []),
            "facts": [
                {
                    "id": _clean(item.get("id"), 80),
                    "evidence": _clean(item.get("evidence"), 160),
                }
                for item in (profile.get("facts") or [])[:4]
                if isinstance(item, dict)
            ],
            "error": _clean(profile.get("error"), MAX_ERROR_CHARS) or None,
        },
        "application_status": application_status,
        "no_response_closure": _no_response_compact(no_response_closure),
        "candidates": [
            {
                key: candidate.get(key)
                for key in (
                    "url",
                    "title",
                    "company",
                    "location",
                    "source_id",
                    "source_label",
                    "direct_verified",
                    "liveness",
                    "recommendation_eligible",
                    "verification_method",
                    "candidate_origin",
                    "posting_date",
                    "first_seen",
                    "evaluation_id",
                    "evaluation_score_text",
                    "actionability",
                    "actionability_reasons",
                    "search_firm_or_hidden_employer",
                    "profile_fit_score",
                    "profile_fit_reasons",
                    "profile_review_required",
                    "recommendation_cooldown",
                    "recommendation_cooldown_until",
                    "profile_match",
                    "profile_evidence_ids",
                    "description",
                )
                if candidate.get(key) not in (None, "", [], {})
            }
            for candidate in candidates
        ],
    }
    _deduplicate_compact_apply_results(payload)
    statuses = [
        payload["mail_audit"]["status"],
        payload["linkedin"]["status"],
        payload["collector"]["status"],
        payload["liveness"]["status"],
        payload["identity"]["status"],
    ]
    apply_status = (
        (payload["mail_audit"].get("reconciliation") or {})
        .get("apply_result", {})
        .get("status")
    )
    if apply_status in {"error", "partial"}:
        statuses.append(apply_status)
    review_queue_status = payload["mail_audit"].get("review_queue", {}).get("status")
    if review_queue_status == "error":
        statuses.append("partial")
    closure_status = payload["no_response_closure"].get("status")
    if mode == "apply" and closure_status in {
        "blocked_mail_audit",
        "error",
        "partial",
    }:
        statuses.append("partial")
    if any(status in {"error", "timeout", "partial"} for status in statuses):
        payload["overall_status"] = "partial"

    # Reserve room for the protected-file audit appended by main().
    initial_budget = max(1024, max_bytes - 1024)
    size = _bound_compact_payload(payload, initial_budget)
    if size > initial_budget:
        raise ValueError(
            f"Compact payload core exceeds {initial_budget} byte pre-audit budget: {size}"
        )
    return payload, size


def diagnostic_artifact_filename(mode: str, timestamp: str) -> str:
    """Return a diagnostic artifact name that identifies the execution mode."""
    if mode not in {"dry-run", "apply"}:
        raise ValueError(f"Unsupported execution mode for artifact name: {mode}")
    return f"{mode}-{timestamp}.json"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated Career-Ops V2 staging pre-run")
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--linkedin-config", type=Path, default=DEFAULT_LINKEDIN_CONFIG)
    parser.add_argument("--profile-evidence", type=Path, default=DEFAULT_PROFILE_EVIDENCE)
    parser.add_argument("--mode", choices=("dry-run", "apply"), default=None)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--max-details", type=int, default=None)
    parser.add_argument("--include-mail", action="store_true")
    parser.add_argument("--include-collector", action="store_true")
    parser.add_argument(
        "--mail-payload",
        type=Path,
        default=None,
        help="Reuse a sanitized read-only Gmail audit instead of querying Gmail again",
    )
    parser.add_argument("--artifact-dir", type=Path, default=STAGING_ROOT / "artifacts")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    runtime = _json_load(args.runtime_config.resolve())
    run_id = datetime.now(UTC).strftime("career-ops-v2-%Y%m%dT%H%M%SZ")
    runtime = {**runtime, "_run_id": run_id}
    linkedin_config = _json_load(args.linkedin_config.resolve())
    mode = args.mode or str(runtime.get("activation_mode") or "dry-run")
    if mode == "apply":
        if runtime.get("activation_mode") != "apply" or os.environ.get("CAREER_OPS_V2_ENABLE_APPLY") != "1":
            raise SystemExit("Apply mode is locked pending production activation approval")
    protected_paths = [str(path) for path in runtime.get("protected_files") or []]
    before = snapshot_files(protected_paths)
    started = time.monotonic()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)

    supplied_mail = _json_load(args.mail_payload.resolve()) if args.mail_payload else None
    include_mail = bool(
        supplied_mail is not None
        or args.include_mail
        or mode == "apply"
        or runtime.get("include_mail_in_dry_run")
    )
    include_collector = bool(
        args.include_collector or mode == "apply" or runtime.get("include_collector_in_dry_run")
    )
    worker_count = 1 + int(include_mail and supplied_mail is None) + int(include_collector)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        direct_future = executor.submit(
            run_direct_linkedin,
            runtime,
            args.linkedin_config.resolve(),
            skip_network=args.skip_network,
            max_details=args.max_details,
        )
        mail_future = (
            executor.submit(run_legacy_component, runtime, "--mail-only")
            if include_mail and supplied_mail is None
            else None
        )
        collector_future = None
        if include_collector:
            if mode == "apply":
                collector_future = executor.submit(
                    run_legacy_component, runtime, "--collector-only"
                )
            else:
                collector_future = executor.submit(run_multi_board_preview, runtime)

        direct = direct_future.result()
        if supplied_mail is not None:
            mail = supplied_mail
        elif mail_future is not None:
            mail = mail_future.result()
        else:
            mail = {"status": "skipped_in_dry_run", "messages": [], "message_count": 0}
        if collector_future is not None:
            collector_raw = collector_future.result()
            project_guard = (
                collector_raw.get("project_guard")
                if isinstance(collector_raw.get("project_guard"), dict)
                else {}
            )
            collector = (
                collector_raw.get("collector")
                if mode == "apply" and isinstance(collector_raw.get("collector"), dict)
                else collector_raw
            )
            if mode == "apply" and isinstance(collector, dict):
                root_validation = validate_collector_project_root(
                    runtime,
                    collector,
                    project_guard,
                )
                collector = {
                    **collector,
                    **root_validation,
                    "dry_run": False,
                    "writes_enabled": bool(root_validation["root_verified"]),
                }
        else:
            collector = {"status": "skipped_in_dry_run", "summary": {}, "error": None}

    if mode == "apply" and collector.get("root_verified") is not True:
        blocked = {
            "schema_version": "career-ops-v2.root-guard.v1",
            "mode": mode,
            "run_id": run_id,
            "status": "blocked_project_root",
            "collector": collector,
        }
        blocked_path = args.artifact_dir / f"root-guard-{run_id.removeprefix('career-ops-v2-')}.json"
        blocked_path.write_text(
            json.dumps(blocked, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(blocked, ensure_ascii=False))
        return 2

    fallback = None
    if direct.get("fallback_required") and not args.skip_network:
        fallback = run_ddgs_fallback(runtime)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    direct_path = args.artifact_dir / f"linkedin-direct-{timestamp}.json"
    direct_path.write_text(json.dumps(direct, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ingest = run_direct_ingest_preview(runtime, direct_path, apply=mode == "apply")

    project_root = Path(runtime["production_project_root"])
    tracker = parse_application_tracker(project_root / "data" / "applications.md")
    pipeline = parse_pipeline(project_root / "data" / "pipeline.md")
    raw_processed_candidates = (
        pipeline.get("processed_candidates")
        if isinstance(pipeline.get("processed_candidates"), list)
        else []
    )
    raw_pipeline_candidates = (
        pipeline.get("pending") if isinstance(pipeline.get("pending"), list) else []
    )
    raw_direct_candidates = (
        direct.get("candidates") if isinstance(direct.get("candidates"), list) else []
    )
    freshness_stats = annotate_candidate_freshness(
        [raw_direct_candidates, raw_processed_candidates, raw_pipeline_candidates],
        load_scan_history_metadata(project_root / "data" / "scan-history.tsv"),
    )
    pipeline["freshness"] = freshness_stats
    recommendation_cooldown_days = _nonnegative_int(
        runtime.get("recommendation_cooldown_days"), 7
    )
    recommendation_cooldown_stats = annotate_recommendation_cooldown(
        [raw_direct_candidates, raw_processed_candidates, raw_pipeline_candidates],
        load_recommendation_history(_recommendation_history_path(runtime)),
        cooldown_days=recommendation_cooldown_days,
    )
    identity = annotate_identity_records(
        runtime,
        tracker,
        mail,
        [raw_direct_candidates, raw_processed_candidates, raw_pipeline_candidates],
    )
    mail = attach_tracker_matches(mail, tracker)
    apply_same_run_mail_history_blocks(
        mail,
        [raw_direct_candidates, raw_processed_candidates, raw_pipeline_candidates],
    )
    apply_mail_reconciliation_actions(
        mail,
        project_root=project_root,
        node_bin=Path(runtime["node_bin"]),
        mode=mode,
    )
    review_queue_setting = runtime.get("mail_review_queue_path")
    review_queue_path = (
        Path(str(review_queue_setting))
        if review_queue_setting
        else project_root / "data" / MAIL_REVIEW_QUEUE_FILENAME
    )
    if not review_queue_path.is_absolute():
        review_queue_path = project_root / review_queue_path
    update_mail_review_queue(
        mail,
        queue_path=review_queue_path,
        mode=mode,
    )
    # Explicit mail evidence always runs first. Re-read the canonical tracker
    # before considering no-response closure so a same-run rejection,
    # interview, response, or offer cannot be overwritten by the age rule.
    tracker = parse_application_tracker(project_root / "data" / "applications.md")
    no_response_closure = apply_no_response_closures(
        mail,
        tracker,
        project_root=project_root,
        node_bin=Path(runtime["node_bin"]),
        mode=mode,
        enabled=bool(runtime.get("enable_no_response_closure", True)),
        threshold_days=int(runtime.get("no_response_close_days") or 60),
        max_per_run=int(runtime.get("no_response_close_max_per_run") or 20),
    )
    # Automatic closures also use the canonical writer. Re-read again so
    # history gating and Slack reflect every successful change from this run.
    tracker = parse_application_tracker(project_root / "data" / "applications.md")
    history_pending_reconciliation = reconcile_history_excluded_pending(
        project_root / "data" / "pipeline.md",
        raw_pipeline_candidates,
        mode=mode,
    )
    moved_history_urls = set(history_pending_reconciliation.get("moved_urls") or [])
    if moved_history_urls:
        raw_pipeline_candidates = [
            candidate
            for candidate in raw_pipeline_candidates
            if str(candidate.get("url") or "") not in moved_history_urls
        ]
    if history_pending_reconciliation.get("moved_count"):
        # Refresh aggregate/source inventory state while retaining the identity
        # decisions already attached to the remaining candidate objects.
        pipeline = parse_pipeline(project_root / "data" / "pipeline.md")
    liveness_state = load_liveness_state(_liveness_state_path(runtime))
    liveness_state_stats = annotate_liveness_recency(
        [raw_processed_candidates, raw_pipeline_candidates],
        liveness_state,
        max_age_days=_nonnegative_int(runtime.get("liveness_cache_ttl_days"), 7),
    )
    application_status = build_application_status(tracker)
    profile = load_profile_evidence(args.profile_evidence.resolve())
    max_candidates = int(runtime.get("max_candidates") or 5)
    max_liveness_checks = max(
        max_candidates,
        int(runtime.get("max_liveness_checks") or max_candidates),
    )
    max_processed_candidates = max(
        0,
        min(
            int(runtime.get("max_processed_candidates") or 4),
            max_liveness_checks,
        ),
    )
    max_linkedin_rechecks = max(
        0,
        min(int(runtime.get("max_linkedin_rechecks") or 1), 2),
    )
    history_gate_stats: dict[str, Any] = {
        "checked": 0,
        "eligible": 0,
        "excluded": 0,
        "by_reason": {},
        "examples": [],
        "pending_reconciliation": {
            "status": history_pending_reconciliation.get("status"),
            "would_move_count": history_pending_reconciliation.get(
                "would_move_count", 0
            ),
            "moved_count": history_pending_reconciliation.get("moved_count", 0),
            "by_reason": history_pending_reconciliation.get("by_reason", {}),
        },
        "recommendation_cooldown": {
            **recommendation_cooldown_stats,
            "days": recommendation_cooldown_days,
        },
    }
    direct_candidates = select_candidates(
        verified_direct_records(direct.get("candidates")),
        [],
        linkedin_config,
        limit=max_candidates,
        tracker=tracker,
        history_gate_stats=history_gate_stats,
    )
    # A historical LinkedIn row does not carry the direct-page evidence used by
    # V2. It may re-enter only when the current direct collector finds it again.
    non_linkedin_processed = [
        candidate
        for candidate in raw_processed_candidates
        if isinstance(candidate, dict)
        and candidate.get("source_id") != "linkedin"
        and candidate.get("_cached_liveness_expired") is not True
    ]
    processed_candidates = select_candidates(
        [],
        non_linkedin_processed,
        linkedin_config,
        limit=max_processed_candidates,
        tracker=tracker,
        history_gate_stats=history_gate_stats,
    ) if max_processed_candidates else []
    # LinkedIn is already covered by the bounded direct-page path. Do not spend
    # generic liveness slots on inherited LinkedIn rows that cannot establish
    # the same direct verification semantics.
    non_linkedin_pipeline = [
        candidate
        for candidate in raw_pipeline_candidates
        if isinstance(candidate, dict)
        and candidate.get("source_id") != "linkedin"
        and candidate.get("_cached_liveness_expired") is not True
    ]
    linkedin_recheck_candidates = [
        candidate
        for candidate in raw_pipeline_candidates
        if isinstance(candidate, dict)
        and candidate.get("source_id") == "linkedin"
        and candidate.get("candidate_origin") == "linkedin_pending_recheck"
    ]
    selected_linkedin_rechecks = select_candidates(
        [],
        linkedin_recheck_candidates,
        linkedin_config,
        limit=max_linkedin_rechecks,
        tracker=tracker,
        history_gate_stats=history_gate_stats,
    ) if max_linkedin_rechecks else []
    pipeline_candidates = select_candidates(
        [],
        non_linkedin_pipeline,
        linkedin_config,
        limit=max_liveness_checks,
        tracker=tracker,
        history_gate_stats=history_gate_stats,
    )
    preliminary_candidates: list[dict[str, Any]] = []
    seen_candidate_instances: set[str] = set()
    seen_candidate_clusters: set[str] = set()
    # Fresh direct and pending records consume the bounded liveness budget
    # before older Processed evaluations. Recommendation cooldown and explicit
    # posted/first-seen dates keep yesterday's result from crowding out a new one.
    ordered_candidates = prioritize_non_cooldown([
        *direct_candidates,
        *selected_linkedin_rechecks,
        *pipeline_candidates,
        *processed_candidates,
    ])
    for candidate in ordered_candidates:
        instance_key = str(candidate.get("listing_instance_id") or candidate.get("url") or "")
        cluster_key = str(
            candidate.get("posting_cluster_id")
            or f"{candidate.get('company')}::{candidate.get('title')}"
        )
        if instance_key in seen_candidate_instances or cluster_key in seen_candidate_clusters:
            continue
        seen_candidate_instances.add(instance_key)
        seen_candidate_clusters.add(cluster_key)
        preliminary_candidates.append(candidate)
    liveness = run_liveness_precheck(
        runtime,
        preliminary_candidates,
        max_checks=max_liveness_checks,
        skip_network=args.skip_network,
    )
    liveness["state"] = {
        **persist_liveness_state(runtime, liveness, mode=mode),
        **liveness_state_stats,
    }
    candidates = apply_liveness_results(
        preliminary_candidates,
        liveness,
        limit=max_candidates,
    )
    candidates = attach_profile_matches(candidates, profile)
    compact, compact_bytes = build_compact_payload(
        mode=mode,
        mail=mail,
        direct=direct,
        fallback=fallback,
        ingest=ingest,
        liveness=liveness,
        collector=collector,
        pipeline=pipeline,
        profile=profile,
        candidates=candidates,
        application_status=application_status,
        no_response_closure=no_response_closure,
        max_bytes=int(runtime.get("max_compact_payload_bytes") or 10000),
        history_gate=history_gate_stats,
        identity=identity,
    )

    after = snapshot_files(protected_paths)
    changed = changed_snapshots(before, after)
    file_audit = build_file_audit(
        mode=mode,
        protected_paths=protected_paths,
        changed=changed,
        runtime=runtime,
    )
    # Bound only the compact copy. The diagnostic keeps the full changed-path
    # list and verifier output for post-run evidence.
    compact["file_audit"] = json.loads(json.dumps(file_audit, ensure_ascii=False))
    if mode == "dry-run" and changed:
        compact["overall_status"] = "error"
        compact["dry_run_violation"] = changed
    elif mode == "apply" and not file_audit.get("integrity_verified"):
        compact["overall_status"] = "partial"
    compact_bytes = _bound_compact_payload(
        compact,
        int(runtime.get("max_compact_payload_bytes") or 10000),
    )
    if compact_bytes > int(runtime.get("max_compact_payload_bytes") or 10000):
        raise ValueError(
            f"Compact payload exceeds configured limit after protection result: {compact_bytes}"
        )

    diagnostic = {
        "schema_version": "career-ops-v2.diagnostic.v1",
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "compact_payload_bytes": compact_bytes,
        "protected_before": before,
        "protected_after": after,
        "protected_changed": changed,
        "file_audit": file_audit,
        "direct": direct,
        "fallback": fallback,
        "ingest": ingest,
        "liveness": liveness,
        "mail_status": _mail_compact(mail),
        "mail_usage": _mail_usage(mail),
        "mail_decisions": [
            {
                "message_id": _clean(item.get("message_id"), 300) or None,
                "thread_id": _clean(item.get("thread_id"), 300) or None,
                "date": _clean(item.get("date"), 80),
                "subject": _clean(item.get("subject"), 240),
                "evidenced_status": item.get("evidenced_status"),
                "evidence_company": item.get("evidence_company"),
                "evidence_role": item.get("evidence_role"),
                "tracker_matches": item.get("tracker_matches") or [],
                "reconciliation_action": item.get("reconciliation_action"),
                "decision_object": item.get("decision_object") or {},
                "identity_decision": item.get("_identity_decision") or {},
            }
            for item in (_mail_audit_object(mail).get("messages") or [])
            if isinstance(item, dict)
        ],
        "no_response_closure": no_response_closure,
        "history_pending_reconciliation": history_pending_reconciliation,
        "collector": collector,
        "identity": identity,
        "candidate_decisions": candidates,
        "profile_evidence": profile,
        "tracker_count": tracker.get("count", 0),
        "pipeline_counts": {
            "pending": pipeline.get("pending_count", 0),
            "processed": pipeline.get("processed_count", 0),
            "processed_active_candidates": pipeline.get(
                "processed_candidate_count", 0
            ),
            "sources": pipeline.get("source_inventory", []),
        },
        "compact_payload": compact,
    }
    diagnostic_path = args.artifact_dir / diagnostic_artifact_filename(mode, timestamp)
    diagnostic_path.write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    if mode == "dry-run" and changed:
        print(f"Dry-run changed protected files: {', '.join(changed)}", file=sys.stderr)
        return 1
    return 0 if compact.get("overall_status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
