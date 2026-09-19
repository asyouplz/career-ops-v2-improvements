"""Read-only Gmail REST transport. No model, shell, or connector is involved.

Mail and credentials are returned only to the caller. Diagnostics contain a
fixed operation vocabulary, counts, HTTP status codes and timing, never request
arguments or response text. The worker owns durable mailbox caches and cursors.
"""
from __future__ import annotations

import copy
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from classifier import BodyError, headers, raw_payload, top_body
from gmail_auth import CredentialError, get_access_token


API_BASE = 'https://gmail.googleapis.com/gmail/v1/users/me'
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class GmailAPIError(RuntimeError):
    error_code = 'mail_provider_failed'

    def __init__(self, message='Gmail request failed', *, status=None, error_code=None):
        super().__init__(message)
        self.status = status
        if error_code is not None:
            self.error_code = error_code


class GmailResponseError(GmailAPIError):
    error_code = 'mail_provider_response_invalid'


class HistoryExpired(GmailAPIError):
    error_code = 'mail_history_expired'

    def __init__(self):
        super().__init__('Gmail history cursor is no longer available', status=404)


class MessageUnavailable(GmailAPIError):
    error_code = 'mail_message_unavailable'

    def __init__(self, resource_type, resource_id):
        super().__init__('Gmail message or conversation is no longer available', status=404)
        self.resource_type = resource_type
        self.resource_id = resource_id


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        # A redirect must never forward a Gmail bearer token to another origin.
        return None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise GmailResponseError('Gmail identifier is invalid')
    return value


def _unique(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError('Expected a list of Gmail identifiers')
    return list(dict.fromkeys(_identifier(value) for value in values))


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _http_reason(error):
    """Read a bounded error body, return only an allowlisted reason enum."""
    allowed = {'accessNotConfigured', 'SERVICE_DISABLED', 'insufficientPermissions',
               'userRateLimitExceeded', 'rateLimitExceeded', 'dailyLimitExceeded'}
    try:
        data = error.read(65537)
        if len(data) > 65536:
            return None
        envelope = json.loads(data)
        if not isinstance(envelope, dict):
            return None
        value = envelope.get('error', {})
        if not isinstance(value, dict):
            return None
        for group in ('errors', 'details'):
            for item in value.get(group, []) if isinstance(value.get(group, []), list) else []:
                if isinstance(item, dict) and item.get('reason') in allowed:
                    return item['reason']
    except (ValueError, TypeError, UnicodeError, OSError, http.client.HTTPException):
        pass
    return None


def _native_raw_payload(value):
    # Unlike connector logs, Gmail's raw field must be untruncated base64url.
    # Never accept connector-specific truncation markers or decoded MIME here.
    if not isinstance(value, str) or not value or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', value):
        raise GmailResponseError('Gmail raw MIME encoding is invalid')
    try:
        base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, UnicodeError):
        raise GmailResponseError('Gmail raw MIME encoding is invalid') from None
    return raw_payload(value)


def _mime(part):
    if not isinstance(part, dict):
        raise GmailResponseError('Gmail MIME part is invalid')
    result = copy.deepcopy(part)
    for original, normalized in (('mimeType', 'mime_type'), ('partId', 'part_id')):
        if original in result:
            result[normalized] = result.pop(original)
    body = result.get('body', {})
    if not isinstance(body, dict):
        raise GmailResponseError('Gmail MIME body is invalid')
    body = dict(body)
    for original, normalized in (('data', 'base64_url_content'), ('attachmentId', 'attachment_id')):
        if original in body:
            body[normalized] = body.pop(original)
    result['body'] = body
    if 'parts' in part:
        if not isinstance(part['parts'], list):
            raise GmailResponseError('Gmail MIME children are invalid')
        result['parts'] = [_mime(child) for child in part['parts']]
    return result


def normalize_message(value, *, expected_id=None, expected_thread=None):
    if not isinstance(value, dict):
        raise GmailResponseError('Gmail message is invalid')
    message_id = _identifier(value.get('id'))
    thread_id = _identifier(value.get('threadId', value.get('thread_id')))
    if expected_id is not None and message_id != expected_id:
        raise GmailResponseError('Gmail returned a different message')
    if expected_thread is not None and thread_id != expected_thread:
        raise GmailResponseError('Gmail returned a message from another conversation')
    result = copy.deepcopy(value)
    for original, normalized in (('threadId', 'thread_id'), ('internalDate', 'internal_date'),
                                 ('labelIds', 'label_ids'), ('historyId', 'history_id')):
        if original in result:
            result[normalized] = result.pop(original)
    # Gmail omits empty repeated fields. An archived message can have no labels;
    # provided SENT/DRAFT values are retained verbatim and never inferred.
    result.setdefault('label_ids', [])
    if not isinstance(result['label_ids'], list) or any(not isinstance(label, str) for label in result['label_ids']):
        raise GmailResponseError('Gmail message labels are invalid')
    if 'payload' in result:
        result['payload'] = _mime(result['payload'])
    return result


class GmailAPI:
    provider_name = 'gmail'

    def __init__(self, credentials_path, timeout=30, max_attempts=3):
        self.credentials_path = Path(credentials_path)
        self.timeout = float(timeout)
        if not 0 < self.timeout <= 300:
            raise ValueError('Gmail timeout must be between zero and 300 seconds')
        self.max_attempts = max(1, min(3, int(max_attempts)))
        self._open = build_opener(_NoRedirect()).open
        self._message_threads = {}
        self._started = time.monotonic()
        self._active = {}
        self._operations = {}
        self._notifying = False
        self._timing = {name: 0 for name in (
            'requested_count', 'completed_count', 'batch_count', 'transport_attempts',
            'retry_attempts', 'auth_refreshes', 'diagnostic_write_failures',
            'progress_callback_failures', 'mapping_hits', 'mapping_misses')}
        self._timing.update({name: 0.0 for name in (
            'transport_seconds', 'validation_seconds', 'auth_seconds', 'retry_wait_seconds')})

    def timing_summary(self):
        current = time.monotonic()
        values = {key: round(value, 6) if isinstance(value, float) else value
                  for key, value in self._timing.items()}
        active = []
        for row in self._active.values():
            active.append({key: copy.deepcopy(row[key]) for key in (
                'diagnostic_id', 'phase', 'started_at', 'attempt', 'request_count',
                'pending_count', 'operation_counts')})
            active[-1]['elapsed_seconds'] = round(max(0, current - row['monotonic_start']), 6)
        return {'version': 1, 'provider': self.provider_name, **values,
                'elapsed_seconds': round(max(0, current - self._started), 6),
                'pending_count': values['requested_count'] - values['completed_count'],
                'cache_hits': 0, 'cache_misses': 0,
                'operation_counts': copy.deepcopy(self._operations), 'active_calls': active,
                'association_seconds_includes_nested_calls': False}

    def _notify(self):
        callback = getattr(self, 'progress_callback', None)
        if self._notifying or not callable(callback):
            return
        self._notifying = True
        try:
            callback(self.timing_summary())
        except Exception:
            self._timing['progress_callback_failures'] += 1
        finally:
            self._notifying = False

    def _write_diagnostic(self, diagnostic):
        if not getattr(self, 'cache_dir', None):
            return
        directory = Path(self.cache_dir) / 'diagnostics'
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, temporary = tempfile.mkstemp(prefix='.' + diagnostic['id'], dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(diagnostic, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / (diagnostic['id'] + '.json'))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _publish(self, diagnostic):
        # Each record owns only its HTTP attempts. Supplemental raw/message
        # reads have separate diagnostics and must never be added here.
        for metric in ('transport_seconds', 'validation_seconds'):
            diagnostic[metric] = round(sum(attempt.get(metric, 0.0)
                                           for attempt in diagnostic['attempts']), 6)
        self.last_diagnostics = copy.deepcopy(diagnostic)
        try:
            self._write_diagnostic(diagnostic)
        except Exception:
            self._timing['diagnostic_write_failures'] += 1
        self._notify()

    def _validate(self, function, *args, **kwargs):
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            self._timing['validation_seconds'] += max(0, time.monotonic() - started)

    def _request(self, operation, path, parameters=None, *, resource_type=None, resource_id=None, validate=None):
        # Paths are built exclusively by the methods below, never by mail data.
        if not re.fullmatch(r'/(?:profile|history|messages|messages/[A-Za-z0-9_-]+|threads/[A-Za-z0-9_-]+)', path):
            raise ValueError('Unsupported Gmail endpoint')
        url = API_BASE + path
        if parameters:
            url += '?' + urlencode(parameters, doseq=True)
        ident = 'gmail-' + uuid.uuid4().hex
        started = time.monotonic()
        row = {'diagnostic_id': ident, 'phase': 'starting', 'started_at': _now(),
               'attempt': 0, 'request_count': 1, 'pending_count': 1,
               'operation_counts': {operation: 1}, 'monotonic_start': started}
        self._active[ident] = row
        self._timing['requested_count'] += 1
        self._timing['batch_count'] += 1
        metrics = self._operations.setdefault(operation, {'requested': 0, 'attempted': 0, 'completed': 0,
                                                          'cache_hits': 0, 'cache_misses': 0})
        metrics['requested'] += 1
        diagnostic = {'version': 2, 'provider': 'gmail', 'id': ident, 'parent_diagnostic_id': None,
                      'started_at': row['started_at'], 'status': 'running', 'phase': 'starting',
                      'request_count': 1, 'pending_count': 1, 'operation_counts': {operation: 1},
                      'request_hash': hashlib.sha256(url.encode()).hexdigest(), 'attempts': []}
        self._publish(diagnostic)
        refreshed = False
        transient_failures = 0
        try:
            auth_started = time.monotonic()
            try:
                access_token = get_access_token(self.credentials_path)
            finally:
                self._timing['auth_seconds'] += max(0, time.monotonic() - auth_started)
            while True:
                row['attempt'] += 1
                row['phase'] = diagnostic['phase'] = 'transport'
                attempt = {'number': row['attempt'], 'started_at': _now(), 'status': 'running',
                           'phase': 'transport', 'request_count': 1, 'operation_counts': {operation: 1}}
                diagnostic['attempts'].append(attempt)
                self._timing['transport_attempts'] += 1
                metrics['attempted'] += 1
                self._publish(diagnostic)
                transport_started = time.monotonic()
                http_status, http_reason, problem, data, retry_after = None, None, None, None, 0.0
                try:
                    request = Request(url, headers={'Authorization': 'Bearer ' + access_token,
                                                   'Accept': 'application/json'}, method='GET')
                    with self._open(request, timeout=self.timeout) as response:
                        http_status = response.status
                        if http_status != 200:
                            raise HTTPError(url, http_status, 'Gmail HTTP error', response.headers, None)
                        data = response.read(MAX_RESPONSE_BYTES + 1)
                except HTTPError as exc:
                    http_status = exc.code
                    problem = 'http_error'
                    http_reason = _http_reason(exc)
                    try:
                        retry_after = min(30.0, max(0.0, float((exc.headers or {}).get('Retry-After', 0))))
                    except (TypeError, ValueError):
                        pass
                    exc.close()
                except (URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, OSError):
                    problem = 'transport_error'
                finally:
                    elapsed = max(0, time.monotonic() - transport_started)
                    self._timing['transport_seconds'] += elapsed
                    attempt.update({'finished_at': _now(), 'transport_seconds': round(elapsed, 6),
                                    'elapsed_seconds': round(elapsed, 6), 'http_status': http_status,
                                    'status': 'failed' if problem else 'complete'})
                    if problem:
                        attempt['problem'] = problem
                    if http_reason:
                        attempt['http_reason'] = http_reason
                self._publish(diagnostic)
                if http_status == 401:
                    if refreshed:
                        raise CredentialError(reason='access_token_rejected')
                    refreshed = True
                    self._timing['auth_refreshes'] += 1
                    row['phase'] = diagnostic['phase'] = 'auth_refresh'
                    self._publish(diagnostic)
                    auth_started = time.monotonic()
                    try:
                        access_token = get_access_token(self.credentials_path, force_refresh=True)
                    finally:
                        self._timing['auth_seconds'] += max(0, time.monotonic() - auth_started)
                    continue
                if http_status == 404:
                    if resource_type == 'history':
                        raise HistoryExpired()
                    if resource_type in {'message', 'thread'}:
                        raise MessageUnavailable(resource_type, resource_id)
                if http_status == 403:
                    if http_reason in {'accessNotConfigured', 'SERVICE_DISABLED'}:
                        raise GmailAPIError('Gmail API must be enabled for this authorization', status=403,
                                            error_code='gmail_api_disabled')
                    if http_reason == 'insufficientPermissions':
                        raise CredentialError(reason='gmail_readonly_scope_missing')
                rate_limited = http_status == 429 or (http_status == 403 and http_reason in {'userRateLimitExceeded', 'rateLimitExceeded'})
                transient = problem == 'transport_error' or rate_limited or (http_status is not None and 500 <= http_status <= 599)
                if transient:
                    transient_failures += 1
                    if transient_failures < self.max_attempts:
                        self._timing['retry_attempts'] += 1
                        row['phase'] = diagnostic['phase'] = 'retry_wait'
                        self._publish(diagnostic)
                        delay_started = time.monotonic()
                        time.sleep(max(retry_after, min(4.0, 0.5 * 2 ** (transient_failures - 1))))
                        self._timing['retry_wait_seconds'] += max(0, time.monotonic() - delay_started)
                        continue
                if problem:
                    code = 'mail_provider_rate_limited' if rate_limited or http_reason == 'dailyLimitExceeded' else 'mail_provider_failed'
                    raise GmailAPIError('Gmail request did not complete', status=http_status, error_code=code)
                if data is None or len(data) > MAX_RESPONSE_BYTES:
                    raise GmailResponseError('Gmail response exceeds the supported size')
                row['phase'] = diagnostic['phase'] = 'validation'
                self._publish(diagnostic)
                validation_started = time.monotonic()
                try:
                    try:
                        result = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise GmailResponseError('Gmail response is not valid JSON') from None
                    if not isinstance(result, dict):
                        raise GmailResponseError('Gmail response is not an object')
                    if validate is not None:
                        result = validate(result)
                finally:
                    elapsed = max(0, time.monotonic() - validation_started)
                    self._timing['validation_seconds'] += elapsed
                    attempt['validation_seconds'] = round(elapsed, 6)
                self._timing['completed_count'] += 1
                metrics['completed'] += 1
                diagnostic['status'] = 'complete'
                diagnostic['pending_count'] = row['pending_count'] = 0
                return result
        except Exception as exc:
            diagnostic['status'] = 'failed'
            diagnostic['error_code'] = getattr(exc, 'error_code', 'mail_provider_failed')
            raise
        finally:
            diagnostic['phase'] = 'finished'
            diagnostic['finished_at'] = _now()
            diagnostic['elapsed_seconds'] = round(max(0, time.monotonic() - started), 6)
            del self._active[ident]
            self._publish(diagnostic)

    def profile(self):
        def validate(result):
            if not isinstance(result.get('emailAddress'), str) or not result['emailAddress'] or not str(result.get('historyId', '')).isdigit():
                raise GmailResponseError('Gmail profile is incomplete')
            return result
        return self._request('profile', '/profile', validate=validate)

    def history(self, start_history_id, token=None):
        if not str(start_history_id).isdigit():
            raise ValueError('Gmail history cursor is invalid')
        parameters = {'startHistoryId': str(start_history_id), 'maxResults': 500}
        if token:
            parameters['pageToken'] = token
        def validate(result):
            if not isinstance(result.get('history', []), list) or not str(result.get('historyId', '')).isdigit():
                raise GmailResponseError('Gmail history response is incomplete')
            self._page_token(result)
            return result
        return self._request('history', '/history', parameters, resource_type='history', validate=validate)

    @staticmethod
    def _page_token(result):
        token = result.get('nextPageToken')
        if token is not None and (not isinstance(token, str) or not token):
            raise GmailResponseError('Gmail pagination token is invalid')
        return token

    def search(self, query, token=None):
        if not isinstance(query, str):
            raise ValueError('Gmail search query must be text')
        parameters = {'q': query, 'maxResults': 500, 'includeSpamTrash': 'false'}
        if token:
            parameters['pageToken'] = token
        def validate(result):
            messages = result.get('messages', [])
            if not isinstance(messages, list):
                raise GmailResponseError('Gmail search messages are invalid')
            mappings = {}
            for message in messages:
                if not isinstance(message, dict):
                    raise GmailResponseError('Gmail search message is invalid')
                message_id, thread_id = _identifier(message.get('id')), _identifier(message.get('threadId'))
                if message_id in mappings and mappings[message_id] != thread_id:
                    raise GmailResponseError('Gmail search associations conflict')
                mappings[message_id] = thread_id
            return mappings, self._page_token(result)
        mappings, next_token = self._request('search_email_ids', '/messages', parameters, validate=validate)
        self._message_threads.update(mappings)
        return list(mappings), next_token

    def search_many(self, queries):
        return [self.search(query) for query in queries]

    def message_thread_ids(self, ids):
        """Return only mappings observed in verified API responses; no I/O."""
        return {message_id: self._message_threads[message_id] for message_id in _unique(ids)
                if message_id in self._message_threads}

    def get_messages(self, ids, format='metadata'):
        if format not in {'metadata', 'minimal', 'full', 'raw'}:
            raise ValueError('Unsupported Gmail message format')
        results = []
        for message_id in _unique(ids):
            def validate(value):
                message = normalize_message(value, expected_id=message_id)
                if format == 'raw':
                    message['payload'] = _native_raw_payload(value.get('raw'))
                return message
            message = self._request('read_email:' + format, '/messages/' + quote(message_id, safe=''),
                                    {'format': format}, resource_type='message', resource_id=message_id,
                                    validate=validate)
            self._message_threads[message_id] = message['thread_id']
            results.append(message)
        return results

    def _thread(self, value, expected_id):
        if not isinstance(value, dict) or value.get('id') != expected_id:
            raise GmailResponseError('Gmail returned a different conversation')
        if value.get('nextPageToken') or value.get('truncated'):
            raise GmailResponseError('Gmail conversation is incomplete')
        if not isinstance(value.get('messages'), list) or not value['messages']:
            raise GmailResponseError('Gmail conversation has no complete messages')
        messages = [normalize_message(message, expected_thread=expected_id)
                    for message in value['messages']]
        if len({message['id'] for message in messages}) != len(messages):
            raise GmailResponseError('Gmail conversation contains duplicate messages')
        result = copy.deepcopy(value)
        result['messages'] = messages
        if 'historyId' in result:
            result['history_id'] = result.pop('historyId')
        return result

    def _repair_body(self, message):
        if set(message.get('label_ids', [])) & {'DRAFT', 'SPAM', 'TRASH'}:
            return
        payload = message.get('payload') or {}
        if set(headers(payload)) & {'list-id', 'list-unsubscribe'}:
            return
        try:
            self._validate(top_body, payload)
            return
        except BodyError as exc:
            if str(exc) == 'Message has no readable author body':
                try:
                    # Quoted-only/forwarded text is complete MIME, not a fetch
                    # failure; the classifier decides whether it is usable.
                    self._validate(top_body, payload, include_quoted=True)
                    return
                except BodyError:
                    pass
        repaired = self.get_messages([message['id']], format='raw')[0]
        if repaired['thread_id'] != message['thread_id']:
            raise GmailResponseError('Gmail raw message belongs to another conversation')
        # Keep original full response labels/time; raw is only MIME repair.
        message['payload'] = repaired['payload']

    def read(self, ids, threads=False):
        requested = _unique(ids)
        associations = {}
        if threads:
            thread_ids = requested
        else:
            for message_id in requested:
                if message_id in self._message_threads:
                    self._timing['mapping_hits'] += 1
                else:
                    self._timing['mapping_misses'] += 1
                    self.get_messages([message_id], format='minimal')
                associations[message_id] = self._message_threads[message_id]
            thread_ids = list(dict.fromkeys(associations.values()))
        results = []
        for thread_id in thread_ids:
            thread = self._request('read_email_thread', '/threads/' + quote(thread_id, safe=''),
                                   {'format': 'full'}, resource_type='thread', resource_id=thread_id,
                                   validate=lambda value: self._thread(value, thread_id))
            present = {message['id'] for message in thread['messages']}
            for message_id, mapped_thread in associations.items():
                if mapped_thread != thread_id or message_id in present:
                    continue
                # Never use a sibling request's response as proof. A direct
                # message GET must establish this exact message/thread pair.
                direct = self.get_messages([message_id], format='full')[0]
                if direct['thread_id'] != thread_id:
                    raise GmailResponseError('Gmail requested message association does not match')
                thread['messages'].append(direct)
                present.add(message_id)
            for message in thread['messages']:
                self._repair_body(message)
            self._message_threads.update({message['id']: thread_id for message in thread['messages']})
            results.append(thread)
        return results
