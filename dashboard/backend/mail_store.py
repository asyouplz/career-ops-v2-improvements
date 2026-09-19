#!/usr/bin/env python3
"""Private, idempotent mail evidence ingestion. No Gmail or outbound messaging access."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

from server import APIError, APPLICATION, Config, SITES, Store, aliases, atomic_json, canonical_url, clean, conflicting_posting, is_application, normalize, now, public_job, read_json, safe_url

KINDS = {'proposal_unanswered', 'pending', 'declined', 'withdrawn', 'applied', 'responded', 'interview', 'offer', 'rejected', 'hired'}
CANONICAL = {'declined': 'SKIP', 'withdrawn': 'Discarded', 'applied': 'Applied', 'responded': 'Responded', 'interview': 'Interview', 'offer': 'Offer', 'rejected': 'Rejected', 'hired': 'Hired'}
REASONS = {'proposal_unanswered': '제안 메일 미회신', 'pending': '지원보류', 'declined': '지원하지 않기로 결정', 'withdrawn': '지원철회'}
LIFECYCLE_KINDS = {'applied', 'responded', 'interview', 'offer', 'rejected', 'hired', 'withdrawn'}
PHASE = {'Evaluated': 0, 'Applied': 1, 'Responded': 2, 'Interview': 3, 'Offer': 4, 'Rejected': 5, 'Hired': 5, 'Discarded': 5, 'SKIP': 0}


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        if parsed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
            raise ValueError()
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (TypeError, ValueError):
        raise APIError(400, 'invalid_mail_date', '메일의 실제 발송 날짜를 확인해야 합니다.')


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get('version') != 1 or not isinstance(bundle.get('events'), list) or len(bundle['events']) > 20000 or not isinstance(bundle.get('run_id'), str):
        raise APIError(400, 'invalid_mail_bundle', '메일 동기화 결과의 형식을 확인해야 합니다.')
    events = []
    ids = set()
    for raw in bundle['events']:
        if not isinstance(raw, dict) or raw.get('kind') not in KINDS:
            raise APIError(400, 'invalid_mail_event', '지원 관련 메일 분류를 확인해야 합니다.')
        if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in ('event_id', 'message_id', 'thread_id', 'company', 'role', 'evidence_text')):
            raise APIError(400, 'mail_evidence_missing', '메일의 회사, 직무, 원문 근거가 필요합니다.')
        if len(raw['event_id']) > 500 or len(raw['message_id']) > 256 or len(raw['thread_id']) > 256:
            raise APIError(400, 'invalid_mail_identity', '메일 식별자를 확인해야 합니다.')
        if raw['event_id'] in ids:
            continue
        ids.add(raw['event_id'])
        mail_url = safe_url(raw.get('mail_url'))
        if not mail_url or urlsplit(mail_url).hostname != 'mail.google.com':
            raise APIError(400, 'invalid_mail_link', '원본 Gmail 대화 링크가 필요합니다.')
        event = {key: clean(raw.get(key), limit) for key, limit in {'event_id': 500, 'thread_id': 256, 'message_id': 256, 'company': 200, 'role': 400, 'kind': 80, 'evidence_text': 2000, 'reason': 1000, 'source_name': 80}.items()}
        event.update(event_at=timestamp(raw.get('event_at')), mail_url=mail_url, job_url=safe_url(raw.get('job_url')), source_name=raw.get('source_name') if raw.get('source_name') in {'headhunter_mail', 'company_mail'} else 'headhunter_mail', needs_review=bool(raw.get('needs_review')), apply=raw.get('apply') is not False)
        if raw.get('tracker_id') is not None:
            event['tracker_id'] = clean(raw['tracker_id'], 40)
        if raw.get('subject'):
            event['subject'] = clean(raw['subject'], 500)
        if raw.get('identity_source') == 'operator_reviewed':
            if not isinstance(raw.get('identity_evidence'), str) or not raw['identity_evidence'].strip():
                raise APIError(400, 'reviewed_identity_evidence_missing', '검토한 지원 기록 연결에는 별도 대조 근거가 필요합니다.')
            if event.get('tracker_id') is not None and (not event['tracker_id'].isdigit() or int(event['tracker_id']) < 1):
                raise APIError(400, 'reviewed_tracker_invalid', '검토한 지원 기록의 번호를 확인해 주세요.')
            event['identity_source'] = 'operator_reviewed'
            event['identity_evidence'] = clean(raw['identity_evidence'], 2000)
        events.append(event)
    return sorted(events, key=lambda event: (event['event_at'], event['event_id']))


def event_job(event):
    identity = event['thread_id'] + '\0' + normalize(event['company']) + '\0' + normalize(event['role'])
    if event.get('job_url'):
        identity += '\0' + canonical_url(event['job_url'])
    ident = 'mail_' + hashlib.sha256(identity.encode()).hexdigest()[:24]
    raw = {'id': ident, 'company': event['company'], 'title': event['role'], 'url': event['job_url'], 'site': event['source_name'] if not event['job_url'] else None, 'date': event['event_at'][:10], 'reason': event['reason'] or REASONS.get(event['kind'], ''), 'first_seen': event['event_at']}
    return public_job(raw)


def exact_identity(left, right):
    company_name = lambda value: re.sub(r'\s*([()])\s*', r'\1', normalize(value))
    return company_name(left.get('company')) == company_name(right.get('company')) and normalize(left.get('title') or left.get('role')) == normalize(right.get('title') or right.get('role')) and not conflicting_posting(left, right)


def explicit_company_aliases(value):
    """Only explicit Hangul/Latin parenthetical names prove bilingual company aliases."""
    value = normalize(value)
    match = re.fullmatch(r'([^()]+?)\s*\(([^()]+)\)', value)
    if not match:
        return {value}
    left, right = (part.strip() for part in match.groups())
    bilingual = (re.search(r'[가-힣]', left) and re.search(r'[a-z]', right)) or (re.search(r'[a-z]', left) and re.search(r'[가-힣]', right))
    latin_alias = right if re.search(r'[가-힣]', left) else left
    if not bilingual or min(len(left), len(right)) < 2 or len(re.sub('[^a-z]', '', latin_alias)) < 3 or right in {'inc', 'ltd', 'korea', '한국', '주식회사'}:
        return {value}
    return {value, left, right}


def paired_receipt_alias(event, candidate, peers):
    """A second near-simultaneous receipt can link an explicit alias to that day's row."""
    if event.get('kind') != 'applied' or event.get('needs_review') or '접수' not in event.get('reason', '') or candidate.get('tracker_id') is None or candidate.get('date') != event['event_at'][:10]:
        return False
    if normalize(event['role']) != normalize(candidate.get('title') or candidate.get('role')) or conflicting_posting(event_job(event), candidate):
        return False
    if not explicit_company_aliases(event['company']).intersection(explicit_company_aliases(candidate['company'])):
        return False
    for peer in peers:
        if peer.get('event_id') == event['event_id'] or peer.get('kind') != 'applied' or peer.get('needs_review') or '접수' not in peer.get('reason', ''):
            continue
        if not exact_identity({'company': peer.get('company'), 'role': peer.get('role')}, candidate):
            continue
        if not explicit_company_aliases(event['company']).intersection(explicit_company_aliases(peer['company'])):
            continue
        try:
            elapsed = abs((dt.datetime.fromisoformat(event['event_at'].replace('Z', '+00:00')) - dt.datetime.fromisoformat(peer['event_at'].replace('Z', '+00:00'))).total_seconds())
        except (ValueError, TypeError):
            continue
        if elapsed <= 300:
            return True
    return False


def resolve_job(store, event, ledger, decisions, rows, exported, peer_events=None):
    job = event_job(event)
    if event.get('identity_source') == 'operator_reviewed' and event.get('identity_evidence') and event.get('tracker_id') is not None:
        reviewed_rows = [row for row in rows if str(row['num']) == str(event['tracker_id'])]
        if len(reviewed_rows) != 1:
            raise APIError(409, 'reviewed_tracker_missing', '대조한 기존 지원 기록을 찾을 수 없어 메일 연결을 중단했습니다.')
        reviewed_row = reviewed_rows[0]
        linked = store.tracker_job(reviewed_row)
        if conflicting_posting(job, linked):
            raise APIError(409, 'reviewed_identity_conflict', '메일과 기존 기록의 구체 공고 번호가 달라 연결하지 않았습니다.')
        linked['aliases'] = sorted(aliases(linked) | aliases(job))
        # Keep incoming and canonical identities separately in the ledger. The
        # explicit reviewed row controls state; it does not rewrite mail text.
        key, previous = store.matching_decision(linked, decisions['decisions'])
        return linked, key or linked['id'], previous, reviewed_row
    prior = [e for e in ledger['events'].values() if e.get('thread_id') == event['thread_id'] and normalize(e.get('company')) == normalize(event['company']) and normalize(e.get('role')) == normalize(event['role']) and e.get('job_id')]
    if prior:
        # Reuse a proven thread-position link across subsequent replies.
        latest = max(prior, key=lambda e: e['event_at'])
        key = latest['job_id']
        saved = decisions['decisions'].get(key, {}).get('job')
        current = next((j for j in exported if key in aliases(j) or aliases(j).intersection(latest.get('job_aliases', []))), None)
        prior_row = next((row for row in rows if latest.get('tracker_id') is not None and str(row['num']) == str(latest['tracker_id'])), None)
        prior_job = public_job(current or saved) if current or saved else store.tracker_job(prior_row) if prior_row else None
        # A repeated conversation can contain a different concrete requisition.
        # Never overwrite the incoming URL before checking that distinction.
        if prior_job is not None and not conflicting_posting(job, prior_job):
            job = prior_job
            job['id'] = key
            if latest.get('tracker_id') is not None:
                job['tracker_id'] = latest['tracker_id']
    candidates = [public_job(j) for j in exported]
    candidates += [public_job(d.get('job') or {'id': key}) for key, d in decisions['decisions'].items()]
    candidates += [store.tracker_job(r) for r in rows]
    # Merge duplicate representations, but never merge two concrete requisitions.
    matched = [j for j in candidates if aliases(job) & aliases(j) and not conflicting_posting(job, j)]
    if not matched:
        matched = [j for j in candidates if exact_identity(job, j)]
    alias_matched = False
    if not matched:
        peers = list(ledger['events'].values()) + list(peer_events or [])
        matched = [j for j in candidates if paired_receipt_alias(event, j, peers)]
        alias_matched = bool(matched)
    unique = []
    for candidate in matched:
        if not any(aliases(candidate) & aliases(old) or (candidate.get('tracker_id') is not None and str(candidate['tracker_id']) == str(old.get('tracker_id'))) for old in unique):
            unique.append(candidate)
    # A single canonical row and a single exact posting may represent one application.
    if len(unique) == 2 and sum(j.get('tracker_id') is not None for j in unique) == 1 and not conflicting_posting(*unique):
        posting = next(j for j in unique if j.get('tracker_id') is None)
        tracked = next(j for j in unique if j.get('tracker_id') is not None)
        if exact_identity(posting, tracked):
            posting = copy.deepcopy(posting)
            posting['tracker_id'] = tracked['tracker_id']
            posting['aliases'] = sorted(aliases(posting) | aliases(tracked))
            unique = [posting]
    if len(unique) == 1:
        job = copy.deepcopy(unique[0])
        if alias_matched:
            event['identity_source'] = 'explicit_bilingual_alias'
            event['identity_evidence'] = f'원문에 병기된 회사명 별칭을 대조: {event["company"]} / {job["company"]}. 동일한 전체 직무명과 지원일, 5분 이내 별도 접수 확인 메일을 함께 확인해 기존 지원 기록에 연결했습니다.'
    elif len(unique) > 1:
        event['needs_review'] = True
        event['identity_note'] = '같은 회사와 직무의 공고가 여러 개여서 연결 확인 필요'
        job = event_job(event)
    row = store.matched_row(job, rows) if len(unique) <= 1 else None
    if row and event.get('tracker_id') is not None and str(event['tracker_id']) != str(row['num']):
        event['needs_review'] = True
        row = None
    key, decision = store.matching_decision(job, decisions['decisions'])
    return job, key or job['id'], decision, row


def backup_mail_data(store, run_id):
    folder = store.c.data / 'dashboard-backups' / ('mail-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + hashlib.sha256(run_id.encode()).hexdigest()[:8])
    folder.mkdir(mode=0o700, parents=True)
    for path in (store.c.tracker, store.c.tracker.with_name('status-log.tsv'), store.c.decisions, store.c.data / 'dashboard-mail-ledger.json'):
        if path.exists():
            destination = folder / path.name
            shutil.copy2(path, destination)
            destination.chmod(0o600)


def canonical_dates(store):
    """Status log records event dates, so a backfill cannot reverse a later manual CLI change."""
    result = {}
    path = store.c.tracker.with_name('status-log.tsv')
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return result
    except (OSError, UnicodeError):
        raise APIError(503, 'status_log_unreadable', '지원 상태 변경 날짜를 확인하지 못해 메일 반영을 중단했습니다.')
    for line in lines:
        fields = line.split('\t')
        if len(fields) >= 4 and fields[0].isdigit() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', fields[1]):
            key = (fields[0], fields[3])
            result[key] = max(result.get(key, ''), fields[1])
    return result


def ingest(store, bundle, apply=False):
    events = validate_bundle(bundle)
    summary = {'ok': True, 'applied_count': 0, 'review_count': 0, 'processed_events': 0, 'duplicate_count': 0, 'errors': [], 'preview': not apply, 'changes': []}
    with store.lock() if apply else store.mutex:
        ledger = store.mail_ledger()
        data = store.decision_data()
        rows = store.tracker_rows()
        status_dates = canonical_dates(store)
        exported = store.export_data()['jobs']
        if apply and events:
            backup_mail_data(store, bundle['run_id'])
        for event in events:
            existing = ledger['events'].get(event['event_id'])
            if existing and existing.get('outcome') not in {'prepared', 'error'}:
                summary['duplicate_count'] += 1
                continue
            job, key, previous, row = resolve_job(store, event, ledger, data, rows, exported, peer_events=events)
            event.update(job_id=key, job_aliases=sorted(aliases(job)), imported_at=now(), applied=False)
            if row:
                event['tracker_id'] = row['num']
                job['tracker_id'] = row['num']
            target = 'excluded' if event['kind'] in {'declined', 'withdrawn'} else 'pending' if event['kind'] in {'pending', 'proposal_unanswered'} else 'applied'
            canonical = CANONICAL.get(event['kind'])
            effective_at = '' if (previous or {}).get('origin') == 'note' else (previous or {}).get('effective_at') or (previous or {}).get('updated_at', '')
            if not event['apply']:
                outcome = 'history_only'
            elif effective_at and event['event_at'] < effective_at:
                outcome = 'older_than_current_decision'
            elif row and event['event_at'][:10] < status_dates.get((str(row['num']), row['status']), ''):
                outcome = 'older_than_canonical_transition'
            elif row and row['status'] == 'SKIP' and event['kind'] == 'proposal_unanswered':
                outcome = 'prior_exclusion_preserved'
            elif event['needs_review'] and (row and is_application(row, previous)):
                outcome = 'review_preserves_application'
            elif row and is_application(row, previous) and event['kind'] in {'pending', 'proposal_unanswered', 'declined'}:
                outcome = 'application_preserved'
                event['needs_review'] = event['kind'] == 'declined'
            elif row and canonical and event['kind'] != 'withdrawn' and PHASE.get(row['status'], 0) > PHASE.get(canonical, 0):
                outcome = 'later_application_stage_preserved'
            elif previous and previous.get('withdrawn') and event['kind'] in {'pending', 'proposal_unanswered', 'declined'}:
                outcome = 'withdrawal_preserved'
            else:
                outcome = 'apply'
            if event['needs_review'] and outcome == 'apply':
                target, canonical = 'pending', None
            event['outcome'] = outcome
            summary['processed_events'] += 1
            summary['review_count'] += int(event['needs_review'])
            summary['changes'].append({'event_id': event['event_id'], 'job_id': key, 'company': job['company'], 'role': job['title'], 'target': target, 'canonical': canonical, 'outcome': outcome, 'needs_review': event['needs_review']})
            if not apply:
                ledger['events'][event['event_id']] = event
                if outcome == 'apply':
                    summary['applied_count'] += 1
                    data['decisions'][key] = {'id': key, 'job': job, 'status': target, 'effective_at': event['event_at'], 'origin': 'mail'}
                continue
            if outcome != 'apply':
                ledger['events'][event['event_id']] = event
                atomic_json(store.c.data / 'dashboard-mail-ledger.json', ledger)
                continue
            ledger['events'][event['event_id']] = dict(event, outcome='prepared')
            atomic_json(store.c.data / 'dashboard-mail-ledger.json', ledger)
            try:
                note = '메일 근거: ' + (event['reason'] or REASONS.get(event['kind'], event['kind'])) + '; mail-event:' + event['event_id'] + '; ' + event['mail_url']
                if event['kind'] == 'withdrawn':
                    note = '지원철회; 기존 지원 이력 유지; ' + note
                if canonical and not event['needs_review']:
                    if row:
                        if row['status'] != canonical:
                            row = store.set_canonical(row, canonical, note, event_date=event['event_at'][:10])
                    else:
                        row = store.add_canonical_record(job, note, rows, canonical, event['event_at'][:10])
                    rows = store.tracker_rows()
                    job['tracker_id'] = row['num']
                    job['canonical_status'] = row['status']
                    event['tracker_id'] = row['num']
                saved_at = now()
                record = {'id': key, 'status': target, 'note': (previous or {}).get('note', ''), 'reason': event['reason'] or REASONS.get(event['kind'], ''), 'updated_at': saved_at, 'effective_at': event['event_at'], 'origin': 'mail', 'aliases': sorted(aliases(job) | aliases(previous or {})), 'job': job, 'mail_event_id': event['event_id'], 'mail_source': event['source_name'], 'needs_review': event['needs_review'], 'ever_applied': bool((event['kind'] in LIFECYCLE_KINDS and not event['needs_review']) or (previous or {}).get('ever_applied') or (row and is_application(row, previous))), 'withdrawn': event['kind'] == 'withdrawn' and not event['needs_review']}
                if row:
                    record['tracker_id'] = row['num']
                if canonical == 'SKIP' and row:
                    record['previous_canonical_status'] = (previous or {}).get('previous_canonical_status', 'Evaluated')
                if not record['note']:
                    record['note'] = record['reason']
                data['decisions'][key] = record
                data['updated_at'] = saved_at
                atomic_json(store.c.decisions, data)
                event.update(applied=True, outcome='applied')
                summary['applied_count'] += 1
            except APIError as error:
                # Canonical writers refuse ambiguous merges before mutation. Keep a reviewable
                # mail-only item instead of changing a similar, unrelated application.
                if error.code == 'ambiguous_application_merge':
                    event.update(needs_review=True, outcome='ambiguous_identity')
                    summary['review_count'] += 1
                    data['decisions'][key] = {'id': key, 'status': 'pending', 'job': job, 'aliases': sorted(aliases(job)), 'note': '기존 지원 이력과 연결 확인 필요', 'reason': '기존 지원 이력과 연결 확인 필요', 'updated_at': now(), 'effective_at': event['event_at'], 'origin': 'mail', 'needs_review': True, 'mail_event_id': event['event_id'], 'ever_applied': False}
                    data['updated_at'] = now()
                    atomic_json(store.c.decisions, data)
                else:
                    event.update(outcome='error', error_code=error.code)
                    summary['errors'].append({'event_id': event['event_id'], 'code': error.code})
                    summary['ok'] = False
            ledger['events'][event['event_id']] = event
            atomic_json(store.c.data / 'dashboard-mail-ledger.json', ledger)
        if apply:
            ledger['runs'][bundle['run_id']] = {'completed_at': now(), 'complete': bundle.get('complete') is True and not summary['errors'] and not bundle.get('errors'), 'window_start': clean(bundle.get('window_start'), 100), 'window_end': clean(bundle.get('window_end'), 100), 'processed_events': summary['processed_events'], 'applied_count': summary['applied_count'], 'review_count': summary['review_count']}
            atomic_json(store.c.data / 'dashboard-mail-ledger.json', ledger)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = ingest(Store(Config()), read_json(args.bundle), args.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 1
    except APIError as error:
        print(json.dumps({'ok': False, 'code': error.code, 'error': error.message}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
