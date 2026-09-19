#!/usr/bin/env python3
"""Private Career dashboard. Standard library only; canonical tracker stays authoritative."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import hmac
import http.cookies
from html.parser import HTMLParser
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent / 'mail-sync'))
import gmail_auth

CANONICAL = ('Evaluated', 'Applied', 'Responded', 'Interview', 'Offer', 'Rejected', 'Discarded', 'SKIP', 'Hired')
APPLICATION = {'Applied', 'Responded', 'Interview', 'Offer', 'Rejected', 'Hired'}
STATUSES_KO = {'Evaluated': '검토 완료', 'Applied': '지원 완료', 'Responded': '기업 회신', 'Interview': '면접 진행', 'Offer': '오퍼 수령', 'Rejected': '불합격 / 전형 종료', 'Discarded': '마감 / 진행 종료', 'SKIP': '지원 제외', 'Hired': '입사 확정'}
SITES = {'wanted': '원티드', 'remember': '리멤버', 'saramin': '사람인', 'jobkorea': '잡코리아', 'jumpit': '점핏', 'linkedin': 'LinkedIn', 'jobplanet': '잡플래닛', 'direct': '기업 채용', 'greeting': '그리팅', 'ninehire': '나인하이어', 'headhunter_mail': '헤드헌터 메일', 'company_mail': '기업 채용 메일', 'unknown': '출처 확인 필요'}
SITE_DOMAINS = {'wanted.co.kr': 'wanted', 'rememberapp.co.kr': 'remember', 'jumpit.co.kr': 'jumpit', 'jumpit.saramin.co.kr': 'jumpit', 'saramin.co.kr': 'saramin', 'jobkorea.co.kr': 'jobkorea', 'linkedin.com': 'linkedin', 'jobplanet.co.kr': 'jobplanet', 'greetinghr.com': 'greeting', 'ninehire.com': 'ninehire'}
MAX_BODY = 8192
MAIL_PHASE_LABELS = {'starting': '메일 동기화 준비', 'restoring_cache': '저장된 대화 확인', 'search_messages': '지원 관련 메일 검색', 'read_recent_threads': '검색된 메일 대화 확인', 'read_known_threads': '기존 지원 대화 재확인', 'validate': '수집 결과 확인', 'apply': '지원 상태 반영', 'complete': '동기화 완료'}
MAIL_ERROR_MESSAGES = {
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
    'sync_interrupted': '메일 동기화 실행이 중단되었습니다. 다시 동기화하면 저장된 진행 지점부터 이어서 확인합니다.',
    'sync_state_unavailable': '메일 동기화의 실행 상태를 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.',
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class APIError(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def read_json(path, missing=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        if missing is not None:
            return copy.deepcopy(missing)
        raise APIError(503, 'data_missing', '수집 데이터가 아직 준비되지 않았습니다.')
    except (OSError, ValueError):
        raise APIError(503, 'data_unreadable', '저장된 데이터를 읽을 수 없습니다. 기존 기록을 보존하고 작업을 중단했습니다.')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def clean(value, limit=4000):
    return str(value or '')[:limit]


def normalize(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', clean(value)).strip()).casefold()


def safe_url(value):
    value = clean(value, 2048)
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        return value
    except ValueError:
        return ''


def canonical_url(value):
    value = safe_url(value)
    if not value:
        return ''
    p = urlsplit(value)
    host = (p.hostname or '').lower().rstrip('.')
    path = re.sub('/+', '/', unquote(p.path)).rstrip('/') or '/'
    pairs = dict(parse_qsl(p.query, keep_blank_values=True))
    if (host == 'saramin.co.kr' or host.endswith('.saramin.co.kr')) and pairs.get('rec_idx', '').isdigit():
        return 'https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=' + pairs['rec_idx']
    patterns = (
        ('linkedin.com', r'/jobs/view/(?:[^/]*-)?(\d+)', 'www.linkedin.com', '/jobs/view/'),
        ('wanted.co.kr', r'/wd/(\d+)', 'www.wanted.co.kr', '/wd/'),
        ('jobkorea.co.kr', r'/(?:Recruit/)?GI_Read/(\d+)', 'www.jobkorea.co.kr', '/Recruit/GI_Read/'),
        ('rememberapp.co.kr', r'/job(?:-posting|/posting)/(\d+)', 'career.rememberapp.co.kr', '/job/posting/'),
    )
    for suffix, pattern, domain, prefix in patterns:
        if host == suffix or host.endswith('.' + suffix):
            match = re.search(pattern, path, re.I)
            if match:
                return 'https://' + domain + prefix + match.group(1)
    keep = [(k, v) for k, v in pairs.items() if not k.lower().startswith('utm_') and k.lower() not in {'trk', 'trackingid', 'refid', 'ref', 'source', 'from', 'gclid', 'fbclid'}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(sorted(keep)), ''))


def aliases(job):
    result = set()
    for name in ('id', 'posting_cluster_id', 'listing_instance_id'):
        if job.get(name):
            result.add(str(job[name]))
    values = list(job.get('aliases') or [])
    values += [job.get(k) for k in ('url', 'canonical_url', 'apply_url', 'source_url')]
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        result.add(value)
        if value.startswith(('http:', 'https:')):
            result.add(canonical_url(value))
    result.discard('')
    if any(value.startswith(('li_', 'https://', 'http://')) for value in result):
        result = {value for value in result if not value.startswith('pc_')}
    return result


def job_id(job):
    ident = job.get('id')
    if ident and not str(ident).startswith('pc_'):
        return clean(ident, 256)
    ident = job.get('listing_instance_id') or next((a for a in job.get('aliases', []) if str(a).startswith('li_')), None)
    url = canonical_url(job.get('url', ''))
    return clean(ident or ('url_' + hashlib.sha256(url.encode()).hexdigest()[:20] if url else None) or job.get('posting_cluster_id') or job.get('id'), 256)


def posting_refs(job):
    """Group observed posting URLs by provider; different requisitions must stay separate."""
    result = {}
    for value in aliases(job):
        url = canonical_url(value)
        if not url:
            continue
        p = urlsplit(url)
        host = p.hostname or ''
        provider = next((domain for domain in ('wanted.co.kr', 'rememberapp.co.kr', 'saramin.co.kr', 'jobkorea.co.kr', 'linkedin.com', 'greetinghr.com', 'ninehire.com') if host == domain or host.endswith('.' + domain)), None)
        if provider or re.search(r'job|career|recruit', p.path, re.I):
            result.setdefault(provider or host, set()).add(url)
    return result


def conflicting_posting(job, tracker_job):
    left, right = posting_refs(job), posting_refs(tracker_job)
    return any(left[source].isdisjoint(right[source]) for source in left.keys() & right.keys())


def is_application(row, decision=None):
    return row['status'] in APPLICATION or (row['status'] == 'Discarded' and ((decision or {}).get('status') == 'applied' or bool(re.search(r'지원 완료|지원 정상|Applied\s+\d{4}-|60일 무응답', row.get('notes', ''), re.I))))


def source_site(raw):
    """Use a stored provider or original URL, never infer a site from the company."""
    value = raw.get('source_id') or raw.get('source') or raw.get('site') or raw.get('provider')
    if isinstance(value, dict):
        value = value.get('id') or value.get('name')
    value = clean(value, 100).strip()
    for sid, label in SITES.items():
        if normalize(value) in {normalize(sid), normalize(label)} and sid != 'unknown':
            return sid, label
    url = safe_url(raw.get('url') or raw.get('canonical_url') or raw.get('apply_url') or raw.get('source_url'))
    if url:
        host = (urlsplit(url).hostname or '').lower().rstrip('.')
        for domain, sid in SITE_DOMAINS.items():
            if host == domain or host.endswith('.' + domain):
                return sid, SITES[sid]
        # An explicit original URL proves this host, but not a named job platform.
        return host, host
    if value and value not in {'direct', 'unknown', '기존 지원 이력', '출처 확인 필요', '-', '—'}:
        return value, clean(raw.get('source_label') or raw.get('site_name') or value, 100)
    return 'unknown', SITES['unknown']


def archive_sources(items):
    groups = {}
    for item in items:
        sid = item['site']
        groups.setdefault(sid, {'id': sid, 'name': item['site_name'], 'total': 0})['total'] += 1
    return sorted(groups.values(), key=lambda group: (group['id'] == 'unknown', group['name']))


def public_job(raw):
    site, site_name = source_site(raw)
    reason = raw.get('reason') or raw.get('recommendation_reason') or raw.get('fit_reason') or raw.get('summary') or ''
    if isinstance(reason, list):
        reason = ' · '.join(map(str, reason))
    score = raw.get('score', raw.get('fit_score'))
    return {
        'id': job_id(raw), 'company': clean(raw.get('company') or raw.get('company_name'), 200),
        'title': clean(raw.get('title') or raw.get('role'), 400), 'site': site, 'site_name': site_name,
        'url': safe_url(raw.get('url') or raw.get('canonical_url') or raw.get('apply_url')),
        'score': score if isinstance(score, (int, float, str)) else None,
        'location': clean(raw.get('location'), 200), 'reason': clean(reason, 1200),
        'verified_at': clean(raw.get('verified_at') or raw.get('liveness_checked_at') or raw.get('checked_at'), 100),
        'published_at': clean(raw.get('published_at') or raw.get('posting_date') or raw.get('posted_at') or raw.get('date'), 100),
        'first_seen': clean(raw.get('first_seen'), 100), 'posting_date': clean(raw.get('posting_date'), 100),
        'date': clean(raw.get('date'), 100), 'decided_at': clean(raw.get('decided_at'), 100),
        'deadline': clean(raw.get('deadline') or raw.get('closes_at'), 100),
        'aliases': sorted(aliases(raw)), 'tracker_id': raw.get('tracker_id'),
        'canonical_status': raw.get('canonical_status'), 'decision_status': 'new', 'updated_at': '',
        'canonical_status_label': STATUSES_KO.get(raw.get('canonical_status'), raw.get('canonical_status')),
        'dashboard_eligible': raw.get('dashboard_eligible') is True, 'liveness': raw.get('liveness'),
    }


class Config:
    def __init__(self):
        self.host = os.environ.get('DASHBOARD_HOST', '127.0.0.1')
        self.port = int(os.environ.get('DASHBOARD_PORT', '9121'))
        self.root = Path(os.environ.get('CAREER_OPS_ROOT', str(Path(__file__).resolve().parents[2] / 'engine'))).resolve()
        self.static = Path(os.environ.get('DASHBOARD_STATIC_DIR', str(Path(__file__).resolve().parents[1] / 'web/dist/client'))).resolve()
        self.data = Path(os.environ.get('DASHBOARD_DATA_DIR', str(self.root / 'data'))).resolve()
        self.export = self.data / 'dashboard-candidates.json'
        self.decisions = self.data / 'dashboard-decisions.json'
        self.tracker = Path(os.environ.get('CAREER_OPS_TRACKER', str(self.data / 'applications.md')))
        self.node = os.environ.get('DASHBOARD_NODE', shutil.which('node') or 'node')
        self.origin = os.environ.get('DASHBOARD_PUBLIC_ORIGIN', f'http://127.0.0.1:{self.port}').rstrip('/')
        self.password_hash = os.environ.get('DASHBOARD_PASSWORD_HASH', '')
        self.dev = os.environ.get('DASHBOARD_DEV_NO_AUTH') == '1'
        self.session_seconds = int(os.environ.get('DASHBOARD_SESSION_SECONDS', '604800'))
        self.stale_hours = int(os.environ.get('DASHBOARD_STALE_HOURS', '30'))
        self.mail_sync_command = os.environ.get('DASHBOARD_MAIL_SYNC_COMMAND', '')
        self.gmail_credentials = Path(os.environ.get('CAREER_GMAIL_CREDENTIALS', os.environ.get('DASHBOARD_GMAIL_CREDENTIALS', str(gmail_auth.DEFAULT_CREDENTIALS)))).expanduser()


TRACKER_READ_JS = r'''
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const {resolveColumns,parseTrackerRow}=await import(pathToFileURL(process.env.DASHBOARD_PARSER));
const lines=readFileSync(process.env.CAREER_OPS_TRACKER,'utf8').split(/\r?\n/);
const columns=resolveColumns(lines);
const rows=lines.map(line=>parseTrackerRow(line,columns)).filter(Boolean).map(({raw,...row})=>({...row,raw_hash:createHash('sha256').update(raw,'utf8').digest('hex')}));
console.log(JSON.stringify(rows));
'''


class Store:
    def __init__(self, config):
        self.c = config
        self.mutex = threading.RLock()
        self._tracker_cache = None
        self._tracker_stamp = None
        self._report_url_cache = {}
        self._lock_local = threading.local()
        self._mail_process = None
        self._mail_launch_at = None
        self._mail_history_cache = None

    def mail_connection(self):
        value = gmail_auth.connection_status(self.c.gmail_credentials)
        value['provider'] = 'gmail' if value['credentials_present'] else 'codex'
        value['can_authorize'] = False
        if value['connected']:
            message = 'Gmail에 연결되어 있습니다. 메일을 읽어 지원 상태를 확인합니다.'
        elif not value['credentials_present']:
            message = '현재 기존 메일 연결을 사용합니다. Gmail 직접 연결은 서버에서 인증 파일을 설정한 뒤 사용할 수 있습니다.'
        elif value['configured'] and value['client_type'] == 'installed':
            message = '설치형 Google 인증이 설정되어 있습니다. 서버의 인증 도구에서 계정 연결을 완료해 주세요.'
        elif value['configured']:
            try:
                self.gmail_callback_uri()
                gmail_auth.prepare_authorization(self.c.gmail_credentials, self.gmail_callback_uri())
                value['can_authorize'] = True
                message = 'Google 계정을 연결하면 변경된 메일을 직접 확인할 수 있습니다.'
            except gmail_auth.CredentialError:
                message = 'Google 웹 인증의 반환 주소를 서버 설정과 일치시켜야 합니다. 서버 설정을 확인해 주세요.'
        else:
            message = '저장된 Gmail 인증 설정을 확인해야 합니다. 기존 기록은 유지됩니다.'
        value['message'] = message
        return value

    def gmail_callback_uri(self):
        origin = self.c.origin
        parsed = urlsplit(origin)
        loopback = parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (parsed.scheme != 'https' and not loopback) or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise gmail_auth.CredentialError(reason='callback_origin_invalid')
        return origin + '/api/mail-connection/callback'

    @contextlib.contextmanager
    def lock(self):
        with self.mutex:
            if getattr(self._lock_local, 'depth', 0):
                self._lock_local.depth += 1
                try:
                    yield
                finally:
                    self._lock_local.depth -= 1
                return
            self.c.data.mkdir(parents=True, exist_ok=True)
            with open(str(self.c.decisions) + '.lock', 'a') as stream:
                os.chmod(stream.name, 0o600)
                fcntl.flock(stream, fcntl.LOCK_EX)
                self._lock_local.depth = 1
                try:
                    yield
                finally:
                    self._lock_local.depth = 0
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def run_node(self, args, extra_env=None):
        env = dict(os.environ, CAREER_OPS_TRACKER=str(self.c.tracker), CAREER_OPS_TRACKER_LOCK_TIMEOUT_MS='8000')
        env.update(extra_env or {})
        try:
            result = subprocess.run([self.c.node, *args], cwd=self.c.root, env=env, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            raise APIError(503, 'tracker_unavailable', '지원 이력 처리에 응답이 없습니다. 새로고침하여 상태를 확인한 뒤 다시 시도해 주세요.')
        if result.returncode:
            if result.returncode == 5:
                raise APIError(409, 'tracker_conflict', '다른 작업에서 지원 이력이 변경되었습니다. 새로고침 후 다시 선택해 주세요.')
            # CLI stderr may contain private mail notes or paths; never send it to a browser/log.
            raise APIError(503, 'tracker_write_failed', '기존 지원 이력 도구가 작업을 완료하지 못했습니다. 현재 상태를 다시 확인해 주세요.')
        return result.stdout

    def tracker_rows(self):
        try:
            s = self.c.tracker.stat()
            stamp = (s.st_mtime_ns, s.st_size)
        except OSError:
            raise APIError(503, 'tracker_missing', '기존 지원 이력을 읽을 수 없습니다.')
        with self.mutex:
            if stamp == self._tracker_stamp:
                return copy.deepcopy(self._tracker_cache)
            output = self.run_node(['--input-type=module', '-e', TRACKER_READ_JS], {'DASHBOARD_PARSER': str(self.c.root / 'tracker-parse.mjs')})
            try:
                rows = json.loads(output)
                if not isinstance(rows, list) or any(not isinstance(x, dict) or x.get('status') not in CANONICAL for x in rows):
                    raise ValueError()
            except ValueError:
                raise APIError(503, 'tracker_invalid', '지원 이력의 상태 형식을 확인해야 합니다. 기록을 변경하지 않았습니다.')
            self._tracker_stamp, self._tracker_cache = stamp, rows
            return copy.deepcopy(rows)

    def decision_data(self):
        value = read_json(self.c.decisions, {'version': 1, 'updated_at': '', 'decisions': {}})
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('decisions'), dict):
            raise APIError(503, 'decisions_invalid', '검토 기록 형식을 확인해야 합니다. 기존 기록을 보존했습니다.')
        for key, decision in value['decisions'].items():
            if not isinstance(decision, dict) or decision.get('status') not in {'new', 'pending', 'excluded', 'applied'}:
                raise APIError(503, 'decisions_invalid', '검토 기록 형식을 확인해야 합니다. 기존 기록을 보존했습니다.')
        return value

    def export_data(self):
        data = read_json(self.c.export)
        if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
            raise APIError(503, 'export_invalid', '공고 수집 결과의 형식을 확인해야 합니다.')
        return data

    def report_url(self, row):
        """Read only original-URL headers in reports explicitly linked by this row."""
        destinations = re.findall(r'\[[^\]\r\n]*\]\(([^)\r\n]+)\)', clean(row.get('report'), 4000))
        urls = set()
        report_root = (self.c.root / 'reports').resolve()
        for destination in destinations[:5]:
            try:
                if urlsplit(destination).scheme or Path(destination).is_absolute():
                    continue
                path = (self.c.tracker.parent / unquote(destination)).resolve()
            except (OSError, ValueError):
                continue
            if not path.is_relative_to(report_root) or path.suffix.lower() != '.md':
                continue
            try:
                stat = path.stat()
                if stat.st_size > 262144:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._report_url_cache.get(path)
                if cached and cached[0] == stamp:
                    urls.update(cached[1])
                    continue
                with path.open(encoding='utf-8') as stream:
                    header = stream.read(16384)
            except (OSError, UnicodeError):
                continue
            found = set()
            for line in header.splitlines()[:120]:
                if re.match(r'^\s*(?:---+\s*$|##\s)', line):
                    break
                match = re.match(r'^\s*(?:\*\*URL:\*\*|(?:URL|공고 URL|원문 URL):)\s*(.+?)\s*$', line, re.I)
                if match:
                    for value in re.findall(r'https?://[^\s<>\]\)]+', match.group(1)):
                        value = safe_url(value.rstrip(';,.'))
                        if value:
                            found.add(value)
            self._report_url_cache[path] = (stamp, found)
            urls.update(found)
        return next(iter(urls)) if len(urls) == 1 else ''

    def tracker_job(self, row):
        urls = [safe_url(u.rstrip(';,.')) for u in re.findall(r'https?://[^\s<>\]\)]+', row.get('notes', ''))]
        urls = [url for url in urls if url]
        original_url = self.report_url(row) or (urls[0] if urls else '')
        # Via can name a recruiter. Only an exact platform name proves a job site.
        via = normalize(row.get('via'))
        platform = next((sid for sid, name in SITES.items() if sid not in {'direct', 'unknown'} and via in {normalize(sid), normalize(name)}), None)
        job = public_job({'id': f'tracker_{row["num"]}', 'company': row['company'], 'title': row['role'], 'score': row.get('score'), 'site': None if original_url else platform, 'url': original_url, 'location': row.get('location'), 'date': row.get('date')})
        job.update(tracker_id=row['num'], canonical_status=row['status'], canonical_status_label=STATUSES_KO[row['status']], note=clean(row.get('notes')), updated_at=self.row_revision(row), date=row.get('date'), decision_status='applied' if row['status'] in APPLICATION else 'excluded' if row['status'] in {'SKIP', 'Discarded'} else 'new')
        if urls:
            job['aliases'] = sorted(set(job['aliases']) | set(urls) | {canonical_url(u) for u in urls})
        job['aliases'] = sorted(set(job['aliases']) | set(re.findall(r'dashboard-id:([^\s;]+)', row.get('notes', ''))))
        return job

    @staticmethod
    def row_revision(row):
        return row.get('raw_hash') or hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]

    def matched_row(self, job, rows, export_rows=None):
        tracker_id = job.get('tracker_id')
        if tracker_id is not None:
            matches = [r for r in rows if str(r['num']) == str(tracker_id)]
            if len(matches) == 1 and not conflicting_posting(job, self.tracker_job(matches[0])):
                return matches[0]
        job_aliases = aliases(job)
        matches = []
        for row in rows:
            if aliases(self.tracker_job(row)) & job_aliases and not conflicting_posting(job, self.tracker_job(row)):
                matches.append(row)
        if len(matches) == 1:
            return matches[0]
        # Exact normalized company AND full role only. Company alone may be another role.
        matches = [r for r in rows if normalize(r['company']) == normalize(job.get('company')) and normalize(r['role']) == normalize(job.get('title') or job.get('role')) and not conflicting_posting(job, self.tracker_job(r))]
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def matching_decision(job, decisions):
        aa = aliases(job)
        matches = [(key, d) for key, d in decisions.items() if key in aa or aa & (aliases(d) | aliases(d.get('job') or {}))]
        if not matches:
            return None, None
        return max(matches, key=lambda pair: pair[1].get('updated_at', ''))

    def snapshot(self):
        with self.mutex:
            export = self.export_data()
            decisions = self.decision_data()
            rows = self.tracker_rows()
            jobs = [public_job(j) for j in export['jobs'] if isinstance(j, dict)]
            # The cron may omit a pending/excluded posting from its next export.
            # Preserve its original bounded liveness evidence for an explicit restore.
            # Any current exported identity wins, including a newer expired/ineligible result.
            current_aliases = set().union(*(aliases(j) for j in jobs)) if jobs else set()
            for decision in decisions['decisions'].values():
                saved = decision.get('job')
                if isinstance(saved, dict) and saved.get('url') and not aliases(saved) & current_aliases:
                    restored = public_job(saved)
                    jobs.append(restored)
                    current_aliases |= aliases(restored)
            applications = []
            pending, excluded = [], []
            used_tracker = set()
            withdrawn_rows = {str(d.get('tracker_id')) for d in decisions['decisions'].values() if d.get('withdrawn') and d.get('status') == 'excluded'}
            # Lifecycle remains canonical even when a historic dashboard click says applied.
            for row in rows:
                if is_application(row) and str(row['num']) not in withdrawn_rows:
                    applications.append(self.tracker_job(row))
                    used_tracker.add(row['num'])
            sidecar_tracker = set()
            for key, d in decisions['decisions'].items():
                if d['status'] == 'new':
                    continue
                job = public_job(d.get('job') or {'id': key})
                job.update(id=key, aliases=sorted(aliases(job) | aliases(d)), note=clean(d.get('note')), updated_at=d.get('updated_at', ''), decided_at=d.get('effective_at') or d.get('updated_at', ''), decision_status=d['status'])
                row = self.matched_row(dict(job, tracker_id=d.get('tracker_id') or job.get('tracker_id')), rows)
                if row:
                    job.update(tracker_id=row['num'], canonical_status=row['status'], canonical_status_label=STATUSES_KO[row['status']])
                    if job['site'] == 'unknown':
                        linked = self.tracker_job(row)
                        job.update({k: linked[k] for k in ('site', 'site_name', 'url') if linked.get(k)})
                    sidecar_tracker.add(row['num'])
                    if is_application(row, d) and not d.get('withdrawn'):
                        if not any(item.get('tracker_id') == row['num'] for item in applications):
                            applications.append(self.tracker_job(row))
                        for item in applications:
                            if item['tracker_id'] == row['num']:
                                item.update({k: job[k] for k in ('url', 'site', 'site_name', 'aliases') if job.get(k)})
                        continue
                if d['status'] == 'pending':
                    pending.append(job)
                elif d['status'] == 'excluded':
                    excluded.append(job)
                elif d['status'] == 'applied' and not row:
                    job['sync_warning'] = '지원 이력 연결 확인 필요'
                    applications.append(job)
            for row in rows:
                if row['num'] in sidecar_tracker:
                    continue
                if row['status'] in {'SKIP', 'Discarded'}:
                    item = self.tracker_job(row)
                    # Discarded includes an application that ended without a company rejection.
                    if not is_application(row):
                        excluded.append(item)
            groups = {}
            for source in export.get('sources', []):
                if isinstance(source, str):
                    sid, name = source, SITES.get(source, source)
                elif isinstance(source, dict):
                    sid = clean(source.get('source_id') or source.get('id') or source.get('source') or source.get('name'))
                    name = clean(source.get('source_label') or source.get('name') or SITES.get(sid, sid))
                else:
                    continue
                groups[sid] = {'id': sid, 'name': name, 'total': 0, 'jobs': []}
            seen = set()
            for job in jobs:
                if not job['dashboard_eligible'] or job['liveness'] != 'active':
                    continue
                try:
                    verified = dt.datetime.fromisoformat(job['verified_at'].replace('Z', '+00:00'))
                    if verified.tzinfo is None:
                        verified = verified.replace(tzinfo=dt.timezone.utc)
                    age = (dt.datetime.now(dt.timezone.utc) - verified).total_seconds()
                    if not -300 <= age <= 72 * 3600:
                        continue
                except (ValueError, TypeError):
                    continue
                if aliases(job) & seen:
                    continue
                seen |= aliases(job)
                row = self.matched_row(job, rows)
                key, decision = self.matching_decision(job, decisions['decisions'])
                if row:
                    job.update(tracker_id=row['num'], canonical_status=row['status'])
                    if row['status'] != 'Evaluated':
                        continue
                if decision and decision['status'] != 'new':
                    continue
                if decision:
                    job['updated_at'] = decision.get('updated_at', '')
                sid = job['site']
                group = groups.setdefault(sid, {'id': sid, 'name': job['site_name'], 'total': 0, 'jobs': []})
                group['total'] += 1
                if len(group['jobs']) < 5:
                    group['jobs'].append(job)
            pending.sort(key=lambda j: j.get('decided_at') or j.get('updated_at', ''), reverse=True)
            excluded.sort(key=lambda j: j.get('decided_at') or j.get('date', ''), reverse=True)
            applications.sort(key=lambda j: j.get('date') or j.get('published_at') or j.get('updated_at', ''), reverse=True)
            source_run_at = export.get('source_run_at') or export.get('generated_at')
            stale = True
            if source_run_at:
                try:
                    timestamp = dt.datetime.fromisoformat(source_run_at.replace('Z', '+00:00'))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
                    stale = (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds() > self.c.stale_hours * 3600
                except (ValueError, TypeError):
                    pass
            warnings = list(export.get('warnings', []))
            if (self.c.data / 'dashboard-write-journal.json').exists():
                warnings.append('완료 여부를 확인해야 하는 상태 변경이 있습니다. 표시된 지원 이력과 검토 상태를 확인해 주세요.')
            if any(e.get('state') in {'prepared', 'verification_required'} for e in self.movement_data()['events']):
                warnings.append('분류 변경의 저장 결과를 확인해야 하는 기록이 있습니다. 해당 공고의 상세 이력을 확인해 주세요.')
            if stale:
                warnings.append('마지막 수집 시점이 오래되었거나 확인되지 않았습니다. 공고 원문에서 현재 모집 여부를 확인해 주세요.')
            status_counts = {s: sum(x.get('canonical_status') == s for x in applications) for s in CANONICAL}
            result = {'generated_at': export.get('generated_at'), 'source_run_at': source_run_at, 'updated_at': decisions.get('updated_at', ''), 'served_at': now(), 'sites': list(groups.values()), 'applications': applications, 'pending': {'items': pending[:5], 'total': len(pending), 'sources': archive_sources(pending)}, 'excluded': {'items': excluded[:5], 'total': len(excluded), 'sources': archive_sources(excluded)}, 'stats': {'recommended': sum(len(g['jobs']) for g in groups.values()), 'available': sum(g['total'] for g in groups.values()), 'applications': len(applications), 'pending': len(pending), 'excluded': len(excluded), 'application_statuses': status_counts}, 'canonical_statuses': [{'id': s, 'label': STATUSES_KO[s]} for s in CANONICAL], 'stale': stale, 'warnings': warnings}
            ledger = self.mail_ledger()
            for category, items in [('new', [j for g in groups.values() for j in g['jobs']]), ('applied', applications), ('pending', pending), ('excluded', excluded)]:
                for item in items:
                    self.decorate_job(item, category, decisions['decisions'], rows, ledger, export['jobs'])
            result['mail_sync'] = self.mail_sync_status()
            return result, {'pending': pending, 'excluded': excluded}, (export, decisions, rows, jobs)

    def dashboard(self):
        return self.snapshot()[0]

    def archive(self, status, offset, limit, site='all'):
        items = self.snapshot()[1][status]
        sources = archive_sources(items)
        if site != 'all':
            items = [item for item in items if item['site'] == site]
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'has_more': offset + limit < len(items), 'sources': sources, 'site': site}

    def backup_tracker(self):
        folder = self.c.data / 'dashboard-backups'
        folder.mkdir(mode=0o700, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        for path in (self.c.tracker, self.c.tracker.with_name('status-log.tsv')):
            if path.exists():
                destination = folder / (stamp + '-' + path.name)
                shutil.copy2(path, destination)
                destination.chmod(0o600)

    def set_canonical(self, row, status, note, event_date=None):
        args = ['set-status.mjs', '--row', str(row['num']), status, '--note', note, '--json']
        if event_date is not None:
            args += ['--on', event_date]
        if row.get('raw_hash'):
            args += ['--expected-revision', row['raw_hash'], '--expected-status', row['status']]
        self.run_node(args + ['--dry-run'])
        self.backup_tracker()
        self.run_node(args)
        self._tracker_stamp = None
        updated = next((r for r in self.tracker_rows() if r['num'] == row['num']), None)
        if not updated or updated['status'] != status:
            raise APIError(503, 'tracker_verification_failed', '지원 이력 변경 결과를 확인하지 못했습니다. 새로고침 후 현재 상태를 확인해 주세요.', partial_commit=True)
        self.run_node(['tracker.mjs', 'sync'])
        return updated

    def add_applied(self, job, note, rows):
        return self.add_canonical_record(job, note, rows, 'Applied')

    def add_canonical_record(self, job, note, rows, status, event_date=None):
        # Separate additions directory prevents accidentally merging another worker's pending batch.
        with tempfile.TemporaryDirectory(prefix='dashboard-add-', dir=self.c.data) as directory:
            number = max([r['num'] for r in rows] + [0]) + 1
            marker = 'dashboard-id:' + job['id']
            sanitize = lambda v: re.sub(r'[\t\r\n|]', ' ', str(v)).strip()
            date = event_date or dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
            description = '메일 근거에 따른 지원 이력 기록' if event_date else '사용자 대시보드에서 지원 완료 기록'
            notes = f'{status} {date}; {description}; {marker}; {job["url"]}'
            if note:
                notes += '; ' + note
            fields = [number, date, job['company'], job['title'], status, 'N/A', '-', '-', notes]
            addition = Path(directory) / 'dashboard.tsv'
            addition.write_text('\t'.join(sanitize(v) for v in fields) + '\n', encoding='utf-8')
            addition.chmod(0o600)
            extra = {'CAREER_OPS_ADDITIONS': directory, 'CAREER_OPS_BATCH_STATE': str(Path(directory) / 'unused-batch-state.tsv'), 'CAREER_OPS_EXPECTED_TRACKER_SHA256': hashlib.sha256(self.c.tracker.read_bytes()).hexdigest()}
            preview = self.run_node(['merge-tracker.mjs', '--dry-run'], extra)
            summary = re.search(r'Summary:\s*\+(\d+) added,\s*🔄(\d+) updated,\s*⏭️(\d+) skipped', preview)
            if not summary or tuple(map(int, summary.groups())) != (1, 0, 0):
                raise APIError(409, 'ambiguous_application_merge', '같은 회사의 유사한 직무 이력이 있어 기존 기록과 구분이 필요합니다. 기존 이력을 변경하지 않았습니다.')
            self.backup_tracker()
            self.run_node(['merge-tracker.mjs'], extra)
            self._tracker_stamp = None
            matches = [r for r in self.tracker_rows() if marker in r.get('notes', '')]
            if len(matches) != 1 or matches[0]['status'] != status:
                raise APIError(503, 'tracker_verification_failed', '지원 이력 저장 결과를 확인하지 못했습니다. 새로고침 후 현재 상태를 확인해 주세요.', partial_commit=True)
            self.run_node(['tracker.mjs', 'sync'])
            return matches[0]

    def _decide_legacy(self, body, restore_metadata=None, restore_canonical_status=None):
        status, ident = body.get('status'), body.get('id')
        if status not in {'pending', 'excluded', 'applied', 'new'} or not isinstance(ident, str) or not ident or len(ident) > 256:
            raise APIError(400, 'invalid_decision', '공고와 변경할 상태를 확인해 주세요.')
        note = body.get('note', '')
        if not isinstance(note, str) or len(note) > 1000 or any(ord(c) < 32 and c not in '\n\t' for c in note):
            raise APIError(400, 'invalid_note', '메모는 1,000자 이내로 입력해 주세요.')
        note = re.sub(r'[\r\n\t|]+', ' ', note).strip()
        with self.lock():
            dashboard, archive, (export, data, rows, jobs) = self.snapshot()
            job = next((j for j in jobs if j['id'] == ident or ident in aliases(j)), None)
            if job is None:
                decision = data['decisions'].get(ident)
                if decision:
                    job = public_job(decision.get('job') or {'id': ident})
                    job['id'] = ident
                elif ident.startswith('tracker_'):
                    row = next((r for r in rows if f'tracker_{r["num"]}' == ident), None)
                    if row:
                        job = self.tracker_job(row)
            if not job:
                raise APIError(404, 'job_missing', '해당 공고를 찾을 수 없습니다. 화면을 새로고침해 주세요.')
            key, previous = self.matching_decision(job, data['decisions'])
            key = key or job['id']
            current_revision = (previous or {}).get('updated_at', '')
            if 'expected_updated_at' in body and body['expected_updated_at'] != current_revision:
                # Tracker archive uses row revision rather than a decision timestamp.
                row_for_revision = self.matched_row(job, rows)
                if previous or not row_for_revision or body['expected_updated_at'] != self.row_revision(row_for_revision):
                    raise APIError(409, 'decision_conflict', '다른 화면에서 상태가 변경되었습니다. 새로고침 후 다시 선택해 주세요.')
            row = self.matched_row(job, rows)
            if row and (row['status'] == 'Discarded' or (previous or {}).get('previous_canonical_status') == 'Discarded') and status == 'new' and not self.can_restore_recommendation(job, previous):
                raise APIError(409, 'closed_history_not_restorable', '마감 또는 진행 종료 이력은 이 메뉴에서 추천으로 복원할 수 없습니다. 모집 중인 새 공고를 확인해 주세요.')
            if row and row['status'] in APPLICATION and status != 'applied':
                raise APIError(409, 'application_exists', '이미 지원 이력이 있습니다. 지원 현황에서 전형 상태를 변경해 주세요.')
            changed_at = now()
            record = {'id': key, 'status': status, 'note': note, 'updated_at': changed_at, 'origin': 'manual', 'effective_at': changed_at, 'aliases': sorted(aliases(job) | aliases(previous or {})), 'job': job}
            for field in ('ever_applied', 'withdrawn', 'mail_event_id', 'mail_source', 'needs_review'):
                if previous and field in previous:
                    record[field] = previous[field]
            if restore_metadata is not None:
                for field in ('reason', 'needs_review', 'withdrawn', 'ever_applied'):
                    if field in restore_metadata:
                        record[field] = restore_metadata[field]
                    else:
                        record.pop(field, None)
            record['job']['decided_at'] = record['updated_at']
            if previous and 'previous_canonical_status' in previous:
                record['previous_canonical_status'] = previous['previous_canonical_status']
            committed = False
            journal_path = self.c.data / 'dashboard-write-journal.json'
            if journal_path.exists():
                unfinished = read_json(journal_path)
                if not isinstance(unfinished, dict) or unfinished.get('decision', {}).get('id') != key:
                    raise APIError(409, 'unfinished_change', '완료 여부를 확인해야 하는 이전 상태 변경이 있습니다. 해당 공고의 상태를 확인하고 같은 변경을 다시 저장해 주세요.', partial_commit=True)
            atomic_json(journal_path, {'version': 1, 'stage': 'prepared', 'decision': record, 'started_at': now()})
            try:
                canonical_note = '사용자 대시보드: ' + {'applied': '지원 완료', 'excluded': '지원 제외', 'new': '다시 검토', 'pending': '지원보류'}[status]
                if note:
                    canonical_note += '; ' + note
                if restore_canonical_status is not None and row and row['status'] != restore_canonical_status:
                    committed = True
                    row = self.set_canonical(row, restore_canonical_status, '사용자 대시보드 실행 취소; ' + canonical_note)
                elif status == 'applied':
                    if row is None:
                        committed = True  # A failed CLI may still have committed its canonical write.
                        row = self.add_applied(job, note, rows)
                    elif row['status'] not in APPLICATION:
                        committed = True
                        row = self.set_canonical(row, 'Applied', canonical_note)
                elif status == 'excluded' and row and row['status'] not in {'SKIP', 'Discarded'}:
                    record.setdefault('previous_canonical_status', row['status'])
                    committed = True
                    row = self.set_canonical(row, 'SKIP', canonical_note)
                elif status in {'new', 'pending'} and row and row['status'] == 'SKIP':
                    committed = True
                    target = record.get('previous_canonical_status', 'Evaluated')
                    if status == 'new' and target == 'Discarded':
                        target = 'Evaluated'
                    row = self.set_canonical(row, target, canonical_note)
                elif status == 'new' and row and row['status'] == 'Discarded':
                    record.setdefault('previous_canonical_status', 'Discarded')
                    committed = True
                    row = self.set_canonical(row, 'Evaluated', canonical_note)
                if row:
                    record['tracker_id'] = row['num']
                    record['job']['tracker_id'] = row['num']
                    record['job']['canonical_status'] = row['status']
                data['decisions'][key] = record
                data['updated_at'] = record['updated_at']
                atomic_json(self.c.decisions, data)
                journal_path.unlink(missing_ok=True)
            except Exception as exc:
                if isinstance(exc, APIError):
                    if exc.code in {'ambiguous_application_merge', 'tracker_conflict'}:
                        # These guards reject before canonical mutation; do not strand the journal.
                        journal_path.unlink(missing_ok=True)
                        committed = False
                    if committed:
                        exc.details['partial_commit'] = True
                    raise
                raise APIError(503, 'decision_save_failed', '상태 저장을 완료하지 못했습니다. 화면을 새로고침하여 현재 기록을 확인해 주세요.', partial_commit=committed)
            try:
                updated_dashboard = self.dashboard()
            except APIError as exc:
                exc.details['partial_commit'] = True
                raise
            return {'ok': True, 'decision': record, 'dashboard': updated_dashboard}

    def mail_ledger(self):
        ledger = read_json(self.c.data / 'dashboard-mail-ledger.json', {'version': 1, 'events': {}, 'runs': {}})
        if ledger.get('version') != 1 or not isinstance(ledger.get('events'), dict):
            raise APIError(503, 'mail_ledger_invalid', '메일 처리 이력을 확인해야 합니다. 기존 기록을 보존했습니다.')
        return ledger

    def related_mail(self, job, ledger=None):
        ledger = ledger or self.mail_ledger()
        aa = aliases(job)
        return sorted((event for event in ledger['events'].values() if event.get('job_id') in aa or aa.intersection(event.get('job_aliases') or []) or (job.get('tracker_id') is not None and str(event.get('tracker_id')) == str(job['tracker_id']))), key=lambda e: (e.get('event_at', ''), e.get('event_id', '')))

    def decision_revision(self, job, decision, row, ledger=None):
        mail = self.related_mail(job, ledger)
        value = {'decision': decision, 'row': self.row_revision(row) if row else None, 'mail': mail}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def can_restore_recommendation(self, job, previous=None, exported=None):
        exported = self.export_data()['jobs'] if exported is None else exported
        current = next((j for j in exported if aliases(j) & aliases(job)), None)
        evidence = current or (previous or {}).get('job') or job
        try:
            verified = dt.datetime.fromisoformat(str(evidence.get('verified_at', '')).replace('Z', '+00:00'))
            age = (dt.datetime.now(dt.timezone.utc) - verified).total_seconds()
        except (ValueError, TypeError):
            age = float('inf')
        return bool(evidence.get('dashboard_eligible') and evidence.get('liveness') == 'active' and -300 <= age <= 72 * 3600)

    def decorate_job(self, job, category, decisions, rows, ledger, exported=None):
        key, decision = self.matching_decision(job, decisions)
        row = self.matched_row(job, rows)
        evidence = self.related_mail(job, ledger)
        ever_applied = bool((decision or {}).get('ever_applied') or (decision or {}).get('withdrawn') or category == 'applied' or (row and is_application(row, decision)) or any(not e.get('needs_review') and e.get('outcome') not in {'prepared', 'error', 'ambiguous_identity'} and e.get('kind') in {'applied', 'responded', 'interview', 'offer', 'rejected', 'hired', 'withdrawn'} for e in evidence))
        movable = category != 'applied' and not ever_applied
        job.update(movement_status=category if category in {'new', 'pending', 'excluded'} else None, movable=movable, decision_revision=self.decision_revision(job, decision, row, ledger), ever_applied=ever_applied, withdrawn=bool((decision or {}).get('withdrawn')))
        job['allowed_moves'] = [target for target in ('new', 'pending', 'excluded') if target != category and (target != 'new' or self.can_restore_recommendation(job, decision, exported))] if movable else []
        if category == 'excluded' and job['withdrawn']:
            job['canonical_status_label'] = '지원철회'
        if decision:
            job['note'] = clean(decision.get('note'), 1000)
            job['needs_review'] = bool(decision.get('needs_review'))
            job['classification_reason'] = clean(decision.get('reason') or decision.get('note'), 1000)
        if evidence:
            job['mail_count'] = len(evidence)
            last = evidence[-1]
            job['mail_source'] = SITES.get(last.get('source_name'), '채용 메일')
            job['mail_url'] = safe_url(last.get('mail_url'))
            job['mail_evidence'] = clean(last.get('evidence_text'), 1200)
            accepted = [e for e in evidence if e.get('kind') == 'applied' and e.get('applied') and not e.get('needs_review')]
            if job.get('canonical_status') == 'Applied' and accepted and (decision or {}).get('origin') != 'manual':
                reason = re.sub(r'\s+', '', accepted[-1].get('reason', ''))
                job['application_stage_label'] = '접수 확인' if '접수확인' in reason else '서류 전달' if '서류전달' in reason else '지원 의사 전달' if '지원의사전달' in reason else '지원 진행 확인'
        if not movable:
            job['movement_disabled_reason'] = '지원 이력이 있는 건은 상세 내역에서 진행 상태를 확인해 주세요.' if ever_applied else '종료된 공고는 모집 중인 새 공고를 확인해 주세요.'
        return job

    def job_context(self, ident, snapshot=None):
        dashboard, archive, (export, data, rows, jobs) = snapshot or self.snapshot()
        public = [j for group in dashboard['sites'] for j in group['jobs']] + dashboard['applications'] + archive['pending'] + archive['excluded']
        job = next((j for j in public if j['id'] == ident or ident in aliases(j)), None)
        if job is None:
            raw = next((j for j in jobs if j['id'] == ident or ident in aliases(j)), None)
            if raw:
                job = copy.deepcopy(raw)
            elif data['decisions'].get(ident):
                job = public_job(data['decisions'][ident].get('job') or {'id': ident})
                job['id'] = ident
            else:
                row = next((r for r in rows if f'tracker_{r["num"]}' == ident), None)
                if row:
                    job = self.tracker_job(row)
            if job:
                _, d = self.matching_decision(job, data['decisions'])
                row = self.matched_row(job, rows)
                category = (d or {}).get('status') or ('applied' if row and is_application(row) else 'excluded' if row and row['status'] in {'SKIP', 'Discarded'} else 'new')
                self.decorate_job(job, category, data['decisions'], rows, self.mail_ledger(), export['jobs'])
        if not job:
            raise APIError(404, 'job_missing', '해당 공고를 찾을 수 없습니다. 화면을 새로고침해 주세요.')
        key, decision = self.matching_decision(job, data['decisions'])
        return job, key or job['id'], decision, self.matched_row(job, rows), data

    def movement_data(self):
        result = read_json(self.c.data / 'dashboard-movements.json', {'version': 1, 'events': [], 'undos': {}})
        if result.get('version') != 1 or not isinstance(result.get('events'), list) or not isinstance(result.get('undos'), dict):
            raise APIError(503, 'movement_history_invalid', '분류 변경 이력을 확인해야 합니다.')
        return result

    @staticmethod
    def valid_note(value):
        if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise APIError(400, 'invalid_note', '메모는 1,000자 이내로 입력해 주세요.')
        return re.sub(r'[\r\n\t|]+', ' ', value).strip()

    def decide(self, body):
        if body.get('status') == 'applied':
            return self._decide_legacy(body)
        return self.move(body, strict=False)

    def move(self, body, strict=True):
        ident, status = body.get('id'), body.get('status')
        if not isinstance(ident, str) or not ident or len(ident) > 256 or status not in {'new', 'pending', 'excluded'}:
            raise APIError(400, 'invalid_move', '이동할 공고와 목적지를 확인해 주세요.')
        if strict and not isinstance(body.get('expected_updated_at'), str):
            raise APIError(400, 'revision_required', '현재 기록의 버전이 필요합니다. 새로고침해 주세요.')
        with self.lock():
            job, key, previous, row, data = self.job_context(ident)
            expected = body.get('expected_updated_at')
            allowed = {job['decision_revision']}
            if not strict:
                allowed.add(previous.get('updated_at', '') if previous else self.row_revision(row) if row else '')
            if expected is not None and expected not in allowed:
                raise APIError(409, 'decision_conflict', '다른 화면이나 메일에서 상태가 변경되었습니다. 최신 기록을 불러와 주세요.')
            if not job['movable']:
                code = 'closed_history_not_restorable' if row and row['status'] == 'Discarded' else 'application_exists'
                raise APIError(409, code, job.get('movement_disabled_reason', '지원 이력은 드래그로 변경할 수 없습니다.'))
            old_status = job['movement_status']
            if old_status == status:
                return {'ok': True, 'unchanged': True, 'dashboard': self.dashboard()}
            if status == 'new' and strict:
                # The latest export is authoritative; archived evidence may be stale or closed.
                if not self.can_restore_recommendation(job, previous):
                    raise APIError(409, 'posting_not_active', '현재 모집 중인 공고인지 확인되지 않아 추천으로 복원하지 않았습니다. 공고 원문을 확인해 주세요.')
            note = self.valid_note(body.get('note', (previous or {}).get('note', clean(job.get('note'), 1000))))
            movement = self.movement_data()
            event_id = secrets.token_hex(16)
            movement_event = {'id': event_id, 'job_id': key, 'job_aliases': sorted(aliases(job)), 'at': now(), 'kind': 'move', 'from': old_status, 'to': status, 'note': note, 'previous': copy.deepcopy(previous), 'source': 'manual', 'state': 'prepared'}
            movement['events'].append(movement_event)
            atomic_json(self.c.data / 'dashboard-movements.json', movement)
            try:
                result = self._decide_legacy({'id': ident, 'status': status, 'note': note})
            except APIError as error:
                movement_event['state'] = 'verification_required' if error.details.get('partial_commit') else 'failed'
                atomic_json(self.c.data / 'dashboard-movements.json', movement)
                raise
            # Keep the pre-write journal and its prior reason through any process interruption.
            changed_job, _, changed_decision, _, _ = self.job_context(ident)
            token = secrets.token_urlsafe(32)
            expires = time.time() + 10
            movement_event['state'] = 'committed'
            movement['undos'] = {k: v for k, v in movement['undos'].items() if v.get('expires', 0) > time.time()}
            movement['undos'][hashlib.sha256(token.encode()).hexdigest()] = {'job_id': key, 'from': old_status, 'previous': copy.deepcopy(previous), 'previous_note': clean(job.get('note'), 1000), 'previous_canonical_status': row['status'] if row else None, 'expected_revision': changed_job['decision_revision'], 'expires': expires, 'event_id': event_id}
            atomic_json(self.c.data / 'dashboard-movements.json', movement)
            result.update(dashboard=self.dashboard(), undo_token=token, undo_expires_at=dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat().replace('+00:00', 'Z'))
            return result

    def undo_move(self, body):
        token = body.get('token')
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise APIError(400, 'invalid_undo', '실행 취소 기록을 확인해 주세요.')
        with self.lock():
            data = self.movement_data()
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            undo = data['undos'].get(token_hash)
            if not undo or undo.get('expires', 0) <= time.time():
                raise APIError(409, 'undo_expired', '실행 취소 시간이 지났습니다. 현재 목록에서 다시 분류해 주세요.')
            job, key, previous, row, decisions = self.job_context(undo['job_id'])
            if job['decision_revision'] != undo['expected_revision']:
                raise APIError(409, 'undo_conflict', '이후 메일이나 다른 화면에서 기록이 변경되어 취소하지 않았습니다. 최신 기록을 확인해 주세요.')
            if not job['movable']:
                raise APIError(409, 'application_exists', '지원 이력이 변경되어 이전 분류로 되돌리지 않았습니다.')
            old = undo.get('previous') or {}
            previous_note = old.get('note', undo.get('previous_note', ''))
            undo_event = {'id': secrets.token_hex(16), 'job_id': key, 'job_aliases': sorted(aliases(job)), 'at': now(), 'kind': 'undo', 'from': job['movement_status'], 'to': undo['from'], 'note': previous_note, 'undoes': undo['event_id'], 'source': 'manual', 'state': 'prepared'}
            data['events'].append(undo_event)
            atomic_json(self.c.data / 'dashboard-movements.json', data)
            try:
                self._decide_legacy({'id': key, 'status': undo['from'], 'note': previous_note}, restore_metadata=old, restore_canonical_status=undo.get('previous_canonical_status'))
            except APIError as error:
                undo_event['state'] = 'verification_required' if error.details.get('partial_commit') else 'failed'
                atomic_json(self.c.data / 'dashboard-movements.json', data)
                raise
            data['undos'].pop(token_hash)
            undo_event['state'] = 'committed'
            atomic_json(self.c.data / 'dashboard-movements.json', data)
            return {'ok': True, 'dashboard': self.dashboard()}

    def update_note(self, ident, body):
        note = self.valid_note(body.get('note'))
        with self.lock():
            job, key, previous, row, data = self.job_context(ident)
            if body.get('expected_updated_at') != job['decision_revision']:
                raise APIError(409, 'decision_conflict', '메모를 작성하는 동안 기록이 변경되었습니다. 최신 내용을 확인해 주세요.')
            record = copy.deepcopy(previous) if previous else {'id': key, 'job': job, 'aliases': sorted(aliases(job)), 'status': job.get('movement_status') or 'applied', 'tracker_id': job.get('tracker_id'), 'origin': 'note', 'effective_at': ''}
            record.update(note=note, updated_at=now())
            # A note does not assert a new classification date or block later mail evidence.
            data['decisions'][key] = record
            data['updated_at'] = record['updated_at']
            atomic_json(self.c.decisions, data)
            movements = self.movement_data()
            movements['events'].append({'id': secrets.token_hex(16), 'job_id': key, 'job_aliases': sorted(aliases(job)), 'at': now(), 'kind': 'note', 'note': note, 'source': 'manual'})
            atomic_json(self.c.data / 'dashboard-movements.json', movements)
            return {'ok': True, 'dashboard': self.dashboard(), 'job': self.job_context(ident)[0]}

    def job_history(self, ident):
        job, key, decision, row, _ = self.job_context(ident)
        labels = {'proposal_unanswered': '제안 메일 미회신', 'pending': '지원보류', 'declined': '지원제외', 'withdrawn': '지원철회', 'applied': '지원 의사 / 서류 전달', 'responded': '기업 회신', 'interview': '면접', 'offer': '오퍼', 'rejected': '불합격', 'hired': '입사'}
        events = [{'id': e['event_id'], 'at': e['event_at'], 'kind': e['kind'], 'label': labels.get(e['kind'], e['kind']), 'note': clean(e.get('reason')), 'evidence': clean(e.get('evidence_text')), 'url': safe_url(e.get('mail_url')), 'source': SITES.get(e.get('source_name'), '채용 메일'), 'applied': e.get('applied', False), 'needs_review': bool(e.get('needs_review')), 'outcome': e.get('outcome', ''), 'identity_source': e.get('identity_source'), 'identity_evidence': clean(e.get('identity_evidence'), 2000), 'posting_url': safe_url(e.get('job_url'))} for e in self.related_mail(job)]
        aa = aliases(job)
        state_names = {'new': '추천 공고', 'pending': '지원보류', 'excluded': '지원제외'}
        for e in self.movement_data()['events']:
            if e.get('job_id') in aa or aa.intersection(e.get('job_aliases') or []):
                label = '메모 수정' if e['kind'] == 'note' else ('실행 취소: ' if e['kind'] == 'undo' else '') + state_names.get(e.get('from'), '') + ' → ' + state_names.get(e.get('to'), '')
                if e.get('state') == 'failed':
                    label += ' (저장되지 않음)'
                elif e.get('state') in {'prepared', 'verification_required'}:
                    label += ' (저장 결과 확인 필요)'
                events.append({**{k: v for k, v in e.items() if k not in {'previous', 'job_aliases'}}, 'label': label, 'source': '대시보드', 'url': '', 'evidence': ''})
        if row:
            events.append({'id': 'canonical_' + str(row['num']), 'at': row.get('date', ''), 'kind': 'canonical', 'label': STATUSES_KO.get(row['status'], row['status']), 'note': clean(row.get('notes')), 'evidence': '', 'url': '', 'source': '지원 이력'})
        return {'job': job, 'events': sorted(events, key=lambda e: (e.get('at', ''), e['id']), reverse=True)}

    def mail_sync_lock_held(self):
        try:
            stream = (self.c.data / '.mail-sync.lock').open('rb')
        except FileNotFoundError:
            return False
        except OSError:
            return None
        with stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return None
            fcntl.flock(stream, fcntl.LOCK_UN)
            return False

    @staticmethod
    def mail_error_code(state):
        code = state.get('error_code')
        if code in MAIL_ERROR_MESSAGES:
            return code
        text = clean(state.get('error_diagnostic') or state.get('error'), 4000).casefold()
        if 'verified connector' in text or 'connector response' in text or 'gmail response validation failed' in text:
            return 'connector_response_invalid'
        if any(value in text for value in ('unauthorized', 'token_expired', 'invalid_grant', 'authentication', '401')):
            return 'mail_auth_required'
        if 'timeout' in text or 'timed out' in text:
            return 'mail_provider_timeout'
        if 'pagination' in text or 'page token' in text:
            return 'mail_pagination_invalid'
        if 'ingestion' in text:
            return 'mail_ingestion_failed'
        if 'coverage' in text or 'did not cover' in text:
            return 'mail_coverage_incomplete'
        if 'body' in text or 'mime' in text:
            return 'mail_body_unreadable'
        return 'mail_sync_failed'

    def mail_sync_run_summaries(self):
        path = self.c.data / 'mail-sync-history.jsonl'
        try:
            stat = path.stat()
        except OSError:
            return {'last_run': None, 'last_full_run': None, 'last_resumed_run': None}
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if self._mail_history_cache and self._mail_history_cache[0] == stamp:
            return self._mail_history_cache[1]
        output = {'last_run': None, 'last_full_run': None, 'last_resumed_run': None}
        try:
            with path.open() as stream:
                for line in stream:
                    try: record = json.loads(line)
                    except ValueError: continue  # An interrupted append cannot hide older valid entries.
                    if not isinstance(record, dict) or record.get('status') != 'complete': continue
                    duration = record.get('duration_seconds')
                    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0 <= duration < float('inf'): continue
                    value = {key: record.get(key) for key in ('run_id', 'attempt_id', 'started_at', 'completed_at', 'duration_seconds', 'resumed', 'run_mode', 'total_threads', 'processed_threads', 'initial_processed_threads', 'newly_processed_threads', 'provider', 'sync_mode')}
                    output['last_run'] = value
                    if record.get('resumed') is False and record.get('run_mode') == 'fresh':
                        output['last_full_run'] = value
                    elif record.get('resumed') is True:
                        output['last_resumed_run'] = value
        except OSError:
            return output
        self._mail_history_cache = (stamp, output)
        return output

    @staticmethod
    def mail_sync_elapsed(state, active):
        def stamp(value):
            try: return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except (AttributeError, TypeError, ValueError): return None
        def seconds(value):
            return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value < float('inf') else None
        started, phase_started = stamp(state.get('started_at')), stamp(state.get('phase_started_at'))
        elapsed, phase_elapsed = seconds(state.get('elapsed_seconds')), seconds(state.get('phase_elapsed_seconds'))
        duration = seconds(state.get('duration_seconds'))
        anchor = stamp(state.get('timing_updated_at'))
        end = time.time() if active else stamp(state.get('completed_at')) or stamp(state.get('updated_at'))
        if active and anchor is not None:
            extra = max(0, end - anchor)
            elapsed = elapsed + extra if elapsed is not None else None
            phase_elapsed = phase_elapsed + extra if phase_elapsed is not None else None
        if elapsed is None and end is not None and started is not None:
            elapsed = max(0, end - started)
        if phase_elapsed is None and end is not None and phase_started is not None:
            phase_elapsed = max(0, end - phase_started)
        if not active and duration is None and state.get('completed_at') and elapsed is not None:
            duration = elapsed
        return {'elapsed_seconds': round(elapsed, 3) if elapsed is not None else None, 'phase_elapsed_seconds': round(phase_elapsed, 3) if phase_elapsed is not None else None, 'duration_seconds': duration if not active else None}

    def mail_sync_status(self):
        state = read_json(self.c.data / 'mail-sync-state.json', {})
        defaults = {'status': 'idle', 'last_success_at': None, 'started_at': None, 'completed_at': None, 'processed_threads': 0, 'applied_count': 0, 'review_count': 0, 'phase': '대기', 'phase_detail': 'starting', 'phase_label': MAIL_PHASE_LABELS['starting'], 'total_threads': None, 'window_start': None, 'window_end': None, 'remaining_messages': None, 'remaining_known_threads': None, 'remaining_windows': None, 'updated_at': None, 'error': None, 'error_code': None, 'error_message': None}
        defaults.update(elapsed_seconds=None, duration_seconds=None, phase_elapsed_seconds=None, phase_started_at=None, phase_durations={}, resumed=None, run_mode=None, initial_processed_threads=None, initial_total_threads=None, initial_remaining_threads=None, initial_remaining_messages=None, initial_remaining_known_threads=None, initial_remaining_windows=None, newly_processed_threads=None, attempt_id=None, timing_updated_at=None, provider_timing_summary=None)
        defaults.update(provider=None, sync_mode=None, history_pages=None, ignored_changes=None, changed_threads=None, cache_reused_threads=None)
        allowed = set(defaults) | {'progress', 'run_id'}
        defaults.update({k: v for k, v in state.items() if k in allowed})
        # Older failed runs have counts in their checkpoint but no public window
        # metadata. Derive it read-only instead of displaying a false zero total.
        cp = read_json(self.c.data / 'mail-sync-checkpoint.json', {})
        if cp.get('version') == 1 and cp.get('run_id') == state.get('run_id') and any(key not in state for key in ('window_start', 'window_end', 'phase_detail', 'remaining_known_threads')):
            wanted, read_messages = set(cp.get('message_ids', [])), set(cp.get('read_message_ids', []))
            known, read_threads = set(cp.get('known_thread_ids', [])), set(cp.get('read_thread_ids', []))
            remaining_messages, remaining_known = len(wanted - read_messages), len(known - read_threads)
            remaining_windows = sum(not window.get('done') for window in cp.get('windows', []))
            detail = cp.get('phase_detail') or ('read_known_threads' if cp.get('phase') == 'read' and not remaining_messages else 'read_recent_threads' if cp.get('phase') == 'read' else 'search_messages' if cp.get('phase') == 'search' else 'validate' if cp.get('phase') == 'validated' else cp.get('phase'))
            meta = {'window_start': cp.get('window_start'), 'window_end': cp.get('window_end'), 'phase': cp.get('phase'), 'phase_detail': detail, 'phase_label': MAIL_PHASE_LABELS.get(detail, MAIL_PHASE_LABELS['starting']), 'processed_threads': len(read_threads), 'total_threads': len(known | read_threads) if not remaining_windows and not remaining_messages else None, 'remaining_messages': remaining_messages, 'remaining_known_threads': remaining_known, 'remaining_windows': remaining_windows}
            defaults.update(meta)
            defaults['progress'] = {**(defaults.get('progress') or {}), **{key: value for key, value in meta.items() if key in {'processed_threads', 'total_threads', 'remaining_messages', 'remaining_known_threads', 'remaining_windows'}}, 'read_target_messages': len(wanted & read_messages), 'known_threads': len(known)}
        defaults['last_run_review_count'] = defaults['review_count']
        defaults['review_count'] = sum(bool(decision.get('needs_review')) for decision in self.decision_data()['decisions'].values())
        held = self.mail_sync_lock_held()
        local_alive = self._mail_process is not None and self._mail_process.poll() is None
        starting = local_alive and self._mail_launch_at is not None and time.monotonic() - self._mail_launch_at < 10
        defaults['is_running'] = held is True
        if held is True:
            defaults['status'] = 'running'
            if state.get('status') not in {'running', 'queued'}:
                defaults.update(phase_detail='starting', phase_label=MAIL_PHASE_LABELS['starting'])
        elif held is None:
            defaults.update(status='failed', error_code='sync_state_unavailable')
        elif starting:
            defaults.update(status='queued', phase_detail='starting', phase_label=MAIL_PHASE_LABELS['starting'])
        elif defaults['status'] in {'running', 'queued'} or local_alive:
            defaults.update(status='failed', error_code='sync_interrupted')
        if defaults['status'] == 'failed':
            defaults['error_code'] = defaults['error_code'] if defaults['error_code'] in MAIL_ERROR_MESSAGES else self.mail_error_code(state)
            defaults['error_message'] = MAIL_ERROR_MESSAGES.get(defaults['error_code'], MAIL_ERROR_MESSAGES['mail_sync_failed'])
            defaults['error'] = defaults['error_message']
        else:
            defaults.update(error=None, error_code=None, error_message=None)
        # Only the state owned by the active worker advances; a previous result
        # briefly visible while a new worker acquires its lock is not its timer.
        active_state = held is True and state.get('status') in {'running', 'queued'}
        if defaults['status'] in {'running', 'queued'} and not active_state:
            defaults.update(elapsed_seconds=None, duration_seconds=None, phase_elapsed_seconds=None)
        else:
            defaults.update(self.mail_sync_elapsed(state, active_state))
        defaults.update(self.mail_sync_run_summaries())
        connection = self.mail_connection()
        defaults['mail_connection'] = connection
        if defaults['provider'] not in {'gmail', 'codex'}: defaults['provider'] = connection['provider']
        if defaults['sync_mode'] not in {'bootstrap', 'incremental', 'rescan'}: defaults['sync_mode'] = None
        for name in ('history_pages', 'ignored_changes', 'changed_threads', 'cache_reused_threads'):
            if not isinstance(defaults[name], int) or isinstance(defaults[name], bool) or defaults[name] < 0: defaults[name] = None
        defaults['enabled'] = bool(getattr(self.c, 'mail_sync_command', ''))
        return defaults

    def start_mail_sync(self):
        with self.mutex:
            if self._mail_process is not None and self._mail_process.poll() is None:
                return dict(self.mail_sync_status(), accepted=False)
            # The worker/cron owns this same lock. A manual request joins its status
            # instead of spawning another scan while an existing run is active.
            self.c.data.mkdir(parents=True, exist_ok=True)
            with open(self.c.data / '.mail-sync.lock', 'a') as running_lock:
                try:
                    fcntl.flock(running_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return dict(self.mail_sync_status(), accepted=False, status='running')
                finally:
                    fcntl.flock(running_lock, fcntl.LOCK_UN)
            try:
                command = json.loads(getattr(self.c, 'mail_sync_command', '') or 'null')
            except ValueError:
                command = None
            if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in command):
                raise APIError(503, 'mail_sync_unconfigured', '메일 동기화 실행 경로가 아직 연결되지 않았습니다.')
            try:
                self._mail_process = subprocess.Popen(command, cwd=self.c.root, env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                self._mail_launch_at = time.monotonic()
            except OSError:
                raise APIError(503, 'mail_sync_unavailable', '메일 동기화를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.')
            return dict(self.mail_sync_status(), accepted=True)

    def lifecycle(self, ident, body):
        status = body.get('status')
        if status not in CANONICAL or status in {'Evaluated', 'SKIP'}:
            raise APIError(400, 'invalid_status', '지원 전형에 해당하는 상태를 선택해 주세요.')
        note = body.get('note', '')
        if not isinstance(note, str) or len(note) > 1000:
            raise APIError(400, 'invalid_note', '메모는 1,000자 이내로 입력해 주세요.')
        note = re.sub(r'[\r\n\t|]+', ' ', note).strip()
        with self.lock():
            row = next((r for r in self.tracker_rows() if str(r['num']) == ident), None)
            if not row:
                raise APIError(404, 'application_missing', '지원 이력을 찾을 수 없습니다.')
            if body.get('expected_updated_at') not in (None, self.row_revision(row)):
                raise APIError(409, 'application_conflict', '다른 작업에서 지원 상태가 변경되었습니다. 새로고침해 주세요.')
            try:
                self.set_canonical(row, status, '사용자 대시보드 전형 상태 변경' + ('; ' + note if note else ''))
                return {'ok': True, 'dashboard': self.dashboard()}
            except APIError as exc:
                exc.details['partial_commit'] = True
                raise


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return 'scrypt$16384$8$1$' + base64.urlsafe_b64encode(salt).decode() + '$' + base64.urlsafe_b64encode(digest).decode()


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split('$')
        if (algorithm, n, r, p) != ('scrypt', '16384', '8', '1'):
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


class InlineScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.current = None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script':
            self.current = [] if not any(k.lower() == 'src' for k, _ in attrs) else None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self.current is not None:
            self.scripts.append(''.join(self.current).replace('\r\n', '\n').replace('\r', '\n'))
            self.current = None


def inline_script_hashes(html):
    parser = InlineScriptParser()
    parser.feed(html.decode('utf-8'))
    return sorted({"'sha256-" + base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode() + "'" for script in parser.scripts})


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, config, store=None):
        self.config = config
        self.store = store or Store(config)
        self.sessions = {}
        self.auth_lock = threading.Lock()
        self.attempts = {}
        self.oauth_flows = {}
        self.oauth_lock = threading.Lock()
        self.dev_csrf = secrets.token_urlsafe(32)
        super().__init__((config.host, config.port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CareerDashboard'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log paths, query strings, request data or login tokens.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def send(self, status, data, cookie=None, content_type='application/json; charset=utf-8', script_hashes=(), extra_headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        for name, value in {
            'Content-Type': content_type, 'Content-Length': str(len(payload)),
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'" + (' ' + ' '.join(script_hashes) if script_hashes else '') + "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            'X-Robots-Tag': 'noindex, nofollow, noarchive',
        }.items():
            self.send_header(name, value)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        for name, value in (extra_headers or {}).items(): self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def json_body(self):
        if self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            raise APIError(400, 'invalid_body', '지원하지 않는 요청 형식입니다.')
        if self.headers.get_content_type() != 'application/json':
            raise APIError(415, 'json_required', 'JSON 요청이 필요합니다.')
        try:
            size = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            size = -1
        if size < 0 or size > MAX_BODY:
            self.close_connection = True
            raise APIError(413, 'body_too_large', '요청 크기를 확인해 주세요.')
        try:
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, 'invalid_json', '요청 내용을 읽을 수 없습니다.')

    def session(self):
        c = self.server.config
        if c.dev and self.client_address[0] in {'127.0.0.1', '::1'} and urlsplit(c.origin).hostname in {'localhost', '127.0.0.1', '::1'} and not self.headers.get('CF-Connecting-IP') and not self.headers.get('X-Forwarded-For'):
            return {'csrf': self.server.dev_csrf, 'expires': time.time() + 3600}
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie.get('career_session')
            token = token.value if token else ''
        except http.cookies.CookieError:
            token = ''
        with self.server.auth_lock:
            session = self.server.sessions.get(token)
            if session and session['expires'] > time.time():
                return session
            self.server.sessions.pop(token, None)
        return None

    def require_auth(self):
        session = self.session()
        if not session:
            raise APIError(401, 'authentication_required', '개인 대시보드에 로그인해 주세요.')
        return session

    def require_origin(self):
        expected = self.server.config.origin
        if self.headers.get('Origin') != expected:
            raise APIError(403, 'origin_rejected', '접속 주소가 일치하지 않습니다. 대시보드 주소에서 다시 시도해 주세요.')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise APIError(403, 'cross_site_rejected', '다른 사이트에서 보낸 요청을 처리하지 않았습니다.')

    def require_csrf(self):
        session = self.require_auth()
        self.require_origin()
        value = self.headers.get('X-CSRF-Token', '')
        if not value or not hmac.compare_digest(value, session['csrf']):
            raise APIError(403, 'csrf_rejected', '화면 인증 정보가 만료되었습니다. 새로고침해 주세요.')

    def cookie(self, token, max_age):
        secure = '; Secure' if self.server.config.origin.startswith('https://') else ''
        return f'career_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}'

    def login(self, body):
        password = body.get('password')
        if not isinstance(password, str) or len(password) > 512:
            raise APIError(400, 'invalid_password', '비밀번호를 확인해 주세요.')
        current = time.monotonic()
        # Proxy origin is localhost. A global cap intentionally also bounds distributed guesses.
        with self.server.auth_lock:
            history = [v for v in self.server.attempts.get('global', []) if current - v < 300]
            if len(history) >= 20:
                raise APIError(429, 'login_rate_limited', '로그인 시도 횟수가 많습니다. 5분 후 다시 시도해 주세요.')
            history.append(current)
            self.server.attempts['global'] = history
        if not verify_password(password, self.server.config.password_hash):
            raise APIError(401, 'invalid_credentials', '비밀번호가 일치하지 않습니다.')
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.server.auth_lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > time.time()}
            self.server.sessions[token] = {'csrf': csrf, 'expires': time.time() + self.server.config.session_seconds}
        self.send(200, {'authenticated': True, 'csrf_token': csrf}, self.cookie(token, self.server.config.session_seconds))

    def oauth_cookie(self, nonce, max_age):
        secure = '; Secure' if self.server.config.origin.startswith('https://') else ''
        return f'career_gmail_oauth={nonce}; HttpOnly; SameSite=Lax; Path=/api/mail-connection/callback; Max-Age={max_age}{secure}'

    def start_gmail_connection(self):
        session = self.require_auth()
        try:
            flow = gmail_auth.prepare_authorization(self.server.config.gmail_credentials, self.server.store.gmail_callback_uri())
        except gmail_auth.CredentialError as error:
            raise APIError(409, error.reason, 'Google 인증 설정 또는 등록된 반환 주소를 확인해 주세요.') from None
        nonce = secrets.token_urlsafe(32)
        with self.server.oauth_lock:
            self.server.oauth_flows = {key: row for key, row in self.server.oauth_flows.items()
                                       if row['expires_at'] > time.time() and row['session'] is not session}
            if len(self.server.oauth_flows) >= 32:
                raise APIError(429, 'authorization_busy', '잠시 후 계정 연결을 다시 시도해 주세요.')
            self.server.oauth_flows[flow['state']] = {**flow, 'nonce_hash': hashlib.sha256(nonce.encode()).hexdigest(),
                                                     'session': session}
        return self.send(200, {'authorization_url': flow['authorization_url']}, cookie=self.oauth_cookie(nonce, gmail_auth.FLOW_SECONDS))

    def gmail_callback(self, query):
        # The dashboard cookie stays SameSite=Strict. A short-lived Lax cookie
        # binds Google's top-level callback to the authenticated initiating browser.
        result = 'authorization_invalid'
        try:
            params = parse_qs(query) if len(query) <= 20000 else {}
            state = params.get('state', [])
            state = state[0] if len(state) == 1 and len(state[0]) <= 200 else ''
            cookie = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            nonce = cookie.get('career_gmail_oauth')
            nonce = nonce.value if nonce else ''
            with self.server.oauth_lock:
                flow = self.server.oauth_flows.get(state)
                valid = flow and flow['expires_at'] > time.time() and nonce and hmac.compare_digest(flow['nonce_hash'], hashlib.sha256(nonce.encode()).hexdigest())
                if valid:
                    # Consume before network I/O, including consent denial/errors.
                    self.server.oauth_flows.pop(state, None)
            if valid:
                with self.server.auth_lock:
                    live = any(session is flow['session'] and session['expires'] > time.time() for session in self.server.sessions.values())
                if self.server.config.dev and flow['session'].get('csrf') == self.server.dev_csrf:
                    live = bool(self.session())
                if not live: raise gmail_auth.CredentialError(reason='authorization_expired')
                if params.get('error'): raise gmail_auth.CredentialError(reason='authorization_cancelled')
                codes = params.get('code', [])
                if len(codes) != 1: raise gmail_auth.CredentialError(reason='authorization_invalid')
                gmail_auth.complete_authorization(self.server.config.gmail_credentials, codes[0], flow)
                result = 'connected'
        except gmail_auth.CredentialError as error:
            result = error.reason if error.reason in {'account_mismatch_requires_relink', 'authorization_expired', 'authorization_cancelled', 'configuration_changed', 'scope_not_readonly'} else 'authorization_failed'
        except (http.cookies.CookieError, ValueError):
            pass
        # Never reflect provider error text, code, state or arbitrary return URLs.
        return self.send(303, b'', cookie=self.oauth_cookie('', 0), content_type='text/plain; charset=utf-8',
                         extra_headers={'Location': '/?mail_connection=' + result})

    def handle_request(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            method = self.command
            if method in ('GET', 'HEAD') and path == '/healthz':
                return self.send(200, {'ok': True})
            if method == 'GET' and path == '/api/session':
                session = self.session()
                return self.send(200, {'authenticated': bool(session), **({'csrf_token': session['csrf']} if session else {})})
            if method == 'POST' and path == '/api/login':
                self.require_origin()
                return self.login(self.json_body())
            if method == 'POST' and path == '/api/logout':
                self.require_csrf()
                if self.headers.get('Content-Length', '0') != '0' or self.headers.get('Transfer-Encoding'):
                    self.json_body()
                cookie = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
                token = cookie.get('career_session')
                if token:
                    with self.server.auth_lock:
                        self.server.sessions.pop(token.value, None)
                return self.send(200, {'ok': True}, self.cookie('', 0))
            if method == 'GET' and path == '/api/mail-connection/callback':
                return self.gmail_callback(parsed.query)
            if path.startswith('/api/'):
                self.require_auth()
                if method == 'GET' and path == '/api/dashboard':
                    return self.send(200, self.server.store.dashboard())
                if method == 'GET' and path == '/api/decisions':
                    params = parse_qs(parsed.query)
                    status = params.get('status', [''])[0]
                    site = params.get('site', ['all'])[0]
                    try:
                        offset, limit = int(params.get('offset', ['5'])[0]), int(params.get('limit', ['20'])[0])
                    except ValueError:
                        raise APIError(400, 'invalid_pagination', '목록 페이지 값을 확인해 주세요.')
                    if status not in {'pending', 'excluded'} or offset < 0 or not 1 <= limit <= 100 or len(site) > 253 or any(ord(c) < 32 for c in site):
                        raise APIError(400, 'invalid_pagination', '목록 조건을 확인해 주세요.')
                    return self.send(200, self.server.store.archive(status, offset, limit, site))
                if method == 'POST' and path == '/api/decisions':
                    self.require_csrf()
                    return self.send(200, self.server.store.decide(self.json_body()))
                if method == 'POST' and path == '/api/moves':
                    self.require_csrf()
                    return self.send(200, self.server.store.move(self.json_body()))
                if method == 'POST' and path == '/api/moves/undo':
                    self.require_csrf()
                    return self.send(200, self.server.store.undo_move(self.json_body()))
                if path == '/api/mail-sync' and method == 'GET':
                    return self.send(200, self.server.store.mail_sync_status())
                if path == '/api/mail-sync' and method == 'POST':
                    self.require_csrf()
                    self.json_body()
                    return self.send(202, self.server.store.start_mail_sync())
                if method == 'GET' and path == '/api/mail-connection':
                    return self.send(200, self.server.store.mail_connection())
                if method == 'POST' and path == '/api/mail-connection/start':
                    self.require_csrf(); self.json_body()
                    return self.start_gmail_connection()
                job_match = re.fullmatch(r'/api/jobs/([^/]+)/(history|note)', path)
                if job_match:
                    ident = unquote(job_match[1])
                    if len(ident) > 256:
                        raise APIError(400, 'invalid_job', '공고 정보를 확인해 주세요.')
                    if method == 'GET' and job_match[2] == 'history':
                        return self.send(200, self.server.store.job_history(ident))
                    if method == 'PATCH' and job_match[2] == 'note':
                        self.require_csrf()
                        return self.send(200, self.server.store.update_note(ident, self.json_body()))
                match = re.fullmatch(r'/api/applications/(\d+)', path)
                if method == 'PATCH' and match:
                    self.require_csrf()
                    return self.send(200, self.server.store.lifecycle(match[1], self.json_body()))
                raise APIError(404, 'route_missing', '요청한 기능을 찾을 수 없습니다.')
            if method not in {'GET', 'HEAD'}:
                raise APIError(405, 'method_not_allowed', '지원하지 않는 요청입니다.')
            if path == '/robots.txt':
                return self.send(200, b'User-agent: *\nDisallow: /\n', content_type='text/plain')
            decoded = unquote(path)
            if '\x00' in decoded or '\\' in decoded or any(part.startswith('.') for part in decoded.split('/') if part):
                raise APIError(404, 'file_missing', '파일을 찾을 수 없습니다.')
            static_root = self.server.config.static.resolve()
            target = (static_root / decoded.lstrip('/')).resolve()
            if not target.is_relative_to(static_root):
                raise APIError(404, 'file_missing', '파일을 찾을 수 없습니다.')
            if not target.is_file():
                if Path(decoded).suffix:
                    raise APIError(404, 'file_missing', '파일을 찾을 수 없습니다.')
                target = (static_root / 'index.html').resolve()
            if not target.is_relative_to(static_root) or not target.exists() or target.suffix in {'.map', '.py', '.json', '.env'}:
                raise APIError(404, 'file_missing', '파일을 찾을 수 없습니다.')
            payload = target.read_bytes()
            return self.send(200, payload, content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream', script_hashes=inline_script_hashes(payload) if target.suffix == '.html' else ())
        except APIError as error:
            if self.command not in {'GET', 'HEAD'}:
                self.close_connection = True
            self.send(error.status, {'error': error.message, 'code': error.code, **error.details})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            logging.exception('Dashboard request failed (request details omitted)')
            self.send(500, {'code': 'internal_error', 'error': '요청 처리 중 오류가 발생했습니다. 새로고침 후 현재 상태를 확인해 주세요.'})

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PATCH = handle_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hash-password', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.hash_password:
        import getpass
        print(hash_password(getpass.getpass('Dashboard password: ')))
        return
    config = Config()
    if config.host not in {'127.0.0.1', '::1', 'localhost'}:
        raise SystemExit('Bind only to loopback behind the HTTPS proxy.')
    if config.dev and urlsplit(config.origin).hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise SystemExit('Development bypass requires an explicit localhost origin.')
    if not config.dev and not config.password_hash.startswith('scrypt$16384$8$1$'):
        raise SystemExit('DASHBOARD_PASSWORD_HASH is required; startup refused.')
    if args.check:
        dashboard = Store(config).dashboard()
        print(json.dumps({'ok': True, 'sites': len(dashboard['sites']), 'stats': dashboard['stats'], 'source_run_at': dashboard['source_run_at'], 'stale': dashboard['stale']}))
        return
    server = AppServer(config)
    logging.info('Private dashboard listening on loopback port %s', config.port)
    server.serve_forever()


if __name__ == '__main__':
    main()
