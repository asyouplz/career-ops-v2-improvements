"""Account-bound Gmail history synchronization; canonical changes use the existing ingester."""
from __future__ import annotations
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from classifier import classify_thread
from worker import UTC, RunTiming, append_run_history, atomic, describe_error, fresh_checkpoint, known_threads, now, precise_now, progress_state, query_for, read

SEMANTIC_LABELS = {'DRAFT', 'SENT', 'SPAM', 'TRASH'}


class SyncError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.error_code = code


def unavailable(exc):
    return getattr(exc, 'error_code', None) == 'mail_message_unavailable' or type(exc).__name__ == 'MessageUnavailable'


def expired(exc):
    return getattr(exc, 'error_code', None) == 'mail_history_expired' or type(exc).__name__ == 'HistoryExpired'


def checked_profile(provider):
    profile = provider.profile()
    account, history_id = profile.get('emailAddress'), profile.get('historyId')
    if not isinstance(account, str) or '@' not in account or not isinstance(history_id, str) or not history_id:
        raise SyncError('connector_response_invalid', 'Gmail profile identity or history cursor missing')
    return account.strip().casefold(), history_id


def approved_old_threads(data):
    """Relevance alone never authorizes an old, unreviewed imported conversation."""
    found = set()
    def visit(value):
        if isinstance(value, dict):
            tid = value.get('thread_id')
            if isinstance(tid, str) and tid and not value.get('needs_review') and '최초 조회 범위' not in value.get('reason', ''):
                found.add(tid)
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(read(data / 'dashboard-mail-ledger.json', {}))
    try:
        text = (data / 'applications.md').read_text()
        found.update(re.findall(r'mail\.google\.com/mail/(?:u/\d+/)?#(?:all|inbox)/([a-f0-9]{10,40})', text))
    except FileNotFoundError: pass
    return found


def base_checkpoint(data, end, since=None):
    cp = fresh_checkpoint(data, {}, end, since)
    cp.update(provider='gmail', approved_since=cp['window_start'], bootstrap_done=False, history_done=False, history_token=None,
              history_pages=0, history_seen_tokens=[], candidate_history_id=None,
              bootstrap_thread_ids=[], bootstrap_read_ids=[], history_targets=[], history_read_ids=[],
              changed_messages={}, metadata_done_ids=[], ignored_changes=0, unavailable_message_ids=[],
              label_changes={}, reclassified=False)
    return cp


class GmailSync:
    def __init__(self, args, provider, state_dir, started, started_at, profile):
        self.args, self.provider, self.directory = args, provider, state_dir
        self.data = args.data_dir.resolve()
        account, profile_history = profile
        self.account_hash = hashlib.sha256(account.encode()).hexdigest()
        for location in {self.data, state_dir}:
            binding = read(location / 'gmail-account.json', {})
            if binding and binding.get('account_hash') != self.account_hash:
                raise SyncError('mail_account_mismatch', 'Gmail account binding differs from authenticated profile')
        self.account_dir = state_dir / 'gmail-accounts' / self.account_hash
        self.account_dir.mkdir(parents=True, exist_ok=True); os.chmod(self.account_dir.parent, 0o700); os.chmod(self.account_dir, 0o700)
        self.cache_dir = self.account_dir / 'threads'
        self.cache_dir.mkdir(exist_ok=True); os.chmod(self.cache_dir, 0o700)
        self.canonical_account_dir = self.data / 'gmail-accounts' / self.account_hash
        self.known = set(known_threads(self.data))
        self.approved_old = approved_old_threads(self.data)
        self.cp_path = self.account_dir / 'checkpoint.json'
        self.state_path = state_dir / 'mail-sync-state.json'
        self.cursor_path = self.account_dir / 'cursor.json'
        cursor = read(self.cursor_path, {})
        if not cursor and not args.apply:
            cursor = read(self.canonical_account_dir / 'cursor.json', {})
        if cursor and cursor.get('account_hash') != self.account_hash:
            raise SyncError('mail_account_mismatch', 'Gmail cursor account differs')
        old = read(self.state_path, {})
        prior = {} if args.fresh else read(self.cp_path, {})
        self.resumed = prior.get('version') == 1 and prior.get('provider') == 'gmail' and (not prior.get('complete') or args.reclassify_cache)
        if self.resumed and prior.get('account_hash') != self.account_hash:
            raise SyncError('mail_account_mismatch', 'Gmail checkpoint account differs')
        self.classifier_hash = hashlib.sha256((Path(__file__).parent / 'classifier.py').read_bytes()).hexdigest()
        self.cp = prior if self.resumed else base_checkpoint(self.data, dt.datetime.now(UTC), args.since)
        if not self.resumed:
            mode = 'incremental' if cursor.get('committed_history_id') and not args.fresh else 'bootstrap'
            self.cp.update(sync_mode=mode, account_hash=self.account_hash,
                           history_start=cursor.get('committed_history_id') if mode == 'incremental' else profile_history,
                           reclassify_required=bool(cursor.get('classifier_hash') and cursor['classifier_hash'] != self.classifier_hash) or args.reclassify_cache)
            if mode == 'incremental':
                self.cp.update(bootstrap_done=True, windows=[], known_thread_ids=[], window_start=cursor.get('window_end') or self.cp['window_start'])
                # A newly linked old application may have no recent mailbox event.
                missing = [tid for tid in self.known if not self.cache_path(tid).exists() and (args.apply or not self.cache_path(tid, self.canonical_account_dir / 'threads').exists())]
                self.cp['history_targets'] = sorted(missing)
        elif self.cp.get('classifier_hash') != self.classifier_hash or args.reclassify_cache:
            self.cp.update(reclassify_required=True, reclassified=False)
        self.cp['classifier_hash'] = self.classifier_hash
        self.cp.setdefault('errors', [])
        self.initial_threads = set(self.cp.get('read_thread_ids', []))
        initial = progress_state(self.cp)
        self.state = {'status': 'running', 'provider': 'gmail', 'sync_mode': self.cp['sync_mode'],
                      'run_id': self.cp['run_id'], 'attempt_id': 'attempt-' + os.urandom(8).hex(), 'started_at': started_at,
                      'completed_at': None, 'last_success_at': old.get('last_success_at'), 'applied_count': 0, 'review_count': 0,
                      'resumed': self.resumed, 'run_mode': 'reclassify' if args.reclassify_cache else 'resume' if self.resumed else 'fresh',
                      'initial_processed_threads': len(self.initial_threads), 'initial_total_threads': initial['total_threads'],
                      'initial_remaining_threads': initial['total_threads'] - len(self.initial_threads) if initial['total_threads'] is not None else None,
                      'error': None, 'error_code': None, 'error_message': None}
        self.timing = RunTiming(state_dir, self.cp, self.state, self.initial_threads, started)
        provider.cache_dir = self.account_dir / 'provider-diagnostics' / self.cp['run_id']
        provider.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(provider.cache_dir.parent, 0o700); os.chmod(provider.cache_dir, 0o700)
        provider.progress_callback = self.provider_progress
        atomic(state_dir / 'gmail-account.json', {'version': 1, 'account_hash': self.account_hash, 'account': account})
        self.phase('restoring_cache', '저장된 메일 동기화 진행 확인')

    def provider_progress(self, summary):
        self.state['provider_timing_summary'] = summary
        self.timing.update()
        self.state['updated_at'] = now()
        atomic(self.state_path, self.state)

    def phase(self, detail, label):
        self.cp.update(phase=detail, phase_detail=detail, phase_label=label)
        self.save()

    def save(self):
        self.state.update(progress_state(self.cp), updated_at=now(), provider='gmail', sync_mode=self.cp['sync_mode'],
                          history_pages=self.cp.get('history_pages', 0), ignored_changes=self.cp.get('ignored_changes', 0),
                          changed_threads=len(self.cp.get('history_targets', [])), cache_reused_threads=self.cp.get('cache_reused_threads', 0))
        self.state['phase_label'] = self.cp.get('phase_label', self.state['phase_label'])
        if not self.cp.get('history_done'):
            self.state['total_threads'] = None
            self.state['progress']['total_threads'] = None
        self.timing.update(self.cp.get('phase_detail'))
        method = getattr(self.provider, 'timing_summary', None)
        if callable(method):
            try: self.state['provider_timing_summary'] = method()
            except Exception: self.state['timing_log_error'] = 'provider_timing_unavailable'
        atomic(self.cp_path, self.cp)
        atomic(self.directory / 'mail-sync-checkpoint.json', self.cp)
        atomic(self.state_path, self.state)
        self.timing.emit('checkpoint', phase=self.cp.get('phase_detail'), processed_threads=self.state['processed_threads'], total_threads=self.state['total_threads'], history_pages=self.cp.get('history_pages', 0))

    def cache_path(self, tid, directory=None):
        return (directory or self.cache_dir) / (hashlib.sha256(tid.encode()).hexdigest() + '.json')

    def cached(self, tid):
        value = read(self.cache_path(tid), {})
        if not value and not self.args.apply:
            value = read(self.cache_path(tid, self.canonical_account_dir / 'threads'), {})
        if value and (value.get('_account_hash') != self.account_hash or value.get('id') != tid):
            raise SyncError('mail_account_mismatch', 'Cached Gmail thread identity differs')
        return value

    def classify(self, thread):
        events = classify_thread(thread)
        tid = thread['id']
        approved_since = self.cp.get('approved_since')
        approved = tid in self.approved_old or thread.get('_approved_old_context') is True
        if not approved and approved_since:
            boundary = dt.datetime.fromisoformat(approved_since.replace('Z', '+00:00'))
            outside = bool(events) and all(dt.datetime.fromisoformat(event['event_at'].replace('Z', '+00:00')) < boundary for event in events)
            if outside:
                for event in events:
                    event.update(needs_review=True, reason='최초 조회 범위 이전에 작성된 메일 — 확인 필요; ' + event.get('reason', ''))
            elif events:
                approved = True
        thread['_approved_old_context'] = approved
        self.cp['events'] = [event for event in self.cp['events'] if event['thread_id'] != tid] + events
        return events

    def put_thread(self, tid, current):
        if current.get('id') != tid or not isinstance(current.get('messages'), list):
            raise SyncError('connector_response_invalid', 'Gmail full thread identity or messages missing')
        previous = self.cached(tid)
        messages = {item['id']: item for item in previous.get('messages', [])}
        seen = set()
        for item in current['messages']:
            ident = item.get('id')
            if not isinstance(ident, str) or not ident or ident in seen or item.get('thread_id') not in (None, tid):
                raise SyncError('connector_response_invalid', 'Gmail message identity differs from its thread')
            seen.add(ident)
            messages[ident] = copy.deepcopy(item)
            messages[ident]['thread_id'] = tid
        # Removed mail remains evidence; availability is stored separately.
        result = {**copy.deepcopy(current), 'messages': sorted(messages.values(), key=lambda item: (str(item.get('internal_date', '')), item['id'])),
                  '_account_hash': self.account_hash, '_verified_at': precise_now(), '_verified_run_id': self.cp['run_id'],
                  '_career_relevant': previous.get('_career_relevant') is True,
                  '_approved_old_context': previous.get('_approved_old_context') is True,
                  '_current_message_ids': sorted(seen),
                  '_unavailable_message_ids': sorted((set(previous.get('_unavailable_message_ids', [])) | (set(messages) - seen)) - seen)}
        atomic(self.cache_path(tid), result)
        result['_career_relevant'] = bool(self.classify(result)) or result['_career_relevant']
        atomic(self.cache_path(tid), result)
        self.cp['read_message_ids'] = sorted(set(self.cp['read_message_ids']) | set(messages))
        self.cp['read_thread_ids'] = sorted(set(self.cp['read_thread_ids']) | {tid})

    def read_threads(self, tids, completed_key):
        for tid in sorted(set(tids) - set(self.cp[completed_key])):
            try:
                result = self.provider.read([tid], threads=True)
                if len(result) != 1:
                    raise SyncError('connector_response_invalid', 'Gmail thread response count differs')
                self.put_thread(tid, result[0])
            except Exception as exc:
                if not unavailable(exc): raise
                previous = self.cached(tid) or {'id': tid, 'messages': [], '_account_hash': self.account_hash, '_current_message_ids': []}
                previous['_unavailable_at'] = precise_now()
                atomic(self.cache_path(tid), previous)
                self.cp['unavailable_thread_ids'] = sorted(set(self.cp.get('unavailable_thread_ids', [])) | {tid})
                self.cp['read_thread_ids'] = sorted(set(self.cp['read_thread_ids']) | {tid})
            self.cp[completed_key].append(tid)
            self.save()

    def reclassify_cache(self):
        if not self.cp.get('reclassify_required') or self.cp.get('reclassified'): return
        self.phase('restoring_cache', '저장된 메일 분류 규칙 다시 확인')
        paths = {path.name: path for path in (self.canonical_account_dir / 'threads').glob('*.json')} if not self.args.apply else {}
        paths.update({path.name: path for path in self.cache_dir.glob('*.json')})
        for path in paths.values():
            value = read(path, {})
            if value.get('_account_hash') != self.account_hash:
                raise SyncError('mail_account_mismatch', 'Cached reclassification account differs')
            self.classify(value)
        self.cp['cache_reused_threads'] = len(paths)
        self.cp['reclassified'] = True
        self.save()

    def bootstrap(self):
        if self.cp['bootstrap_done']: return
        self.phase('search_messages', '최근 1년 지원 관련 메일 검색')
        for window in self.cp['windows']:
            seen_tokens = set()
            while not window['done']:
                ids, token = self.provider.search(query_for(window), window['token'])
                if token and (token == window['token'] or token in seen_tokens):
                    raise SyncError('mail_pagination_invalid', 'Gmail search pagination repeated')
                if token: seen_tokens.add(token)
                self.cp['message_ids'] = sorted(set(self.cp['message_ids']) | set(ids))
                window.update(token=token, done=token is None)
                self.save()
        self.phase('read_recent_threads', '검색된 메일의 대화 연결 확인')
        mapping_method = getattr(self.provider, 'message_thread_ids', None)
        mappings = mapping_method(self.cp['message_ids']) if callable(mapping_method) else {}
        for ident in sorted(set(self.cp['message_ids']) - set(self.cp.get('bootstrap_metadata_ids', []))):
            tid = mappings.get(ident)
            metadata = None if isinstance(tid, str) and tid else self.metadata(ident)
            tid = tid if isinstance(tid, str) and tid else metadata['thread_id'] if metadata else None
            if tid:
                self.cp['bootstrap_thread_ids'] = sorted(set(self.cp['bootstrap_thread_ids']) | {tid})
                self.cp.setdefault('bootstrap_message_threads', {})[ident] = tid
            else:
                self.cp['read_message_ids'] = sorted(set(self.cp['read_message_ids']) | {ident})
            self.cp.setdefault('bootstrap_metadata_ids', []).append(ident)
            self.save()
        targets = sorted(set(self.cp['bootstrap_thread_ids']) | self.known)
        self.cp['known_thread_ids'] = targets
        self.phase('read_known_threads', '기존 지원 대화와 원문 확인')
        self.read_threads(targets, 'bootstrap_read_ids')
        self.ensure_messages(self.cp.get('bootstrap_message_threads', {}).items())
        # A message deleted between list and GET is unavailable, not missing evidence.
        self.cp['read_message_ids'] = sorted(set(self.cp['read_message_ids']) | set(self.cp['message_ids']))
        self.cp['bootstrap_done'] = True
        self.save()

    def metadata(self, ident):
        try:
            result = self.provider.get_messages([ident], format='metadata')
        except Exception as exc:
            if not unavailable(exc): raise
            self.cp['unavailable_message_ids'] = sorted(set(self.cp['unavailable_message_ids']) | {ident})
            return None
        if len(result) != 1 or result[0].get('id') != ident or not isinstance(result[0].get('thread_id'), str):
            raise SyncError('connector_response_invalid', 'Gmail message metadata identity differs')
        return result[0]

    def ensure_messages(self, pairs):
        for ident, tid in pairs:
            if not tid or ident in self.cp['unavailable_message_ids']: continue
            thread = self.cached(tid)
            if ident in thread.get('_current_message_ids', []): continue
            try: messages = self.provider.get_messages([ident], format='full')
            except Exception as exc:
                if not unavailable(exc): raise
                self.cp['unavailable_message_ids'] = sorted(set(self.cp['unavailable_message_ids']) | {ident})
                self.save()
                continue
            if len(messages) != 1 or messages[0].get('id') != ident or messages[0].get('thread_id') != tid:
                raise SyncError('connector_response_invalid', 'Gmail changed message body identity differs')
            merged = {item['id']: item for item in thread.get('messages', [])}
            merged[ident] = messages[0]
            self.put_thread(tid, {'id': tid, 'messages': list(merged.values())})
            value = self.cached(tid)
            value['_current_message_ids'] = sorted(set(thread.get('_current_message_ids', [])) | {ident})
            atomic(self.cache_path(tid), value)
            self.save()

    def changes(self, page):
        records = page.get('history', [])
        if not isinstance(records, list):
            raise SyncError('connector_response_invalid', 'Gmail history entries missing')
        for record in records:
            typed = any(record.get(key) for key in ('messagesAdded', 'messagesDeleted', 'labelsAdded', 'labelsRemoved'))
            changes = [('added', item.get('message', {}), []) for item in record.get('messagesAdded', [])]
            changes += [('deleted', item.get('message', {}), []) for item in record.get('messagesDeleted', [])]
            for key, kind in (('labelsAdded', 'labels_added'), ('labelsRemoved', 'labels_removed')):
                changes += [(kind, item.get('message', {}), item.get('labelIds', [])) for item in record.get(key, [])]
            if not typed: changes += [('unknown', item, []) for item in record.get('messages', [])]
            for kind, message, labels in changes:
                ident, tid = message.get('id'), message.get('threadId') or message.get('thread_id')
                if not isinstance(ident, str) or not ident:
                    raise SyncError('connector_response_invalid', 'Gmail history message identity missing')
                if kind.startswith('labels_'):
                    # INBOX/UNREAD/STARRED changes carry no new application decision.
                    if not SEMANTIC_LABELS.intersection(labels):
                        self.cp['ignored_changes'] += 1
                        if tid:
                            value = self.cached(tid)
                            for cached in value.get('messages', []):
                                if cached['id'] == ident:
                                    old_labels = set(cached.get('label_ids', []))
                                    cached['label_ids'] = sorted(old_labels | set(labels) if kind == 'labels_added' else old_labels - set(labels))
                            if value: atomic(self.cache_path(tid), value)
                        continue
                if kind == 'deleted':
                    self.cp['unavailable_message_ids'] = sorted(set(self.cp['unavailable_message_ids']) | {ident})
                    if tid:
                        value = self.cached(tid)
                        if value:
                            value['_unavailable_message_ids'] = sorted(set(value.get('_unavailable_message_ids', [])) | {ident})
                            atomic(self.cache_path(tid), value)
                    continue
                item = self.cp['changed_messages'].setdefault(ident, {'thread_id': tid, 'kinds': []})
                if tid and item.get('thread_id') not in (None, tid):
                    raise SyncError('connector_response_invalid', 'Gmail history thread association changed unexpectedly')
                if tid: item['thread_id'] = tid
                item['kinds'] = sorted(set(item['kinds']) | {kind})
                self.cp['message_ids'] = sorted(set(self.cp['message_ids']) | {ident})

    def history(self):
        if self.cp['history_done']: return
        self.phase('list_history', 'Gmail 변경 이력 확인')
        while not self.cp['history_done']:
            token = self.cp['history_token']
            page = self.provider.history(self.cp['history_start'], token)
            history_id = page.get('historyId')
            next_token = page.get('nextPageToken')
            if not isinstance(history_id, str) or not history_id or (next_token and not isinstance(next_token, str)):
                raise SyncError('connector_response_invalid', 'Gmail history cursor missing')
            if next_token and (next_token == token or next_token in self.cp['history_seen_tokens']):
                raise SyncError('mail_pagination_invalid', 'Gmail history pagination repeated')
            self.changes(page)
            if next_token: self.cp['history_seen_tokens'].append(next_token)
            self.cp.update(history_token=next_token, history_done=not next_token, candidate_history_id=history_id)
            self.cp['history_pages'] += 1
            self.save()

    def resolve_changes(self):
        self.phase('read_recent_threads', '변경된 메일의 지원 관련성 확인')
        known = self.known | {event['thread_id'] for event in self.cp['events']}
        for ident, change in self.cp['changed_messages'].items():
            if ident in self.cp['metadata_done_ids']: continue
            tid = change.get('thread_id')
            if not tid or tid not in known:
                metadata = self.metadata(ident)
                if metadata:
                    actual = metadata['thread_id']
                    if tid and actual != tid:
                        raise SyncError('connector_response_invalid', 'Gmail changed message thread identity differs')
                    tid = actual
                    labels = set(metadata.get('label_ids', []))
                    headers = {header.get('name', '').casefold(): header.get('value') for header in metadata.get('payload', {}).get('headers', [])}
                    # Match existing classifier policy; a generic subject alone is never excluded.
                    if labels.intersection({'DRAFT', 'SPAM', 'TRASH'}) or headers.get('list-id') or headers.get('list-unsubscribe'):
                        tid = None
                else: tid = None
            if tid:
                self.cp['history_targets'] = sorted(set(self.cp['history_targets']) | {tid})
            else:
                self.cp['read_message_ids'] = sorted(set(self.cp['read_message_ids']) | {ident})
            change['resolved_thread_id'] = tid
            self.cp['metadata_done_ids'].append(ident)
            self.save()
        self.cp['known_thread_ids'] = sorted(set(self.cp['known_thread_ids']) | set(self.cp['history_targets']))
        self.phase('read_known_threads', '변경된 지원 대화 원문 확인')
        self.read_threads(self.cp['history_targets'], 'history_read_ids')
        # Verify each selected change is present even if a thread view omitted it.
        self.ensure_messages((ident, change.get('resolved_thread_id')) for ident, change in self.cp['changed_messages'].items())
        self.cp['read_message_ids'] = sorted(set(self.cp['read_message_ids']) | set(self.cp['changed_messages']))
        self.save()

    def rescan(self, start_history):
        previous_events = self.cp['events']
        replacement = base_checkpoint(self.data, dt.datetime.now(UTC), self.args.since)
        replacement.update(run_id=self.cp['run_id'], account_hash=self.account_hash, sync_mode='rescan',
                           history_start=start_history, classifier_hash=self.classifier_hash, events=previous_events,
                           expired_history_id=self.cp['history_start'])
        self.cp.clear(); self.cp.update(replacement)
        self.save()

    def finish(self):
        self.phase('validate', '수집 결과와 변경 범위 확인')
        if not self.cp['history_done'] or set(self.cp['bootstrap_thread_ids']) - set(self.cp['bootstrap_read_ids']) or set(self.cp['history_targets']) - set(self.cp['history_read_ids']):
            raise SyncError('mail_coverage_incomplete', 'Gmail selected thread coverage incomplete')
        value = {key: self.cp[key] for key in ('version', 'run_id', 'window_start', 'window_end', 'complete', 'events')}
        value['complete'] = True
        value['errors'] = []
        output = self.args.output.resolve() if self.args.output else self.directory / 'mail-sync-bundle.json'
        atomic(output, value)
        summary = {'ok': True, 'status': 'preview', 'complete': True, 'errors': [], 'applied_count': 0,
                   'review_count': sum(bool(event.get('needs_review')) and event.get('apply', True) for event in self.cp['events']),
                   'processed_events': len(self.cp['events']), 'processed_threads': len(self.cp['read_thread_ids']), 'bundle_path': str(output)}
        if self.args.apply:
            self.phase('apply', '메일 근거를 지원 현황에 반영')
            env = dict(os.environ, CAREER_OPS_ROOT=str(self.args.project_root.resolve()), DASHBOARD_DATA_DIR=str(self.data))
            result = subprocess.run([sys.executable, str(self.args.ingest_script.resolve()), '--bundle', str(output), '--apply'], env=env, capture_output=True, text=True, timeout=300)
            try: summary.update(json.loads(result.stdout))
            except ValueError: raise SyncError('mail_ingestion_failed', 'Gmail ingestion returned invalid output')
            if result.returncode or not summary.get('ok') or summary.get('errors'):
                raise SyncError('mail_ingestion_failed', 'Gmail ingestion failed; cursor retained')
            summary['status'] = 'complete'
        # This account's watermark is advanced only after all selected work and ingestion succeed.
        atomic(self.cursor_path, {'version': 1, 'account_hash': self.account_hash, 'committed_history_id': self.cp['candidate_history_id'],
                                 'classifier_hash': self.classifier_hash, 'window_end': self.cp['window_end'], 'completed_at': precise_now(), 'preview': not self.args.apply})
        self.cp['complete'] = True
        self.state.update(status=summary['status'], completed_at=precise_now(), applied_count=summary['applied_count'], review_count=summary['review_count'])
        if self.args.apply: self.state['last_success_at'] = self.cp['window_end']
        self.phase('complete', '동기화 완료')
        self.timing.finish()
        self.record_end()
        return {**summary, **{key: self.state.get(key) for key in ('duration_seconds', 'resumed', 'run_mode', 'sync_mode', 'initial_processed_threads', 'newly_processed_threads', 'phase_durations')}}

    def record_end(self):
        try: append_run_history(self.directory, self.state)
        except OSError: self.state['timing_log_error'] = 'history_unavailable'
        atomic(self.state_path, self.state)

    def failed(self, exc):
        code, message, diagnostic = describe_error(exc, self.cp.get('phase'))
        self.cp.update(complete=False, errors=[diagnostic])
        self.state.update(status='failed', completed_at=precise_now(), error_code=code, error_message=message, error=message, error_diagnostic=diagnostic)
        self.save(); self.timing.finish(); self.record_end()
        return {'ok': False, 'status': 'failed', 'complete': False, 'error_code': code, 'error_message': message,
                'processed_threads': self.state['processed_threads'], 'errors': [diagnostic]}


def run_gmail(args, provider):
    data = args.data_dir.resolve()
    directory = data if args.apply else args.work_dir.resolve()
    if not args.apply and (directory == data or data in directory.parents):
        raise ValueError('Preview --work-dir must be outside canonical data')
    if not args.apply and args.output:
        output = args.output.resolve()
        if output == data or data in output.parents:
            raise ValueError('Preview --output must be outside canonical data')
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / '.mail-sync.lock'
    with lock_path.open('a+') as lock:
        os.chmod(lock_path, 0o600)
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'ok': True, 'status': 'already_running', 'applied_count': 0}
        started, started_at = time.monotonic(), precise_now()
        old = read(directory / 'mail-sync-state.json', {})
        preflight = {'status': 'running', 'provider': 'gmail', 'run_id': 'gmail-preflight-' + os.urandom(8).hex(), 'started_at': started_at,
                     'completed_at': None, 'last_success_at': old.get('last_success_at'), 'processed_threads': 0, 'total_threads': None,
                     'phase': 'restoring_cache', 'phase_detail': 'restoring_cache', 'phase_label': '연결된 Gmail 계정 확인', 'applied_count': 0, 'review_count': 0}
        atomic(directory / 'mail-sync-state.json', preflight)
        preflight_dir = directory / 'gmail-preflight-diagnostics' / preflight['run_id']
        preflight_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(preflight_dir.parent, 0o700); os.chmod(preflight_dir, 0o700)
        provider.cache_dir = preflight_dir
        def preflight_progress(summary):
            elapsed = round(max(0, time.monotonic() - started), 3)
            preflight.update(provider_timing_summary=summary, elapsed_seconds=elapsed, phase_elapsed_seconds=elapsed,
                             phase_started_at=started_at, timing_updated_at=precise_now(), updated_at=now())
            atomic(directory / 'mail-sync-state.json', preflight)
        provider.progress_callback = preflight_progress
        sync = None
        try:
            profile = checked_profile(provider)
            sync = GmailSync(args, provider, directory, started, started_at, profile)
            # Preserve the first profile request's diagnostics in the verified run.
            try:
                for source in preflight_dir.rglob('*.json'):
                    destination = provider.cache_dir / source.relative_to(preflight_dir)
                    destination.parent.mkdir(parents=True, exist_ok=True); os.chmod(destination.parent, 0o700)
                    os.replace(source, destination); os.chmod(destination, 0o600)
            except OSError:
                sync.state['timing_log_error'] = 'preflight_diagnostics_pending'
            sync.cp['errors'] = []
            sync.reclassify_cache()
            rescans = 0
            while True:
                sync.bootstrap()
                try:
                    sync.history()
                    break
                except Exception as exc:
                    if not expired(exc) or rescans >= 1: raise
                    rescans += 1
                    account, history_id = checked_profile(provider)
                    if hashlib.sha256(account.encode()).hexdigest() != sync.account_hash:
                        raise SyncError('mail_account_mismatch', 'Gmail account changed during rescan')
                    sync.rescan(history_id)
            sync.resolve_changes()
            return sync.finish()
        except Exception as exc:
            if sync: return sync.failed(exc)
            code, message, diagnostic = describe_error(exc)
            preflight.update(status='failed', completed_at=precise_now(), error_code=code, error_message=message, error=message,
                             error_diagnostic=diagnostic, duration_seconds=round(max(0, time.monotonic() - started), 3))
            atomic(directory / 'mail-sync-state.json', preflight)
            try: append_run_history(directory, preflight)
            except OSError: pass
            return {'ok': False, 'status': 'failed', 'complete': False, 'error_code': code, 'error_message': message}
