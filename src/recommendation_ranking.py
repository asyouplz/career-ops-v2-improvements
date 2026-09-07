"""Pure, source-independent ranking from verified career evidence and job text.

Keyword overlap is a relevance signal, not confirmation that every job
requirement is met.  No personal career facts or portal preferences live here.
"""

from __future__ import annotations

from datetime import date
import math
import re
import unicodedata
from typing import Any


FACT_MATCH_POINTS = 4
MAX_SCORED_FACTS = 4
# Even a title with every positive signal and four matching facts stays below
# a non-entry title with no positive signals.  This is a soft rank penalty.
ENTRY_TITLE_PENALTY = 24


def _text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _terms(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({_text(term) for term in value if isinstance(term, str) and _text(term)})


def _has_term(text: str, term: str) -> bool:
    """Match ASCII tokens, while allowing Korean terms in compound words.

    SQL must not match NoSQL, PG must not match upgrade, and lead must not
    match misleading. ASCII abbreviations next to Korean text still match.
    """
    if not text or not term:
        return False
    return re.search(_term_pattern(term), text) is not None


def _term_pattern(term: str) -> str:
    before = r"(?<![a-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
    after = r"(?![a-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
    return before + re.escape(term) + after


def _matched_terms(text: str, terms: Any) -> list[str]:
    return [term for term in _terms(terms) if _has_term(text, term)]


def _mixed_seniority_title(title: str, policy: dict[str, Any], entry: list[str]) -> bool:
    """Recognize explicit level alternatives, not a junior manager title.

    Adjacent level enumerations (junior/senior, 신입 및 경력) are mixed. For
    longer role clauses, require an entry-free preferred role before a separate
    entry role. This does not waive the penalty on Junior Accounting Manager or
    Junior Accounting and Finance Manager merely because 'manager' appears.
    """
    if not entry:
        return False
    preferred = _terms(policy.get("preferred_title_terms"))
    # Preferred roles such as Manager may themselves be modified by Junior.
    # Handle those only as complete entry-free clauses below.
    non_entry = sorted(set(_terms(policy.get("secondary_title_terms")) + ["경력"]))
    separator = r"(?:\s*[/·ㆍ,&]\s*|\s+(?:and|or|및|또는)\s+)"
    for entry_term in entry:
        for other_term in non_entry:
            if entry_term == other_term:
                continue
            left, right = _term_pattern(entry_term), _term_pattern(other_term)
            if re.search(left + separator + right, title) or re.search(right + separator + left, title):
                return True

    clauses = re.split(r"\s+(?:and|or|및|또는)\s+|;", title)
    preferred_role_seen = False
    for clause in clauses:
        has_entry = any(_has_term(clause, term) for term in entry)
        if preferred_role_seen and has_entry:
            return True
        if not has_entry and any(_has_term(clause, term) for term in preferred):
            preferred_role_seen = True
    return False


def assess_profile_fit(
    candidate: dict[str, Any],
    config: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score verified fact overlap first, with bounded title-based signals.

    Each fact ID contributes once, regardless of repeated keywords or text.
    At most four facts contribute points, but all matched IDs/counts remain
    available for explanation. Missing/stale sources cannot supply fact points.
    Experience-year requirements are deliberately not inferred from free text.
    """
    title = _text(candidate.get("title"))
    description = _text(candidate.get("description"))
    policy = config.get("profile_fit")
    policy = policy if isinstance(policy, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    verified = profile.get("status") == "verified" and profile.get("sources_verified") is True
    status = "verified" if verified else str(profile.get("status") or "unavailable")
    if status == "verified":
        # A status label alone is not sufficient to trust career evidence.
        status = "verified" if verified else "unverified"

    leadership = _matched_terms(title, policy.get("preferred_title_terms"))
    senior = _matched_terms(title, policy.get("secondary_title_terms"))
    entry = _matched_terms(title, policy.get("deprioritized_title_terms"))
    mixed_seniority = _mixed_seniority_title(title, policy, entry)
    entry_only = bool(entry) and not mixed_seniority
    matched_ids: set[str] = set()
    matching_fields: set[str] = set()
    if verified:
        facts = profile.get("facts")
        for fact in facts if isinstance(facts, list) else []:
            if not isinstance(fact, dict):
                continue
            fact_id = str(fact.get("id") or "").strip()
            if not fact_id or not str(fact.get("evidence") or "").strip():
                continue
            fact_terms = _terms(fact.get("match_terms"))
            title_match = any(_has_term(title, term) for term in fact_terms)
            description_match = any(_has_term(description, term) for term in fact_terms)
            if title_match or description_match:
                matched_ids.add(fact_id)
                if title_match:
                    matching_fields.add("title")
                if description_match:
                    matching_fields.add("description")

    matched_fact_count = len(matched_ids)
    scored_fact_count = min(matched_fact_count, MAX_SCORED_FACTS)
    score = (
        scored_fact_count * FACT_MATCH_POINTS
        + (3 if leadership else 0)
        + (1 if senior else 0)
        - (ENTRY_TITLE_PENALTY if entry_only else 0)
    )
    reasons: list[str] = []
    if matched_fact_count:
        reasons.append("verified_profile_fact_terms")
    if not verified:
        reasons.append("profile_evidence_unverified")
    elif not matched_fact_count:
        reasons.append("profile_fact_match_not_found")
    if leadership:
        reasons.append("preferred_title")
    if senior:
        reasons.append("secondary_title")
    if mixed_seniority:
        reasons.append("mixed_seniority_title_requires_review")
    elif entry_only:
        reasons.append("deprioritized_title")
    review_required = bool(entry) or not bool(leadership) or not verified
    if review_required:
        reasons.append("title_only_seniority_uncertain")

    if not verified:
        rationale = "이력 근거 검증 필요"
    elif matched_fact_count:
        rationale = f"경력 근거 {matched_fact_count}개 관련 표현"
    else:
        rationale = "경력 근거 일치 표현 미확인"
    if mixed_seniority:
        rationale += " · 복수 직급 공고, 담당 직급 확인 필요"
    elif entry_only:
        rationale += " · 입문 직급 표현으로 후순위"
    elif leadership:
        rationale += " · 선호 직급 표현"
    elif senior:
        rationale += " · 시니어 직급 표현, 직급 확인 필요"
    else:
        rationale += " · 직급 확인 필요"

    scope = (
        "title_and_description"
        if matching_fields == {"title", "description"}
        else next(iter(matching_fields), "none")
    )
    return {
        "profile_fit_score": score,
        "profile_fit_reasons": reasons,
        "profile_review_required": review_required,
        "profile_evidence_ids": sorted(matched_ids),
        "profile_match": {
            "status": status,
            "rationale": rationale,
            "matched_fact_count": matched_fact_count,
            "scored_fact_count": scored_fact_count,
            "evidence_scope": scope,
            "seniority_evidence": "title_only",
            "seniority_classification": "mixed" if mixed_seniority else "entry_only" if entry_only else "unspecified",
            "experience_requirements": "not_assessed",
        },
    }


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _posting_ordinal(candidate: dict[str, Any]) -> int:
    """Use only a valid actual posting date; discovery dates never substitute."""
    value = str(candidate.get("posting_date") or "").strip()
    try:
        return date.fromisoformat(value[:10]).toordinal() if value else 0
    except ValueError:
        return 0


def candidate_fit_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Shared ascending order after history/eligibility/cooldown gates.

    Career relevance precedes review/evidence strength and earlier evaluation.
    Actual posting dates break equal-fit/evidence ties. A missing date is not
    represented as a newly posted job, and portal identity is never a signal.
    """
    return (
        -_number(candidate.get("profile_fit_score")),
        1 if candidate.get("profile_review_required") else 0,
        -_number(candidate.get("actionability_score")),
        -_number(candidate.get("evaluation_score")),
        0 if candidate.get("direct_verified") and candidate.get("liveness") == "active" else 1,
        0 if candidate.get("liveness") == "active" else 1,
        -_posting_ordinal(candidate),
        _text(candidate.get("company")),
        _text(candidate.get("title")),
        str(candidate.get("url") or ""),
        str(candidate.get("listing_instance_id") or ""),
        str(candidate.get("evaluation_id") or ""),
    )
