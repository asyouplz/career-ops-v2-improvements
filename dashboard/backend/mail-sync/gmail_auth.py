"""Private, read-only Gmail OAuth credentials. No tokens are printed by this CLI."""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
PROFILE_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/profile'
DEFAULT_CREDENTIALS = Path('~/.config/career-dashboard/gmail.json').expanduser()
FLOW_SECONDS = 600


class CredentialError(Exception):
    error_code = 'mail_auth_required'

    def __init__(self, message='Gmail authorization is required', reason='authorization_required'):
        super().__init__(message)
        self.reason = reason


def _failure(reason):
    return CredentialError('Gmail authorization could not be completed (' + reason + ')', reason)


def _private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise _failure('credentials_permissions')


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise _failure('credentials_permissions')
            value = json.loads(stream.read(65537))
            if not isinstance(value, dict): raise _failure('credentials_invalid')
            return value
    except FileNotFoundError:
        return {}
    except CredentialError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise _failure('credentials_unreadable') from None


def _write(path, value):
    _private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextlib.contextmanager
def _locked(path):
    path = Path(path).expanduser()
    try:
        _private_directory(path.parent)
        fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'a+') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise _failure('credentials_permissions')
            deadline = time.monotonic() + 35
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise _failure('credentials_busy')
                    time.sleep(0.05)
            yield path
    except CredentialError:
        raise
    except OSError:
        raise _failure('credentials_unavailable') from None


def _configured(value):
    return value.get('client_type') in {'web', 'installed'} and all(
        isinstance(value.get(key), str) and 0 < len(value[key]) <= 4096 for key in ('client_id', 'client_secret'))


def _scopes(value):
    scopes = value.get('scopes', [])
    if isinstance(scopes, str): scopes = scopes.split()
    return set(scopes) if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) else set()


def _account(value):
    account = value.get('account')
    email = account.get('email') if isinstance(account, dict) else None
    if isinstance(email, str) and 3 <= len(email) <= 320 and '@' in email and not any(ord(c) < 33 for c in email):
        return email
    return None


def _scope_allowed(value):
    scopes = _scopes(value)
    return SCOPE in scopes and (scopes == {SCOPE} or value.get('grant_source') == 'existing_authorized_user')


def _verified_account(value):
    return bool(_account(value) and isinstance(value.get('account'), dict) and value['account'].get('verified_at'))


def connection_status(path):
    """Local credential readiness only; does not claim a live network check."""
    path = Path(path).expanduser()
    result = {'credentials_present': path.exists() or path.is_symlink(), 'configured': False,
              'connected': False, 'account': None, 'setup_required': True, 'reason': 'not_configured',
              'client_type': None, 'can_authorize_web': False, 'read_only': True, 'broader_existing_grant': False}
    try:
        value = _read(path)
        configured = _configured(value)
        account = _account(value)
        connected = configured and bool(value.get('refresh_token')) and _scope_allowed(value) and _verified_account(value) and not value.get('auth_error')
        result.update(configured=configured, connected=connected, account=account,
                      setup_required=not connected, client_type=value.get('client_type') if configured else None,
                      can_authorize_web=configured and value['client_type'] == 'web',
                      broader_existing_grant=value.get('grant_source') == 'existing_authorized_user' and bool(_scopes(value) - {SCOPE}),
                      reason=None if connected else 'authorization_required' if configured else 'not_configured')
        if value.get('auth_error'): result['reason'] = 'authorization_required'
    except CredentialError as error:
        result['reason'] = error.reason
    return result


def configure(client_file, credentials=DEFAULT_CREDENTIALS, token_file=None, verify=False):
    """Import only the explicit client JSON; never discover or copy other grants."""
    destination = Path(credentials).expanduser().resolve()
    if any(destination == Path(source).expanduser().resolve() for source in (client_file, token_file) if source is not None):
        raise _failure('credentials_source_overlap')
    try:
        source = json.loads(Path(client_file).expanduser().read_text(encoding='utf-8'))
        kinds = [kind for kind in ('web', 'installed') if isinstance(source.get(kind), dict)]
        if len(kinds) != 1: raise _failure('client_file_invalid')
        kind = kinds[0]; client = source[kind]
        value = {'version': 1, 'client_type': kind, 'client_id': client.get('client_id'),
                 'client_secret': client.get('client_secret'), 'redirect_uris': client.get('redirect_uris', []),
                 'scopes': [SCOPE]}
        if not _configured(value) or not isinstance(value['redirect_uris'], list) or any(not isinstance(uri, str) for uri in value['redirect_uris']):
            raise _failure('client_file_invalid')
        if token_file is not None:
            grant = json.loads(Path(token_file).expanduser().read_text(encoding='utf-8'))
            if not isinstance(grant, dict) or grant.get('type') != 'authorized_user': raise _failure('token_file_invalid')
            if grant.get('client_id') != value['client_id'] or grant.get('client_secret') != value['client_secret']:
                raise _failure('client_token_mismatch')
            if grant.get('token_uri') != TOKEN_URL or not isinstance(grant.get('refresh_token'), str) or not grant['refresh_token']:
                raise _failure('token_file_invalid')
            if SCOPE not in _scopes(grant): raise _failure('readonly_scope_missing')
            email = grant.get('account')
            if email and not _account({'account': {'email': email}}): raise _failure('account_unverified')
            expiry = grant.get('expiry')
            try:
                if isinstance(expiry, str):
                    parsed = dt.datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    expiry = parsed.replace(tzinfo=dt.timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
                expiry = float(expiry or 0)
                if not math.isfinite(expiry): raise ValueError()
            except (ValueError, TypeError): raise _failure('token_file_invalid')
            value.update(refresh_token=grant['refresh_token'], access_token=grant.get('token'), expiry=expiry,
                         scopes=sorted(_scopes(grant)), imported_scopes=sorted(_scopes(grant)),
                         grant_source='existing_authorized_user', auth_error=None,
                         account={'email': email, 'verified_at': None} if email else None)
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise _failure('client_file_unreadable') from None
    with _locked(credentials) as path:
        old = _read(path)
        if _account(old) and (old.get('client_id'), old.get('client_secret')) != (value['client_id'], value['client_secret']):
            raise _failure('existing_connection_requires_relink')
        if token_file is not None and _account(old):
            if _account(value) and _account(old).casefold() != _account(value).casefold():
                raise _failure('account_mismatch_requires_relink')
            # Imported tokens must prove the same identity before replacing a verified grant.
            value = _refresh_import(value)
            profile = _request_json(PROFILE_URL, token=value['access_token'])
            if not isinstance(profile.get('emailAddress'), str) or profile['emailAddress'].casefold() != _account(old).casefold():
                raise _failure('account_mismatch_requires_relink')
            value['account'] = old['account']
        if token_file is None:
            value['scopes'] = old.get('scopes', [SCOPE])
        # Re-importing the same client must not erase its grant or account.
        _write(path, {**old, **value})
    if verify: return verify_connection(credentials)
    return connection_status(credentials)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_json(url, fields=None, token=None):
    if url not in {TOKEN_URL, PROFILE_URL}: raise _failure('endpoint_not_allowed')
    if fields is not None and fields.get('grant_type') not in {'refresh_token', 'authorization_code'}:
        raise _failure('grant_not_allowed')
    headers = {'Accept': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    payload = urlencode(fields).encode() if fields is not None else None
    if payload is not None: headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=payload, headers=headers), timeout=25) as response:
            value = json.loads(response.read(65537))
        if not isinstance(value, dict): raise _failure('oauth_response_invalid')
        return value
    except HTTPError as error:
        # Do not include response text, URL, authorization code or token in errors.
        reason = 'oauth_request_failed'
        if url == TOKEN_URL and error.code == 400:
            try:
                if json.loads(error.read(65537)).get('error') == 'invalid_grant': reason = 'grant_revoked'
            except (ValueError, AttributeError, OSError): pass
        raise _failure(reason) from None
    except CredentialError:
        raise
    except (OSError, URLError, ValueError, UnicodeError, http.client.HTTPException):
        raise _failure('oauth_unavailable') from None


def _token_fields(response, previous=None):
    token = response.get('access_token')
    refresh = response.get('refresh_token') or (previous or {}).get('refresh_token')
    scope = response.get('scope')
    scopes = set(scope.split()) if isinstance(scope, str) else _scopes(previous or {})
    lifetime = response.get('expires_in')
    if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
        raise _failure('refresh_token_missing')
    if SCOPE not in scopes or (scopes != {SCOPE} and ((previous or {}).get('grant_source') != 'existing_authorized_user' or not scopes <= _scopes(previous or {}))):
        raise _failure('scope_not_readonly')
    if not isinstance(response.get('token_type', 'Bearer'), str) or response.get('token_type', 'Bearer').casefold() != 'bearer': raise _failure('oauth_response_invalid')
    if not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool) or not math.isfinite(lifetime) or lifetime <= 0:
        raise _failure('oauth_response_invalid')
    return {'access_token': token, 'refresh_token': refresh, 'expiry': time.time() + lifetime,
            'scopes': sorted(scopes), 'auth_error': None}


def _refresh_import(value):
    response = _request_json(TOKEN_URL, {'grant_type': 'refresh_token', 'client_id': value['client_id'],
                                       'client_secret': value['client_secret'], 'refresh_token': value['refresh_token']})
    return {**value, **_token_fields(response, value)}


def get_access_token(path, force_refresh=False):
    with _locked(path) as path:
        value = _read(path)
        if not _configured(value) or not _verified_account(value) or not value.get('refresh_token') or value.get('auth_error') or not _scope_allowed(value):
            raise _failure('authorization_required')
        expiry = value.get('expiry')
        if not force_refresh and isinstance(expiry, (int, float)) and expiry > time.time() + 60 and isinstance(value.get('access_token'), str) and value['access_token']:
            return value['access_token']
        try:
            response = _request_json(TOKEN_URL, {'grant_type': 'refresh_token', 'client_id': value['client_id'],
                                               'client_secret': value['client_secret'], 'refresh_token': value['refresh_token']})
            updated = {**value, **_token_fields(response, value)}
        except CredentialError as error:
            if error.reason in {'grant_revoked', 'scope_not_readonly'}:
                _write(path, {**value, 'auth_error': error.reason, 'access_token': None, 'expiry': 0})
            raise
        _write(path, updated)
        return updated['access_token']


def verify_connection(path):
    """Explicit CLI verification of imported credentials; never changes career records."""
    with _locked(path) as path:
        value = _read(path)
        if not _configured(value) or not value.get('refresh_token') or not _scope_allowed(value):
            raise _failure('authorization_required')
        updated = _refresh_import(value)
        profile = _request_json(PROFILE_URL, token=updated['access_token'])
        account = {'email': profile.get('emailAddress'), 'verified_at': time.time()}
        if not _account({'account': account}): raise _failure('account_unverified')
        if _account(value) and _account(value).casefold() != account['email'].casefold():
            raise _failure('account_mismatch_requires_relink')
        _write(path, {**updated, 'account': value['account'] if _verified_account(value) else account})
    return connection_status(path)


def _client_revision(value):
    return hashlib.sha256(json.dumps({key: value.get(key) for key in ('client_type', 'client_id', 'client_secret', 'redirect_uris')}, sort_keys=True).encode()).hexdigest()


def prepare_authorization(path, redirect_uri, installed=False):
    value = _read(Path(path).expanduser())
    if not _configured(value): raise _failure('not_configured')
    parsed = urlsplit(redirect_uri)
    loopback = parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and bool(parsed.port)
    if installed:
        if value['client_type'] != 'installed' or not loopback: raise _failure('installed_client_required')
    else:
        if value['client_type'] != 'web': raise _failure('loopback_authorization_required')
        if parsed.scheme != 'https' and not loopback: raise _failure('callback_origin_invalid')
        if redirect_uri not in value.get('redirect_uris', []): raise _failure('callback_not_registered')
    if parsed.username or parsed.password or parsed.query or parsed.fragment: raise _failure('callback_origin_invalid')
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    parameters = {'client_id': value['client_id'], 'redirect_uri': redirect_uri, 'response_type': 'code',
                  'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'false',
                  'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    return {'authorization_url': AUTH_URL + '?' + urlencode(parameters), 'state': state,
            'code_verifier': verifier, 'redirect_uri': redirect_uri, 'expires_at': time.time() + FLOW_SECONDS,
            'client_revision': _client_revision(value)}


def complete_authorization(path, code, flow):
    if not isinstance(code, str) or not 0 < len(code) <= 8192 or flow.get('expires_at', 0) <= time.time():
        raise _failure('authorization_expired')
    with _locked(path) as path:
        old = _read(path)
        if not _configured(old) or not hmac.compare_digest(_client_revision(old), flow.get('client_revision', '')):
            raise _failure('configuration_changed')
        response = _request_json(TOKEN_URL, {'grant_type': 'authorization_code', 'code': code,
                                           'client_id': old['client_id'], 'client_secret': old['client_secret'],
                                           'redirect_uri': flow['redirect_uri'], 'code_verifier': flow['code_verifier']})
        fields = _token_fields(response, {'scopes': [SCOPE]})
        profile = _request_json(PROFILE_URL, token=fields['access_token'])
        account = {'email': profile.get('emailAddress'), 'verified_at': time.time()}
        if not _account({'account': account}): raise _failure('account_unverified')
        if _account(old) and _account(old).casefold() != account['email'].casefold():
            raise _failure('account_mismatch_requires_relink')
        _write(path, {**old, **fields, 'account': old['account'] if _verified_account(old) else account, 'grant_source': 'dashboard_readonly_oauth'})
    return connection_status(path)


def authorize_installed(path, open_browser=True):
    """Official desktop loopback flow, bound to 127.0.0.1 and a random port."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import webbrowser
    result = {}
    flow = None

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            valid = parsed.path == '/callback' and len(params.get('state', [])) == 1 and hmac.compare_digest(params['state'][0], flow['state'])
            if not valid:
                self.send_response(400); self.end_headers(); return
            result['received'] = True
            result['code'] = params.get('code', [None])[0] if len(params.get('code', [])) == 1 and not params.get('error') else None
            body = b'Authorization received. You may close this window.'
            self.send_response(200); self.send_header('Content-Type', 'text/plain')
            self.send_header('Cache-Control', 'no-store'); self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    with HTTPServer(('127.0.0.1', 0), Callback) as listener:
        listener.timeout = 1
        flow = prepare_authorization(path, 'http://127.0.0.1:' + str(listener.server_port) + '/callback', installed=True)
        if open_browser: webbrowser.open(flow['authorization_url'])
        else:
            # Authorization URL contains a client ID/state/challenge, never a token or client secret.
            print(flow['authorization_url'], flush=True)
        while not result.get('received') and time.time() < flow['expires_at']:
            listener.handle_request()
    if not result.get('code'): raise _failure('authorization_cancelled_or_expired')
    return complete_authorization(path, result['code'], flow)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, default=DEFAULT_CREDENTIALS)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('configure'); setup.add_argument('--client-file', type=Path, required=True)
    setup.add_argument('--token-file', type=Path); setup.add_argument('--verify', action='store_true')
    setup.add_argument('--credentials', type=Path, default=argparse.SUPPRESS)
    status = commands.add_parser('status'); status.add_argument('--credentials', type=Path, default=argparse.SUPPRESS)
    auth = commands.add_parser('authorize'); auth.add_argument('--credentials', type=Path, default=argparse.SUPPRESS)
    auth.add_argument('--no-browser', action='store_true')
    check = commands.add_parser('verify'); check.add_argument('--credentials', type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == 'configure': result = configure(args.client_file, args.credentials, args.token_file, args.verify)
        elif args.command == 'authorize': result = authorize_installed(args.credentials, not args.no_browser)
        elif args.command == 'verify': result = verify_connection(args.credentials)
        else: result = connection_status(args.credentials)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except CredentialError as error:
        print(json.dumps({'ok': False, 'code': error.error_code, 'reason': error.reason})); return 1


if __name__ == '__main__': raise SystemExit(main())
