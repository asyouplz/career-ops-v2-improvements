#!/usr/bin/env python3
"""Initialize local settings and empty operational records without overwriting them."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"


def create_local(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        path.chmod(0o600)
        print(f"Created {relative}")
    except FileExistsError:
        print(f"Kept existing {relative}")


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=shutil.which("node") or "node")
    args = parser.parse_args()
    if not (ENGINE / "scan.mjs").is_file():
        raise SystemExit("Bundled engine is missing; download the complete repository")
    runtime = json.loads((ROOT / "config/runtime.example.json").read_text(encoding="utf-8"))
    runtime.update({
        "production_project_root": str(ENGINE),
        "production_hermes_home": os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
        "node_bin": str(Path(args.node).resolve()),
        "python_bin": sys.executable,
        "codex_bin": shutil.which("codex") or "codex",
        "legacy_prerun_script": str(ROOT / "legacy/career_ops_daily_scan.py"),
        "legacy_ddgs_script": str(ROOT / "legacy/linkedin_site_search_fetch.py"),
        "identity_resolver_script": str(ENGINE / "identity-resolver.mjs"),
        "mail_review_queue_path": str(ENGINE / "data/mail-review-queue.json"),
        "recommendation_history_path": str(ENGINE / "data/recommendation-history.json"),
    })
    runtime["slack_delivery"]["hermes_bin"] = shutil.which("hermes") or "hermes"
    create_local("config/runtime.json", json.dumps(runtime, ensure_ascii=False, indent=2) + "\n")
    for name in ("linkedin_queries", "profile_evidence"):
        create_local(f"config/{name}.json", (ROOT / f"config/{name}.example.json").read_text(encoding="utf-8"))
    create_local("engine/portals.yml", (ROOT / "config/portals.example.yml").read_text(encoding="utf-8"))
    create_local("engine/config/profile.yml", (ROOT / "config/profile.example.yml").read_text(encoding="utf-8"))
    create_local("engine/cv.md", "# Resume\n\n")
    create_local("engine/data/applications.md", "# Applications\n\n| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n|---|------|---------|------|-------|--------|-----|--------|-------|\n")
    create_local("engine/data/pipeline.md", "# Pipeline\n\n## Pending\n\n## Processed\n\n")
    create_local("engine/data/scan-history.tsv", "date\turl\tcompany\ttitle\tresult\n")
    for directory in ("engine/reports", "engine/batch/tracker-additions", "artifacts"):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    print("Ready. Add your own sources and filters in engine/portals.yml. Fresh settings disable mail and Slack; existing settings were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
