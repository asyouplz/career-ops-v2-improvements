#!/usr/bin/env python3
"""Deterministic Slack root/thread renderer and delivery helper for V2."""

from __future__ import annotations

from datetime import datetime
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


STATUS_LABELS = {
    "Applied": "지원중",
    "Responded": "회신",
    "Interview": "면접",
    "Offer": "오퍼",
    "Hired": "합격",
    "Rejected": "불합격",
    "Evaluated": "평가",
    "Discarded": "제외",
    "SKIP": "제외",
}
ORIGIN_LABELS = {
    "processed_active": "기존 평가 재확인",
    "linkedin_direct_new": "신규 발견",
    "pending_new": "신규 발견",
    "pending_backlog": "기존 수집 재확인",
    "linkedin_pending_recheck": "기존 LinkedIn 재확인",
}
CLOSURE_STATUS_LABELS = {
    "ok": "완료",
    "no_changes": "변경 없음",
    "dry_run": "미리보기",
    "blocked_mail_audit": "메일 확인 실패로 보류",
    "partial": "일부 반영",
    "error": "확인 필요",
    "disabled": "비활성",
}


class SlackDeliveryError(RuntimeError):
    """Raised when root or thread delivery is not explicitly confirmed."""


def _text(value: Any, limit: int = 1000) -> str:
    cleaned = " ".join(str(value or "").split())[:limit]
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_url(value: Any) -> str | None:
    raw = "".join(str(value or "").split())
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return raw.replace("|", "%7C").replace("<", "%3C").replace(">", "%3E")


def _report_date(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _status_count(status: dict[str, Any], name: str) -> int:
    counts = status.get("status_counts") if isinstance(status, dict) else {}
    return int((counts or {}).get(name, 0) or 0)


def _candidate_lines(
    payload: dict[str, Any], *, max_candidates: int = 5
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    rendered_urls: list[str] = []
    if max_candidates <= 0:
        return lines, rendered_urls
    candidates = payload.get("candidates") if isinstance(payload, dict) else []
    eligible_index = 0
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if (
            candidate.get("recommendation_eligible") is not True
            or candidate.get("liveness") != "active"
        ):
            continue
        url = _safe_url(candidate.get("url"))
        if not url:
            continue
        eligible_index += 1
        company = _text(candidate.get("company"), 100)
        role = _text(candidate.get("title"), 160)
        location = _text(candidate.get("location"), 60) or "근무지 미표기"
        source = _text(candidate.get("source_label"), 60) or "출처 미확인"
        origin = ORIGIN_LABELS.get(
            str(candidate.get("candidate_origin") or ""), "현재 공고 재확인"
        )
        score = _text(candidate.get("evaluation_score_text"), 20)
        evidence = " · ".join(item for item in (score, source, origin) if item)
        lines.append(f"{eligible_index}. <{url}|*{company} — {role}*>")
        posting_date = _text(candidate.get("posting_date"), 20)
        first_seen = _text(candidate.get("first_seen"), 20)
        date_evidence = (
            f"게시 {posting_date}"
            if posting_date
            else f"최초 확인 {first_seen}"
            if first_seen
            else ""
        )
        review_label = (
            "연차·직급 확인 필요"
            if candidate.get("profile_review_required")
            else "지원 검토 권고"
        )
        details = " · ".join(
            item for item in (location, date_evidence, evidence, review_label) if item
        )
        lines.append(f"   {details}")
        rendered_urls.append(url)
        if eligible_index >= max(0, int(max_candidates)):
            break
    return lines, rendered_urls


def _active_company_summary(active: list[dict[str, Any]], *, max_chars: int = 700) -> str:
    """Keep the current-company list visible in the root without repeating roles."""
    companies: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for item in active:
        company = _text(item.get("company"), 48) or "회사 미상"
        key = company.casefold()
        if key in seen:
            continue
        candidate = " · ".join([*companies, company])
        if len(candidate) > max_chars:
            omitted += 1
            continue
        seen.add(key)
        companies.append(company)
    if omitted:
        companies.append(f"외 {omitted}개사")
    return " · ".join(companies)


def _active_line(item: dict[str, Any], *, include_id: bool = False) -> str:
    date = _text(item.get("date"), 20) or "날짜 미상"
    company = _text(item.get("company"), 100)
    role = _text(item.get("role"), 150)
    status = STATUS_LABELS.get(str(item.get("status") or ""), _text(item.get("status"), 30))
    row_id = f"#{_text(item.get('id'), 20)} · " if include_id else ""
    return f"• {row_id}{date} · {company} — {role} · {status}"


def _resolved_applied_results(reconciliation: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve compact apply-result references against canonical actions."""
    actions = [
        item
        for item in (reconciliation.get("actions") or [])
        if isinstance(item, dict)
    ]
    resolved: list[dict[str, Any]] = []
    apply_result = reconciliation.get("apply_result") or {}
    for item in apply_result.get("applied") or []:
        if not isinstance(item, dict):
            continue
        action_index = item.get("action_index")
        if (
            isinstance(action_index, int)
            and not isinstance(action_index, bool)
            and 0 <= action_index < len(actions)
        ):
            action = actions[action_index]
            combined = {**action, **item}
            combined["old_status"] = item.get("old_status") or action.get(
                "current_status"
            )
            combined["new_status"] = item.get("new_status") or action.get(
                "new_status"
            )
            resolved.append(combined)
        else:
            resolved.append(dict(item))
    return resolved


def _render_root(
    payload: dict[str, Any],
    *,
    root_active_limit: int,
    root_max_chars: int,
    prefix: str | None,
) -> tuple[str, list[str]]:
    status = payload.get("application_status") or {}
    active = [item for item in (status.get("active") or []) if isinstance(item, dict)]
    preview_limit = max(0, min(int(root_active_limit), len(active), 8))
    candidate_limit = 5
    report_date = _report_date(payload.get("generated_at"))
    no_response_closed = int(status.get("no_response_closed_count", 0) or 0)
    other_excluded = status.get("other_excluded_count")
    if other_excluded is None:
        other_excluded = (
            _status_count(status, "Discarded")
            + _status_count(status, "SKIP")
            - no_response_closed
        )
    other_excluded = max(0, int(other_excluded or 0))

    def compose(active_limit: int, job_limit: int) -> tuple[str, list[str]]:
        candidates, rendered_urls = _candidate_lines(
            payload, max_candidates=job_limit
        )
        lines: list[str] = []
        if prefix:
            lines.extend([f"*{_text(prefix, 160)}*", ""])
        lines.extend([f"*채용 지원 리포트 · {report_date}*", "", "*지원 대상*"])
        lines.extend(candidates or ["오늘 지원 검토 권고 공고 없음"])
        lines.extend(
            [
                "",
                "*현재 지원 현황*",
                (
                    f"지원중 {int(status.get('active_count', 0) or 0)} · "
                    f"면접 {_status_count(status, 'Interview')} · "
                    f"오퍼 {_status_count(status, 'Offer')} · "
                    f"불합격 {_status_count(status, 'Rejected')} · "
                    f"평가 {_status_count(status, 'Evaluated')} · "
                    f"무응답종료 {no_response_closed} · "
                    f"기타제외 {other_excluded}"
                ),
            ]
        )
        if active:
            lines.append("지원중 회사: " + _active_company_summary(active))
        if active:
            lines.append(f"최근 지원 {min(active_limit, len(active))}건:")
            lines.extend(_active_line(item) for item in active[:active_limit])
            remaining = max(
                0,
                int(status.get("active_count", len(active)) or 0) - active_limit,
            )
            if remaining:
                lines.append(f"• 외 {remaining}건은 첫 번째 댓글의 전체 지원중 목록 참조")
        else:
            lines.append("현재 지원중으로 기록된 항목 없음")
        lines.extend(["", "_상태변경 상세와 오늘 실행 진단은 댓글로 분리했습니다._"])
        return "\n".join(lines), rendered_urls

    root, rendered_urls = compose(preview_limit, candidate_limit)
    while len(root) > root_max_chars and preview_limit > 0:
        preview_limit -= 1
        root, rendered_urls = compose(preview_limit, candidate_limit)
    while len(root) > root_max_chars and candidate_limit > 0:
        candidate_limit -= 1
        root, rendered_urls = compose(preview_limit, candidate_limit)
    if len(root) > root_max_chars:
        raise ValueError(
            f"Slack root exceeds {root_max_chars} characters after bounding: {len(root)}"
        )
    return root, rendered_urls


def _render_status_reply(payload: dict[str, Any]) -> str:
    status = payload.get("application_status") or {}
    mail = payload.get("mail_audit") or {}
    reconciliation = mail.get("reconciliation") or {}
    review_queue = mail.get("review_queue") or {}
    apply_result = reconciliation.get("apply_result") or {}
    closure = payload.get("no_response_closure") or {}
    active = [item for item in (status.get("active") or []) if isinstance(item, dict)]
    lines = ["*지원중 / 상태변경*", "", f"*현재 지원중 {int(status.get('active_count', 0) or 0)}건*"]
    if active:
        lines.extend(_active_line(item, include_id=True) for item in active)
        if status.get("active_truncated"):
            lines.append("• 목록 상한을 넘어 일부 항목 생략")
    else:
        lines.append("• 없음")

    lines.extend(["", "*오늘 상태변경*"])
    applied = _resolved_applied_results(reconciliation)
    for item in applied:
        verb = "추가" if item.get("action") == "add" else "변경"
        before = _text(item.get("old_status"), 30) or "신규"
        after = _text(item.get("new_status"), 30)
        lines.append(
            f"• {_text(item.get('company'), 100)} — {_text(item.get('role'), 150)}: "
            f"{before} → {after} ({verb})"
        )
    closure_applied = [
        item
        for item in (closure.get("actions") or [])
        if isinstance(item, dict) and item.get("outcome") == "applied"
    ]
    for item in closure_applied:
        lines.append(
            f"• {_text(item.get('company'), 100)} — {_text(item.get('role'), 150)}: "
            f"Applied → 무응답 종료 ({int(item.get('age_days', 0) or 0)}일, "
            "명시적 불합격 아님)"
        )
    if not applied and not closure_applied and int(reconciliation.get("action_count", 0) or 0):
        lines.append("• 변경 후보는 확인됐으나 tracker 반영 결과가 없음")
    elif not applied and not closure_applied:
        lines.append("• 변경 없음")

    failures = [item for item in (apply_result.get("failures") or []) if isinstance(item, dict)]
    for item in failures[:5]:
        lines.append(f"• 확인 필요: {_text(item.get('error'), 220)}")
    closure_failures = [
        item for item in (closure.get("failures") or []) if isinstance(item, dict)
    ]
    for item in closure_failures[:5]:
        lines.append(f"• 무응답 종료 확인 필요: {_text(item.get('error'), 200)}")
    lines.append(
        "• 메일 감사: "
        f"{'읽기 전용' if mail.get('readonly') else '상태 미확인'} · "
        f"최근 {mail.get('lookback_days', '?')}일 · {int(mail.get('message_count', 0) or 0)}건 · "
        f"반영 {int(apply_result.get('applied_count', 0) or 0)}건 · "
        f"검토 {int(reconciliation.get('review_count', 0) or 0)}건"
    )
    lines.append(
        "• 무응답 종료: "
        f"기준 {int(closure.get('threshold_days', 60) or 60)}일 · "
        f"최근 메일 보호 {int(closure.get('protected_recent_mail_count', 0) or 0)}건 · "
        f"반영 {int(closure.get('applied_count', 0) or 0)}건 · "
        "상태 "
        + CLOSURE_STATUS_LABELS.get(
            str(closure.get("status") or ""),
            _text(closure.get("status"), 40) or "미확인",
        )
    )
    open_reviews = [
        item for item in (review_queue.get("open_items") or []) if isinstance(item, dict)
    ]
    if int(review_queue.get("open_count", 0) or 0):
        lines.extend(["", f"*확인 필요 메일 {int(review_queue.get('open_count', 0) or 0)}건*"])
        for item in open_reviews[:5]:
            tracker_ids = ", ".join(
                f"#{_text(value, 20)}" for value in (item.get("tracker_ids") or [])[:3]
            )
            suffix = f" · {tracker_ids}" if tracker_ids else ""
            lines.append(f"• {_text(item.get('subject'), 180)}{suffix}")
        if review_queue.get("open_items_truncated"):
            lines.append("• 나머지 확인 필요 메일은 대기열에 유지")
    return "\n".join(lines)


def _render_execution_reply(payload: dict[str, Any]) -> str:
    linkedin = payload.get("linkedin") or {}
    filter_stats = linkedin.get("filter_stats") or {}
    ingest = linkedin.get("ingest_preview") or {}
    pipeline = payload.get("pipeline") or {}
    liveness = payload.get("liveness") or {}
    history = payload.get("history_gate") or {}
    identity = payload.get("identity") or {}
    collector = payload.get("collector") or {}
    collector_summary = collector.get("summary") or {}
    collector_sources = (
        collector_summary.get("source_results")
        if isinstance(collector_summary.get("source_results"), dict)
        else {}
    )
    file_audit = payload.get("file_audit") or {}
    mail = payload.get("mail_audit") or {}
    reconciliation = mail.get("reconciliation") or {}
    review_queue = mail.get("review_queue") or {}
    closure = payload.get("no_response_closure") or {}
    mail_usage = mail.get("usage") if isinstance(mail.get("usage"), dict) else {}
    lines = [
        "*오늘 실행 요약*",
        "",
        (
            "• LinkedIn: "
            f"direct={_text(linkedin.get('status'), 30)} · "
            f"fallback={'사용' if linkedin.get('fallback_used') else '미사용'} · "
            f"수신 {int(filter_stats.get('received', 0) or 0)} · "
            f"필터 통과 {int(filter_stats.get('accepted', 0) or 0)} · "
            f"저장 {int(ingest.get('added', 0) or 0)}"
        ),
        (
            "• 전체 수집: "
            f"보드 {int(collector_summary.get('job_boards_scanned', 0) or 0)} · "
            f"공고 {int(collector_summary.get('total_jobs_found', 0) or 0)} · "
            f"제목 제외 {int(collector_summary.get('filtered_by_title', 0) or 0)} · "
            f"지역 제외 {int(collector_summary.get('filtered_by_location', 0) or 0)} · "
            f"중복 {int(collector_summary.get('duplicates', 0) or 0)}"
        ),
        (
            "• Pipeline: "
            f"Pending {int(pipeline.get('pending_count', 0) or 0)} · "
            f"Processed {int(pipeline.get('processed_count', 0) or 0)} · "
            f"활성 재검토 {int(pipeline.get('processed_active_candidate_count', 0) or 0)} · "
            f"소스 {int(pipeline.get('source_count', 0) or 0)}"
        ),
        (
            "• 공고 상태: "
            f"확인 {int(liveness.get('checked', 0) or 0)}/{int(liveness.get('requested', 0) or 0)} · "
            f"활성 {int(liveness.get('active', 0) or 0)} · "
            f"종료 {int(liveness.get('expired', 0) or 0)} · "
            f"불확실 {int(liveness.get('uncertain', 0) or 0)}"
        ),
        (
            "• 지원이력 게이트: "
            f"확인 {int(history.get('checked', 0) or 0)} · "
            f"제외 {int(history.get('excluded', 0) or 0)} · "
            + ", ".join(
                f"{_text(key, 80)} {int(value or 0)}"
                for key, value in (history.get("by_reason") or {}).items()
            )
        ),
        (
            "• 공통 식별: "
            f"{_text(identity.get('status'), 30)} · "
            f"{int(identity.get('resolved_count', 0) or 0)}/"
            f"{int(identity.get('record_count', 0) or 0)}건 · "
            f"{float(identity.get('elapsed_seconds', 0) or 0):.3f}초"
        ),
        (
            "• 메일: "
            f"{int(mail.get('message_count', 0) or 0)}건 · "
            f"상태변경 후보 {int(reconciliation.get('action_count', 0) or 0)} · "
            f"변경 없음 {int(reconciliation.get('unchanged_count', 0) or 0)} · "
            f"이번 검토 {int(reconciliation.get('review_count', 0) or 0)} · "
            f"대기 {int(review_queue.get('open_count', 0) or 0)}"
        ),
        (
            "• 무응답 종료: "
            f"기준 {int(closure.get('threshold_days', 60) or 60)}일 · "
            f"경과 {int(closure.get('aged_count', 0) or 0)} · "
            f"메일 보호 {int(closure.get('protected_recent_mail_count', 0) or 0)} · "
            f"대상 {int(closure.get('requested_count', 0) or 0)} · "
            f"반영 {int(closure.get('applied_count', 0) or 0)} · "
            f"이월 {int(closure.get('deferred_count', 0) or 0)}"
        ),
        (
            "• 파일 검증: "
            f"{_text(file_audit.get('status'), 60)} · "
            f"변경 {int(file_audit.get('changed_count', 0) or 0)}개 · "
            f"검증 {'통과' if file_audit.get('integrity_verified') else '확인 필요'}"
        ),
    ]
    source_labels = {
        "wanted": "원티드",
        "saramin": "사람인",
        "remember": "리멤버",
        "jobkorea": "잡코리아",
    }
    source_parts: list[str] = []
    for source_id in ("wanted", "saramin", "remember", "jobkorea"):
        counts = collector_sources.get(source_id)
        if not isinstance(counts, dict):
            continue
        other_filtered = sum(
            int(counts.get(key, 0) or 0)
            for key in (
                "filtered_tier",
                "filtered_posting_age",
                "filtered_posted_date",
                "filtered_salary",
                "filtered_content",
                "filtered_country_eligibility",
                "filtered_visa",
                "filtered_blacklist",
                "filtered_cooldown",
            )
        )
        source_parts.append(
            f"{source_labels[source_id]} 발견 {int(counts.get('found', 0) or 0)}"
            f"/활성 {int(counts.get('active', 0) or 0)}"
            f"/종료 {int(counts.get('expired', 0) or 0)}"
            f"/불확실 {int(counts.get('uncertain', 0) or 0)}"
            f"/지원버튼미확인 {int(counts.get('no_apply_control', 0) or 0)}"
            f"/제목제외 {int(counts.get('filtered_title', 0) or 0)}"
            f"/지역제외 {int(counts.get('filtered_location', 0) or 0)}"
            f"/기타제외 {other_filtered}"
            f"/중복 {int(counts.get('duplicates', 0) or 0)}"
            f"/주소오류 {int(counts.get('invalid', 0) or 0)}"
        )
    if source_parts:
        lines.insert(3, "• 플랫폼별: " + " · ".join(source_parts))
    if mail_usage.get("available"):
        prompt_tokens = int(mail_usage.get("prompt_tokens", 0) or 0)
        cached_tokens = int(mail_usage.get("cached_input_tokens", 0) or 0)
        cache_percent = (
            round(cached_tokens * 100 / prompt_tokens, 1) if prompt_tokens else 0
        )
        lines.append(
            "• 메일 토큰: "
            f"총 {int(mail_usage.get('total_tokens', 0) or 0):,} · "
            f"입력 {int(mail_usage.get('input_tokens', 0) or 0):,} · "
            f"캐시 {cached_tokens:,} ({cache_percent}%) · "
            f"출력 {int(mail_usage.get('output_tokens', 0) or 0):,} · "
            f"추론 {int(mail_usage.get('reasoning_output_tokens', 0) or 0):,}"
        )
    if payload.get("overall_status") != "ok":
        lines.append(f"• 전체 상태: {_text(payload.get('overall_status'), 40)} — 일부 항목 확인 필요")
    return "\n".join(lines)


def render_slack_bundle(
    payload: dict[str, Any],
    *,
    root_active_limit: int = 5,
    root_max_chars: int = 2400,
    prefix: str | None = None,
) -> dict[str, Any]:
    """Render one concise root message and two deterministic thread replies."""
    root, rendered_candidate_urls = _render_root(
        payload,
        root_active_limit=root_active_limit,
        root_max_chars=root_max_chars,
        prefix=prefix,
    )
    replies = [_render_status_reply(payload), _render_execution_reply(payload)]
    return {
        "schema_version": "career-ops-v2.slack-bundle.v1",
        "root": root,
        "thread_replies": replies,
        "lengths": {"root": len(root), "thread_replies": [len(item) for item in replies]},
        "rendered_candidate_urls": rendered_candidate_urls,
    }


def _send(
    *,
    hermes_bin: Path,
    target: str,
    message: str,
    runner: Any,
) -> dict[str, Any]:
    completed = runner(
        [str(hermes_bin), "send", "--json", "--to", target, message],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SlackDeliveryError("hermes send returned invalid JSON") from exc
    if completed.returncode != 0 or not payload.get("success") or payload.get("error"):
        detail = payload.get("error") or completed.stderr or "delivery was not confirmed"
        raise SlackDeliveryError(_text(detail, 300))
    return payload


def deliver_slack_bundle(
    bundle: dict[str, Any],
    *,
    hermes_bin: Path,
    target: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Post the root, then attach every detail section to that exact thread."""
    if not re.fullmatch(r"slack:[CGD][A-Z0-9]{8,}", target):
        raise ValueError("Slack root target must be slack:<channel_id> without a thread ID")
    if not hermes_bin.is_file() and runner is subprocess.run:
        raise ValueError(f"Hermes executable unavailable: {hermes_bin}")
    root_result = _send(
        hermes_bin=hermes_bin,
        target=target,
        message=str(bundle.get("root") or ""),
        runner=runner,
    )
    message_id = str(root_result.get("message_id") or "").strip()
    if not message_id:
        raise SlackDeliveryError("Slack root delivery returned no message_id")
    thread_target = f"{target}:{message_id}"
    reply_results = []
    reply_errors = []
    for index, reply in enumerate(bundle.get("thread_replies") or []):
        try:
            reply_results.append(_send(
                hermes_bin=hermes_bin,
                target=thread_target,
                message=str(reply),
                runner=runner,
            ))
        except SlackDeliveryError as exc:
            reply_errors.append({"index": index, "error": _text(exc, 300)})
    return {
        "status": "partial" if reply_errors else "ok",
        "target": target,
        "message_id": message_id,
        "thread_target": thread_target,
        "reply_count": len(reply_results),
        "failed_reply_count": len(reply_errors),
        "reply_errors": reply_errors,
        "reply_message_ids": [item.get("message_id") for item in reply_results],
    }
