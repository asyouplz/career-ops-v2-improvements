#!/usr/bin/env python3
"""Restartable Gmail synchronization. Preview never writes canonical/dashboard data."""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from classifier import classify_thread
from provider import CodexGmail, ProviderError

UTC = dt.timezone.utc
TERMS = '{포지션 헤드헌터 헤드헌팅 채용 지원서 이력서 면접 인터뷰 오퍼 합격 불합격 "지원 접수" "지원 철회" "전형 결과" "application received" "thank you for applying" "career opportunity" "interview invitation" "offer letter"}'
PHASE_LABELS = {'restoring_cache': '저장된 대화 확인', 'search_messages': '지원 관련 메일 검색', 'list_history': 'Gmail 변경 이력 확인', 'read_recent_threads': '검색된 메일 대화 확인', 'read_known_threads': '기존 지원 대화 재확인', 'validate': '수집 결과 확인', 'apply': '지원 상태 반영', 'complete': '동기화 완료'}
ERROR_MESSAGES = {
    'gmail_api_disabled': '연결된 Google 프로젝트에서 Gmail API를 사용 설정한 뒤 다시 동기화해 주세요.',
    'mail_provider_rate_limited': 'Gmail 조회 한도에 도달해 잠시 중단했습니다. 잠시 후 다시 동기화하면 이어서 확인합니다.',
    'mail_account_mismatch': '연결된 Gmail 계정이 기존 지원 기록의 계정과 다릅니다. 기존 계정으로 연결한 뒤 다시 동기화해 주세요.',
    'mail_history_expired': '메일 변경 이력의 보관 기간이 지나 재동기화가 필요합니다. 다시 동기화해 주세요.',
    'connector_response_invalid': '메일 연결 응답을 확인하지 못해 중단했습니다. 다시 동기화하면 저장된 진행 지점부터 이어서 확인합니다.',
    'mail_auth_required': '메일 연결의 로그인을 다시 확인해야 합니다. 연결을 복구한 뒤 다시 동기화해 주세요.',
    'mail_provider_timeout': '메일 연결 응답을 기다리다 중단했습니다. 다시 동기화하면 이어서 확인합니다.',
    'mail_body_unreadable': '일부 메일 본문을 읽지 못해 전체 완료로 처리하지 않았습니다. 다시 동기화해 주세요.',
    'mail_pagination_invalid': '메일 검색의 다음 페이지를 확인하지 못해 중단했습니다. 다시 동기화하면 이어서 확인합니다.',
    'mail_coverage_incomplete': '아직 확인하지 못한 메일 대화가 남아 있어 중단했습니다. 다시 동기화하면 남은 대화를 확인합니다.',
    'mail_ingestion_failed': '메일은 확인했지만 지원 상태 반영을 완료하지 못했습니다. 다시 동기화하여 반영 결과를 확인해 주세요.',
    'mail_sync_failed': '메일 동기화를 완료하지 못했습니다. 저장된 진행 내역은 유지되며 다시 동기화하면 이어서 확인합니다.',
}


def now(): return dt.datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def precise_now(): return dt.datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def read(path, default):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def windows(start, end):
    output, cursor = [], start
    while cursor < end:
        year, month = cursor.year + (cursor.month == 12), cursor.month % 12 + 1
        stop = min(end, dt.datetime(year, month, 1, tzinfo=UTC))
        output.append({'start': cursor.isoformat(), 'end': stop.isoformat(), 'token': None, 'done': False})
        cursor = stop
    return output


def known_threads(data_dir):
    found = set()
    def visit(value):
        if isinstance(value, dict):
            tid = value.get('thread_id') or value.get('gmail_thread_id')
            if isinstance(tid, str) and tid: found.add(tid)
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    for name in ('dashboard-mail-ledger.json', 'mail-review-queue.json'):
        visit(read(data_dir / name, {}))
    # Preserve links already present in canonical notes, without reading unrelated files.
    import re
    try:
        text = (data_dir / 'applications.md').read_text()
        found.update(re.findall(r'mail\.google\.com/mail/(?:u/\d+/)?#(?:all|inbox)/([a-f0-9]{10,40})', text))
    except FileNotFoundError: pass
    return sorted(found)


def fresh_checkpoint(data_dir, status, end, since=None):
    if since: start = dt.datetime.fromisoformat(since.replace('Z', '+00:00')).astimezone(UTC)
    elif status.get('last_success_at'):
        start = dt.datetime.fromisoformat(status['last_success_at'].replace('Z', '+00:00')) - dt.timedelta(days=7)
    else: start = end - dt.timedelta(days=365)
    return {'version': 1, 'run_id': 'mail-' + end.strftime('%Y%m%dT%H%M%S') + '-' + os.urandom(4).hex(),
            'window_start': start.isoformat(), 'window_end': end.isoformat(), 'windows': windows(start, end),
            'message_ids': [], 'read_message_ids': [], 'read_thread_ids': [], 'known_thread_ids': known_threads(data_dir),
            'events': [], 'complete': False, 'phase': 'search', 'errors': []}


def bundle(checkpoint):
    return {key: checkpoint[key] for key in ('version', 'run_id', 'window_start', 'window_end', 'complete', 'events', 'errors')}


def progress_state(cp):
    wanted, read_messages = set(cp.get('message_ids', [])), set(cp.get('read_message_ids', []))
    known, read_threads = set(cp.get('known_thread_ids', [])), set(cp.get('read_thread_ids', []))
    remaining_messages = len(wanted - read_messages)
    remaining_known = len(known - read_threads)
    remaining_windows = sum(not window.get('done') for window in cp.get('windows', []))
    # Message search cannot know thread count until its messages have been read.
    total_threads = len(known | read_threads) if not remaining_windows and not remaining_messages else None
    detail = cp.get('phase_detail') or ('read_known_threads' if cp.get('phase') == 'read' and not remaining_messages else 'read_recent_threads' if cp.get('phase') == 'read' else 'search_messages' if cp.get('phase') == 'search' else 'validate' if cp.get('phase') == 'validated' else cp.get('phase'))
    progress = {'searched_windows': len(cp.get('windows', [])) - remaining_windows, 'total_windows': len(cp.get('windows', [])), 'found_messages': len(wanted), 'read_messages': len(read_messages), 'read_target_messages': len(wanted & read_messages), 'events': len(cp.get('events', [])), 'known_threads': len(known), 'processed_threads': len(read_threads), 'total_threads': total_threads, 'remaining_messages': remaining_messages, 'remaining_known_threads': remaining_known, 'remaining_windows': remaining_windows}
    return {'window_start': cp.get('window_start'), 'window_end': cp.get('window_end'), 'phase': cp.get('phase'), 'phase_detail': detail, 'phase_label': PHASE_LABELS.get(detail, '메일 동기화 준비'), 'processed_threads': len(read_threads), 'total_threads': total_threads, 'remaining_messages': remaining_messages, 'remaining_known_threads': remaining_known, 'remaining_windows': remaining_windows, 'progress': progress}


def describe_error(error, phase=None):
    raw = str(error)
    code = getattr(error, 'error_code', None) or getattr(error, 'code', None)
    if code not in ERROR_MESSAGES:
        text = raw.casefold()
        if phase == 'apply':
            code = 'mail_ingestion_failed'
        elif 'verified connector response' in text or 'verified connector' in text or 'connector response' in text or 'gmail response validation failed' in text:
            code = 'connector_response_invalid'
        elif any(value in text for value in ('unauthorized', 'token_expired', 'invalid_grant', 'authentication', '401')):
            code = 'mail_auth_required'
        elif isinstance(error, subprocess.TimeoutExpired) or 'timeout' in text or 'timed out' in text:
            code = 'mail_provider_timeout'
        elif 'pagination' in text or 'page token' in text:
            code = 'mail_pagination_invalid'
        elif 'ingestion' in text:
            code = 'mail_ingestion_failed'
        elif 'coverage' in text or 'did not cover' in text:
            code = 'mail_coverage_incomplete'
        elif error.__class__.__name__ == 'BodyError' or 'body' in text or 'mime' in text:
            code = 'mail_body_unreadable'
        else:
            code = 'mail_sync_failed'
    return code, ERROR_MESSAGES[code], raw[:4000]


def append_private_jsonl(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fd = None
            stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush(); os.fsync(stream.fileno())
    finally:
        if fd is not None: os.close(fd)


def append_run_history(state_dir, state):
    """Called under the shared worker lock; store only operational metadata."""
    fields = ('run_id', 'attempt_id', 'started_at', 'completed_at', 'status', 'window_start', 'window_end',
              'provider', 'sync_mode', 'history_pages', 'ignored_changes', 'changed_threads', 'cache_reused_threads',
              'phase', 'phase_detail', 'processed_threads', 'total_threads', 'remaining_messages',
              'remaining_known_threads', 'remaining_windows', 'applied_count', 'review_count', 'error_code',
              'duration_seconds', 'resumed', 'run_mode', 'initial_processed_threads', 'initial_total_threads',
              'initial_remaining_threads', 'initial_remaining_messages', 'initial_remaining_known_threads',
              'initial_remaining_windows', 'newly_processed_threads', 'phase_durations', 'provider_timing_summary')
    record = {key: state.get(key) for key in fields}
    progress = state.get('progress') or {}
    record['counts'] = {key: progress.get(key, 0) for key in ('searched_windows', 'total_windows', 'found_messages', 'read_messages', 'read_target_messages', 'events', 'known_threads')}
    append_private_jsonl(state_dir / 'mail-sync-history.jsonl', record)


class RunTiming:
    """One process attempt, distinct from a logical run resumed on another day."""
    def __init__(self, state_dir, cp, state, initial_threads, started):
        self.state, self.cp = state, cp
        self.initial_threads = set(initial_threads)
        self.started = started
        self.phase = None
        self.phase_started = self.started
        self.phase_started_at = None
        self.durations = {}
        self.finished = False
        self.trace = None
        try:
            directory = state_dir / 'mail-sync-traces'
            directory.mkdir(exist_ok=True); os.chmod(directory, 0o700)
            self.trace = directory / (hashlib.sha256(cp['run_id'].encode()).hexdigest() + '.jsonl')
        except OSError:
            state['timing_log_error'] = 'trace_unavailable'
        self.emit('run_start', run_mode=state['run_mode'], resumed=state['resumed'], initial_processed_threads=state['initial_processed_threads'], initial_total_threads=state['initial_total_threads'], initial_remaining_threads=state['initial_remaining_threads'])

    def emit(self, kind, **metadata):
        if self.trace is None: return
        try:
            append_private_jsonl(self.trace, {'at': precise_now(), 'kind': kind, 'run_id': self.cp['run_id'], 'attempt_id': self.state['attempt_id'], **metadata})
        except OSError:
            self.state['timing_log_error'] = 'trace_unavailable'

    def update(self, phase=None):
        current = time.monotonic()
        if self.finished:
            return
        if phase and phase != self.phase:
            if self.phase:
                duration = max(0, current - self.phase_started)
                self.durations[self.phase] = self.durations.get(self.phase, 0) + duration
                self.emit('phase_end', phase=self.phase, started_at=self.phase_started_at, duration_seconds=round(duration, 3))
            self.phase, self.phase_started, self.phase_started_at = phase, current, precise_now()
            self.emit('phase_start', phase=phase, started_at=self.phase_started_at)
        active = max(0, current - self.phase_started) if self.phase else 0
        durations = dict(self.durations)
        if self.phase: durations[self.phase] = durations.get(self.phase, 0) + active
        self.state.update(elapsed_seconds=round(max(0, current - self.started), 3), duration_seconds=None,
                          phase_started_at=self.phase_started_at, phase_elapsed_seconds=round(active, 3),
                          phase_durations={key: round(value, 3) for key, value in durations.items()}, timing_updated_at=precise_now(),
                          newly_processed_threads=len(set(self.cp.get('read_thread_ids', [])) - self.initial_threads))

    def finish(self):
        self.update()
        if self.phase:
            self.emit('phase_end', phase=self.phase, started_at=self.phase_started_at, duration_seconds=self.state['phase_elapsed_seconds'])
        self.state['duration_seconds'] = self.state['elapsed_seconds']
        self.finished = True
        self.emit('run_end', status=self.state['status'], duration_seconds=self.state['duration_seconds'], newly_processed_threads=self.state['newly_processed_threads'], processed_threads=self.state['processed_threads'], total_threads=self.state.get('total_threads'), error_code=self.state.get('error_code'), phase_durations=self.state['phase_durations'])


def run_codex(args, provider=None):
    data = args.data_dir.resolve()
    # Preview outputs/checkpoints must live outside canonical data.
    state_dir = data if args.apply else args.work_dir.resolve()
    if not args.apply and (state_dir == data or data in state_dir.parents):
        raise ValueError('Preview --work-dir must be outside canonical data')
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / '.mail-sync.lock'
    with lock_path.open('a+') as lock:
        os.chmod(lock_path, 0o600)
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': True, 'status': 'already_running', 'applied_count': 0}
        attempt_started, attempt_started_at = time.monotonic(), precise_now()
        state_path = state_dir / 'mail-sync-state.json'
        cp_path = state_dir / 'mail-sync-checkpoint.json'
        old = read(state_path, {})
        cp = read(cp_path, {}) if not args.fresh else {}
        if cp.get('provider') not in (None, 'codex'): cp = {}
        reused_checkpoint = cp.get('version') == 1 and (not cp.get('complete') or args.reclassify_cache)
        if cp.get('version') != 1 or (cp.get('complete') and not args.reclassify_cache):
            cp = fresh_checkpoint(data, old if args.apply else read(data / 'mail-sync-state.json', {}), dt.datetime.now(UTC), args.since)
        initial_threads = set(cp.get('read_thread_ids', []))
        initial_progress = progress_state(cp)
        classifier_hash = hashlib.sha256((Path(__file__).parent / 'classifier.py').read_bytes()).hexdigest()
        reclassifying = reused_checkpoint and (cp.get('classifier_hash') != classifier_hash or args.reclassify_cache)
        cache_dir = state_dir / 'mail-thread-cache'
        cache_dir.mkdir(parents=True, exist_ok=True); os.chmod(cache_dir, 0o700)
        if cp.get('classifier_hash') != classifier_hash or args.reclassify_cache:
            cp.update(events=[], read_message_ids=[], read_thread_ids=[], complete=False)
        cp['classifier_hash'] = classifier_hash
        state = {'status': 'running', 'last_success_at': old.get('last_success_at'), 'started_at': attempt_started_at,
                 'completed_at': None, 'processed_threads': len(cp['read_thread_ids']), 'applied_count': 0,
                 'review_count': 0, 'phase': cp['phase'], 'run_id': cp['run_id'], 'error': None, 'error_code': None, 'error_message': None,
                 'attempt_id': 'attempt-' + os.urandom(8).hex(), 'resumed': bool(reused_checkpoint),
                 'run_mode': 'reclassify' if reclassifying else 'resume' if reused_checkpoint else 'fresh',
                 'initial_processed_threads': len(initial_threads), 'initial_total_threads': initial_progress['total_threads'],
                 'initial_remaining_threads': (initial_progress['total_threads'] - len(initial_threads)) if initial_progress['total_threads'] is not None else None,
                 **{'initial_' + key: initial_progress[key] for key in ('remaining_messages', 'remaining_known_threads', 'remaining_windows')}}
        timing = RunTiming(state_dir, cp, state, initial_threads, attempt_started)
        provider = provider or CodexGmail(args.codex_bin, args.timeout)
        provider.cache_dir = state_dir / 'provider-cache' / cp['run_id']
        def update_provider_timing(summary=None):
            try:
                method = getattr(provider, 'timing_summary', None)
                if summary is None and callable(method): summary = method()
                if isinstance(summary, dict): state['provider_timing_summary'] = summary
            except Exception:
                state['timing_log_error'] = 'provider_timing_unavailable'
        def provider_progress(summary):
            update_provider_timing(summary)
            timing.update()
            state['updated_at'] = now()
            atomic(state_path, state)
        provider.progress_callback = provider_progress
        def checkpoint():
            state.update(progress_state(cp), updated_at=now())
            timing.update(cp.get('phase_detail'))
            update_provider_timing()
            atomic(cp_path, cp); atomic(state_path, state)
            timing.emit('checkpoint', phase=cp.get('phase_detail'), elapsed_seconds=state['elapsed_seconds'], processed_threads=state['processed_threads'], total_threads=state['total_threads'], remaining_messages=state['remaining_messages'], remaining_known_threads=state['remaining_known_threads'])
        def record_history():
            try: append_run_history(state_dir, state)
            except OSError: state['timing_log_error'] = 'history_unavailable'
        cp['phase_detail'] = 'restoring_cache'
        checkpoint()
        try:
            cp['errors'] = []
            # Cached normalized MIME bodies are private and avoid re-reading Gmail
            # when classification rules are improved after a preview review.
            for cached in cache_dir.glob('*.json'):
                thread = read(cached, {})
                if thread.get('_sync_run_id') != cp['run_id']:
                    continue
                tid = thread.get('id') or thread.get('thread_id')
                wanted = set(cp['message_ids'])
                if tid in cp['known_thread_ids'] or any(m.get('id') in wanted for m in thread.get('messages', [])):
                    process_threads(cp, [thread])
            checkpoint()
            # Fetch the first pages together; continuation pages stay explicit and durable.
            initial = [w for w in cp['windows'] if not w['done'] and w['token'] is None]
            if initial:
                cp.update(phase='search', phase_detail='search_messages'); checkpoint()
            if initial and hasattr(provider, 'search_many'):
                for offset in range(0, len(initial), 13):
                    batch = initial[offset:offset + 13]
                    pages = provider.search_many([query_for(w) for w in batch])
                    for window, (ids, token) in zip(batch, pages):
                        cp['message_ids'] = sorted(set(cp['message_ids']) | set(ids))
                        window['token'], window['done'] = token, token is None
                        checkpoint()
            for window in cp['windows']:
                if window['done']: continue
                cp.update(phase='search', phase_detail='search_messages'); checkpoint()
                query = query_for(window)
                seen_tokens = set()
                while True:
                    previous = window['token']
                    ids, token = provider.search(query, previous)
                    if token and (token == previous or token in seen_tokens):
                        raise ProviderError('Gmail pagination token repeated')
                    if token: seen_tokens.add(token)
                    cp['message_ids'] = sorted(set(cp['message_ids']) | set(ids))
                    window['token'], window['done'] = token, token is None
                    checkpoint()
                    if window['done']: break
            cp.update(phase='read', phase_detail='read_recent_threads'); checkpoint()
            pending = sorted(set(cp['message_ids']) - set(cp['read_message_ids']))
            while pending:
                selected = pending[:20]
                threads = provider.read(selected)
                cache_threads(cache_dir, threads, cp['run_id'])
                process_threads(cp, threads)
                checkpoint()
                after = sorted(set(cp['message_ids']) - set(cp['read_message_ids']))
                if after == pending:
                    raise ProviderError('Mail thread read did not cover any requested messages')
                pending = after
            pending_threads = sorted(set(cp['known_thread_ids']) - set(cp['read_thread_ids']))
            cp.update(phase='read', phase_detail='read_known_threads'); checkpoint()
            for offset in range(0, len(pending_threads), 20):
                threads = provider.read(pending_threads[offset:offset + 20], threads=True)
                cache_threads(cache_dir, threads, cp['run_id'])
                process_threads(cp, threads); checkpoint()
            coverage = progress_state(cp)
            if coverage['remaining_messages'] or coverage['remaining_known_threads'] or coverage['remaining_windows']:
                raise ProviderError('Mail read coverage incomplete')
            cp.update(complete=True, phase='validated', phase_detail='validate'); checkpoint()
            output = args.output.resolve() if args.output else state_dir / 'mail-sync-bundle.json'
            atomic(output, bundle(cp))
            summary = {'ok': True, 'status': 'preview', 'applied_count': 0,
                       'review_count': sum(bool(e.get('needs_review')) and e.get('apply', True) for e in cp['events']),
                       'processed_events': len(cp['events']), 'processed_threads': len(cp['read_thread_ids']),
                       'bundle_path': str(output), 'complete': True, 'errors': []}
            if args.apply:
                cp.update(phase='apply', phase_detail='apply'); checkpoint()
                env = os.environ.copy(); env['CAREER_OPS_ROOT'] = str(args.project_root.resolve())
                env['DASHBOARD_DATA_DIR'] = str(data)
                result = subprocess.run([sys.executable, str(args.ingest_script.resolve()), '--bundle', str(output), '--apply'],
                                        env=env, capture_output=True, text=True, timeout=300)
                try: summary.update(json.loads(result.stdout))
                except ValueError: raise ProviderError('Mail ingestion returned invalid output')
                if result.returncode or not summary.get('ok') or summary.get('errors'):
                    raise ProviderError('Mail ingestion failed; checkpoint retained for retry')
                summary['status'] = 'complete'
            state.update(status='complete' if args.apply else 'preview', completed_at=precise_now(),
                         applied_count=summary['applied_count'], review_count=summary['review_count'], phase='complete')
            if args.apply: state['last_success_at'] = cp['window_end']
            cp.update(phase='complete', phase_detail='complete'); checkpoint()
            timing.finish()
            record_history()
            atomic(state_path, state)
            summary.update({key: state[key] for key in ('duration_seconds', 'resumed', 'run_mode', 'initial_processed_threads', 'newly_processed_threads', 'phase_durations')})
            return summary
        except Exception as exc:
            error_code, error_message, diagnostic = describe_error(exc, cp.get('phase'))
            cp['complete'] = False
            cp['errors'] = [diagnostic]
            state.update(status='failed', completed_at=precise_now(), error=error_message, error_code=error_code, error_message=error_message, error_diagnostic=diagnostic)
            checkpoint()
            timing.finish()
            record_history()
            atomic(state_path, state)
            if args.output: atomic(args.output, bundle(cp))
            return {'ok': False, 'status': 'failed', 'complete': False, 'errors': cp['errors'],
                    'processed_threads': len(cp['read_thread_ids']), 'last_success_at': state['last_success_at'], 'error_code': error_code, 'error_message': error_message, **progress_state(cp),
                    **{key: state[key] for key in ('duration_seconds', 'resumed', 'run_mode', 'initial_processed_threads', 'newly_processed_threads', 'phase_durations')}}


def process_threads(cp, threads):
    for thread in threads:
        tid = thread.get('id') or thread.get('thread_id')
        events = classify_thread(thread)
        # Atomic replacement per thread lets resumed batches safely re-read it.
        cp['events'] = [e for e in cp['events'] if e['thread_id'] != tid] + events
        cp['read_thread_ids'] = sorted(set(cp['read_thread_ids']) | {tid})
        cp['read_message_ids'] = sorted(set(cp['read_message_ids']) | {m['id'] for m in thread['messages']})


def cache_threads(cache_dir, threads, run_id):
    for thread in threads:
        tid = thread.get('id') or thread.get('thread_id')
        name = hashlib.sha256(str(tid).encode()).hexdigest() + '.json'
        previous = read(cache_dir / name, {})
        if previous.get('_sync_run_id') == run_id:
            messages = {m['id']: m for m in previous.get('messages', [])}
            messages.update({m['id']: m for m in thread.get('messages', [])})
            # Mutate the value passed to process_threads too, so a connector's
            # later partial thread view cannot erase already-observed SENT mail.
            thread['messages'] = sorted(messages.values(), key=lambda m: (str(m.get('internal_date') or ''), m['id']))
        atomic(cache_dir / name, {**thread, '_sync_run_id': run_id})


def query_for(window):
    return ('in:anywhere -in:spam -in:trash -in:drafts after:' +
            str(int(dt.datetime.fromisoformat(window['start']).timestamp())) + ' before:' +
            str(int(dt.datetime.fromisoformat(window['end']).timestamp())) + ' ' + TERMS)


def run(args, provider=None):
    if provider is not None:
        if getattr(provider, 'provider_name', 'codex') == 'gmail':
            from gmail_sync import run_gmail
            return run_gmail(args, provider)
        return run_codex(args, provider)
    choice = getattr(args, 'provider', 'codex')
    credentials = Path(getattr(args, 'gmail_credentials', Path.home() / '.config/career-dashboard/gmail.json')).expanduser()
    if choice == 'auto':
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from gmail_auth import connection_status
        status = connection_status(credentials)
        choice = 'gmail' if status.get('connected') or status.get('configured') or status.get('credentials_present') or credentials.exists() else 'codex'
    if choice == 'gmail':
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from gmail_api import GmailAPI
        from gmail_sync import run_gmail
        return run_gmail(args, GmailAPI(credentials, timeout=min(getattr(args, 'timeout', 30), 60)))
    return run_codex(args)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=Path(os.environ.get('CAREER_OPS_ROOT', str(Path(__file__).resolve().parents[3] / 'engine'))))
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ['DASHBOARD_DATA_DIR']).expanduser() if os.environ.get('DASHBOARD_DATA_DIR') else None)
    parser.add_argument('--work-dir', type=Path, default=Path('/tmp/career-mail-preview'))
    parser.add_argument('--ingest-script', type=Path, default=Path(__file__).resolve().parent.parent / 'mail_store.py')
    parser.add_argument('--codex-bin', default=os.environ.get('CODEX_BIN', os.environ.get('CAREER_OPS_CODEX_BIN', 'codex')))
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--provider', choices=('auto', 'gmail', 'codex'), default=os.environ.get('CAREER_MAIL_PROVIDER', 'auto'))
    parser.add_argument('--gmail-credentials', type=Path, default=Path(os.environ.get('CAREER_GMAIL_CREDENTIALS', os.environ.get('DASHBOARD_GMAIL_CREDENTIALS', str(Path.home() / '.config/career-dashboard/gmail.json')))))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--since')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--reclassify-cache', action='store_true', help='Rebuild existing preview from its private cached threads')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir or args.project_root / 'data'
    result = run(args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__': raise SystemExit(main())
