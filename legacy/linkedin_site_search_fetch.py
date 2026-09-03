#!/usr/bin/env python3
"""Bounded LinkedIn discovery through a public search index.

This collector never requests linkedin.com. It submits at most two configured
``site:linkedin.com/jobs/view`` queries to the configured DDGS search backend and
returns at most ten result cards per query. Field parsing and policy gates live
in ``linkedin-search-ingest.mjs``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from ddgs import DDGS

DEFAULT_PORTALS = Path(os.environ.get("CAREER_OPS_PORTALS") or Path(__file__).resolve().parents[1] / "engine" / "portals.yml")
MAX_QUERIES = 2
MAX_RESULTS_PER_QUERY = 10


def _clean(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())[:max_length]


def load_queries(portals_path: Path) -> list[str]:
    config = yaml.safe_load(portals_path.read_text(encoding="utf-8")) or {}
    raw_queries = config.get("search_queries") or []
    if not isinstance(raw_queries, list):
        raise ValueError("search_queries must be a list")

    queries: list[str] = []
    for entry in raw_queries:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        name = _clean(entry.get("name"), 100).lower()
        if not name.startswith("linkedin"):
            continue
        query = _clean(entry.get("query"), 500)
        if not query:
            continue
        if "site:linkedin.com/jobs/view" not in query.lower() and "site:kr.linkedin.com/jobs/view" not in query.lower():
            raise ValueError("LinkedIn query must be restricted to linkedin.com/jobs/view")
        queries.append(query)

    if len(queries) > MAX_QUERIES:
        raise ValueError(f"LinkedIn discovery allows at most {MAX_QUERIES} enabled queries")
    return queries


def fetch_search_results(
    queries: Iterable[str],
    *,
    client_factory: Callable[..., Any] = DDGS,
) -> dict[str, Any]:
    bounded_queries = list(queries)
    if len(bounded_queries) > MAX_QUERIES:
        raise ValueError(f"LinkedIn discovery allows at most {MAX_QUERIES} queries")
    if not bounded_queries:
        return {
            "status": "ok",
            "provider": "ddgs",
            "query_count": 0,
            "results": [],
            "error": None,
        }

    results: list[dict[str, Any]] = []
    with client_factory(timeout=15) as client:
        for query in bounded_queries:
            for index, hit in enumerate(client.text(query, max_results=MAX_RESULTS_PER_QUERY)):
                if index >= MAX_RESULTS_PER_QUERY:
                    break
                if not isinstance(hit, dict):
                    continue
                results.append(
                    {
                        "title": _clean(hit.get("title"), 500),
                        "url": _clean(hit.get("href") or hit.get("url"), 2000),
                        "description": _clean(hit.get("body"), 1000),
                        "position": index + 1,
                        "searched_query": _clean(query, 500),
                    }
                )

    return {
        "status": "ok",
        "provider": "ddgs",
        "query_count": len(bounded_queries),
        "results": results,
        "error": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded LinkedIn search-index discovery")
    parser.add_argument("--portals", type=Path, default=DEFAULT_PORTALS)
    args = parser.parse_args()
    try:
        payload = fetch_search_results(load_queries(args.portals))
    except Exception as exc:  # ddgs exposes provider-specific exception classes
        payload = {
            "status": "error",
            "provider": "ddgs",
            "query_count": 0,
            "results": [],
            "error": _clean(str(exc), 500) or exc.__class__.__name__,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
