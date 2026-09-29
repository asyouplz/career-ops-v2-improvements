"""All mutation tests use temporary synthetic data; never a production tracker."""
import copy
import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import server


def candidate(n, source='wanted', **extra):
    return dict(id=f'li_{n}', aliases=[f'li_{n}', f'li_{n}'], company=f'회사{n}', title='회계 팀장', source=source, source_id=source, site='원티드', url=f'https://www.wanted.co.kr/wd/{n}?utm_source=test', score=20 - n, verified_at=server.now(), liveness='active', dashboard_eligible=True, **extra)


def row(n, status='Evaluated', **extra):
    return dict(num=n, company=f'회사{n}', role='회계 팀장', status=status, notes='', date='2026-09-01', score='N/A', **extra)


class FakeStore(server.Store):
    def __init__(self, config, rows=None):
        super().__init__(config)
        self.rows = rows or []
        self.calls = []

    def tracker_rows(self):
        return copy.deepcopy(self.rows)

    def set_canonical(self, old, status, note, event_date=None):
        self.calls.append(('set', old['num'], status))
        for r in self.rows:
            if r['num'] == old['num']:
                r.update(status=status, notes=note)
                return copy.deepcopy(r)

    def add_applied(self, job, note, rows):
        n = max([r['num'] for r in self.rows] + [0]) + 1
        new = row(n, 'Applied')
        new.update(company=job['company'], role=job['title'], notes='Applied 2026-09-07; ' + job['url'])
        self.rows.append(new)
        self.calls.append(('add', n))
        return copy.deepcopy(new)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = server.Config()
        self.c.data = Path(self.temp.name)
        self.c.root = self.c.data
        self.c.export = self.c.data / 'dashboard-candidates.json'
        self.c.decisions = self.c.data / 'dashboard-decisions.json'
        self.c.tracker = self.c.data / 'applications.md'
        self.c.gmail_credentials = self.c.data / 'missing-test-credentials.json'
        self.c.static = self.c.data / 'dist'
        self.c.static.mkdir()
        self.c.static.joinpath('index.html').write_text('<html>dashboard</html>')
        self.store = FakeStore(self.c)
        self.export([candidate(i) for i in range(1, 9)])

    def export(self, jobs, **extra):
        server.atomic_json(self.c.export, dict(version=1, generated_at=server.now(), source_run_at=server.now(), jobs=jobs, sources=[{'source_id': 'wanted', 'source_label': '원티드'}], **extra))

    def ids(self):
        return [j['id'] for s in self.store.dashboard()['sites'] for j in s['jobs']]

    def decide(self, ident='li_1', status='pending', **extra):
        return self.store.decide(dict(id=ident, status=status, **extra))

    def test_top_five_keeps_reviewed_slot_until_refill(self):
        self.assertEqual(self.ids(), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.decide()
        self.assertEqual(self.ids(), ['li_1', 'li_2', 'li_3', 'li_4', 'li_5'])
        self.assertEqual(self.store.dashboard()['stats']['recommended'], 4)
        self.assertEqual(self.store.dashboard()['pending']['total'], 1)

    def test_separate_top_five_per_site(self):
        self.export([candidate(i, 'wanted' if i < 8 else 'saramin') for i in range(1, 15)])
        groups = {s['id']: s for s in self.store.dashboard()['sites']}
        self.assertEqual(len(groups['wanted']['jobs']), 5)
        self.assertEqual(len(groups['saramin']['jobs']), 5)
        self.assertEqual(groups['wanted']['name'], '원티드')

    def test_pending_and_excluded_keep_five_and_paginate(self):
        for i in range(1, 9):
            self.decide(f'li_{i}', 'excluded' if i % 2 else 'pending')
        self.assertEqual(self.store.dashboard()['pending']['total'], 4)
        self.assertEqual(self.store.dashboard()['excluded']['total'], 4)
        self.export([candidate(i) for i in range(1, 16)])
        for i in range(9, 16):
            self.decide(f'li_{i}', 'pending')
        self.assertEqual(len(self.store.dashboard()['pending']['items']), 5)
        page = self.store.archive('pending', 5, 3)
        self.assertEqual(page['total'], 11)
        self.assertEqual(len(page['items']), 3)
        self.assertTrue(page['has_more'])

    def test_archive_site_filter_applies_to_full_history_before_pagination(self):
        jobs = [candidate(i, 'saramin' if i <= 7 else 'wanted') for i in range(1, 16)]
        self.export(jobs)
        for i in range(1, 16):
            self.decide(f'li_{i}', 'excluded' if i == 15 else 'pending')
        data = self.store.dashboard()
        self.assertTrue(all(job['site'] == 'wanted' for job in data['pending']['items']))
        self.assertEqual({s['id']: s['total'] for s in data['pending']['sources']}, {'saramin': 7, 'wanted': 7})
        self.assertEqual(data['excluded']['sources'], [{'id': 'wanted', 'name': '원티드', 'total': 1}])
        first = self.store.archive('pending', 0, 5, 'saramin')
        self.assertEqual(first['total'], 7)
        self.assertEqual([job['id'] for job in first['items']], ['li_7', 'li_6', 'li_5', 'li_4', 'li_3'])
        self.assertTrue(first['has_more'])
        older = self.store.archive('pending', 5, 20, 'saramin')
        self.assertEqual([job['id'] for job in older['items']], ['li_2', 'li_1'])
        self.assertFalse(older['has_more'])
        self.assertEqual(older['sources'], first['sources'])
        self.assertEqual(self.store.archive('excluded', 0, 5, 'saramin')['items'], [])

    def test_legacy_source_uses_only_linked_report_header_and_preserves_records(self):
        reports = self.c.root / 'reports'
        reports.mkdir()
        report = reports / 'legacy.md'
        report.write_text('# 평가\n\n**URL:** https://www.wanted.co.kr/wd/700\n\n---\n## 근거\nhttps://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=999\n')
        legacy = row(70, 'Discarded', report='[원문](reports/legacy.md)')
        legacy['company'] = '원티드'
        self.store.rows = [legacy, row(71, 'SKIP')]
        before_rows = copy.deepcopy(self.store.rows)
        before_report = report.read_bytes()
        items = {job['tracker_id']: job for job in self.store.dashboard()['excluded']['items']}
        self.assertEqual((items[70]['site'], items[70]['site_name']), ('wanted', '원티드'))
        self.assertEqual(items[70]['url'], 'https://www.wanted.co.kr/wd/700')
        self.assertEqual((items[71]['site'], items[71]['site_name']), ('unknown', '출처 확인 필요'))
        self.assertEqual(self.store.archive('excluded', 0, 5, 'unknown')['total'], 1)
        self.assertEqual(self.store.rows, before_rows)
        self.assertEqual(report.read_bytes(), before_report)
        self.assertFalse(self.c.decisions.exists())

    def test_missing_or_unrelated_report_does_not_invent_source(self):
        reports = self.c.root / 'reports'
        reports.mkdir()
        (reports / 'body-only.md').write_text('# 평가\n\n---\n## 근거\n**URL:** https://www.wanted.co.kr/wd/1\n')
        (self.c.root / 'outside.md').write_text('**URL:** https://www.wanted.co.kr/wd/2\n')
        (reports / 'escape.md').symlink_to(self.c.root / 'outside.md')
        for link in ('reports/missing.md', 'reports/body-only.md', 'outside.md', 'reports/escape.md', 'https://example.com/report.md'):
            with self.subTest(link=link):
                legacy = row(70, 'SKIP', report=f'[report]({link})')
                legacy['company'] = '원티드'
                job = self.store.tracker_job(legacy)
                self.assertEqual(job['site'], 'unknown')
                self.assertEqual(job['url'], '')

    def test_report_header_cache_refreshes_and_conflicting_links_stay_unknown(self):
        reports = self.c.root / 'reports'
        reports.mkdir()
        one, two = reports / 'one.md', reports / 'two.md'
        one.write_text('**URL:** https://www.wanted.co.kr/wd/1\n')
        two.write_text('**URL:** https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=2\n')
        legacy = row(70, 'SKIP', report='[one](reports/one.md)')
        self.assertEqual(self.store.tracker_job(legacy)['site'], 'wanted')
        one.write_text('**URL:** https://www.jobkorea.co.kr/Recruit/GI_Read/333\n')
        self.assertEqual(self.store.tracker_job(legacy)['site'], 'jobkorea')
        legacy['report'] += ' [two](reports/two.md)'
        self.assertEqual(self.store.tracker_job(legacy)['site'], 'unknown')

    def test_saved_archive_source_recovers_from_own_url(self):
        saved = candidate(99)
        for key in ('source', 'source_id', 'site'):
            saved.pop(key)
        server.atomic_json(self.c.decisions, {'version': 1, 'decisions': {'li_99': {'status': 'pending', 'job': saved, 'updated_at': server.now()}}})
        saved_bytes = self.c.decisions.read_bytes()
        pending = self.store.dashboard()['pending']
        self.assertEqual(pending['items'][0]['site_name'], '원티드')
        self.assertEqual(pending['sources'], [{'id': 'wanted', 'name': '원티드', 'total': 1}])
        self.assertEqual(self.c.decisions.read_bytes(), saved_bytes)

    def test_source_host_matching_does_not_accept_suffix_lookalikes(self):
        job = server.public_job({'id': 'external', 'url': 'https://wanted.co.kr.example.com/job/1'})
        self.assertEqual(job['site'], 'wanted.co.kr.example.com')
        self.assertNotEqual(job['site_name'], '원티드')

    def test_legacy_via_recruiter_is_not_a_job_site(self):
        self.assertEqual(self.store.tracker_job(row(1, via='반석써치'))['site'], 'unknown')
        self.assertEqual(self.store.tracker_job(row(1, via='원티드'))['site'], 'wanted')
        legacy = row(1, via='원티드')
        legacy['notes'] = 'https://career.rememberapp.co.kr/job/posting/123'
        self.assertEqual(self.store.tracker_job(legacy)['site'], 'remember')

    def test_decision_survives_crawl_refresh_and_url_variant(self):
        self.decide(status='excluded')
        old = candidate(1)
        old.update(id='new_cluster', aliases=['new_listing'], url='https://wanted.co.kr/wd/1?utm_campaign=new')
        self.export([old, candidate(2)])
        self.assertEqual(self.ids(), ['li_2'])

    def test_identity_cluster_survives_repost_url(self):
        self.decide(status='excluded')
        changed = candidate(1)
        changed.update(url='https://wanted.co.kr/wd/900', aliases=['li_1', 'li_new'])
        self.export([changed, candidate(2)])
        self.assertEqual(self.ids(), ['li_2'])

    def test_restoration_reintroduces_candidate(self):
        self.decide()
        previous = self.store.decision_data()['decisions']['li_1']['updated_at']
        self.decide(status='new', expected_updated_at=previous)
        self.assertIn('li_1', self.ids())
        self.assertEqual(self.store.dashboard()['pending']['total'], 0)

    def test_filters_unverified_expired_and_old_evidence(self):
        jobs = [candidate(i) for i in range(1, 5)]
        jobs[0]['dashboard_eligible'] = False
        jobs[1]['liveness'] = 'expired'
        jobs[2]['verified_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=73)).isoformat()
        jobs[3]['verified_at'] = 'unknown'
        self.export(jobs)
        self.assertEqual(self.ids(), [])

    def test_72_hour_boundary_is_required_even_when_export_recent(self):
        job = candidate(1)
        job['verified_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=71)).isoformat()
        self.export([job])
        self.assertEqual(self.ids(), ['li_1'])

    def test_slack_cooldown_does_not_block_dashboard(self):
        self.export([candidate(1, recommendation_cooldown=True, recommendation_eligible=False)])
        self.assertEqual(self.ids(), ['li_1'])

    def test_company_rejection_is_application_not_user_exclusion(self):
        self.store.rows = [row(1, 'Rejected'), row(2, 'SKIP'), row(3, 'Applied')]
        dashboard = self.store.dashboard()
        self.assertEqual({x['canonical_status'] for x in dashboard['applications']}, {'Rejected', 'Applied'})
        self.assertEqual(dashboard['excluded']['total'], 1)
        self.assertNotIn('li_1', self.ids())
        self.assertNotIn('li_3', self.ids())

    def test_discarded_application_keeps_application_context(self):
        closed_application = row(1, 'Discarded')
        closed_application['notes'] = '지원 완료; 60일 무응답 자동 종료'
        self.store.rows = [closed_application, row(2, 'Discarded')]
        dashboard = self.store.dashboard()
        self.assertEqual(dashboard['applications'][0]['tracker_id'], 1)
        self.assertEqual(dashboard['excluded']['items'][0]['tracker_id'], 2)

    def test_company_only_never_matches_other_role(self):
        item = row(1, 'Applied')
        item['role'] = '다른 직무'
        self.store.rows = [item]
        self.assertIn('li_1', self.ids())

    def test_different_explicit_posting_does_not_match_same_title_or_tracker_hint(self):
        item = row(1, 'Applied')
        item['notes'] = 'Applied 2026-09-01; https://www.wanted.co.kr/wd/900'
        self.store.rows = [item]
        job = candidate(1)
        job['tracker_id'] = 1
        self.export([job])
        self.assertIsNone(self.store.matched_row(server.public_job(job), self.store.rows))
        self.assertIn('li_1', self.ids())

    def test_different_saramin_requisition_is_not_same_application(self):
        item = row(1, 'Applied')
        item['notes'] = 'https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=900'
        job = candidate(1)
        job['url'] = 'https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1'
        self.assertIsNone(self.store.matched_row(server.public_job(job), [item]))

    def test_company_title_cluster_does_not_merge_distinct_listings(self):
        left, right = candidate(1), candidate(2)
        left['aliases'].append('pc_shared_company_title')
        right['aliases'].append('pc_shared_company_title')
        self.assertFalse(server.aliases(left) & server.aliases(right))

    def test_applied_sidecar_stays_visible_after_discarded_lifecycle(self):
        self.decide(status='applied')
        self.store.lifecycle('1', {'status': 'Discarded'})
        data = self.store.dashboard()
        self.assertEqual(len(data['applications']), 1)
        self.assertEqual(data['applications'][0]['canonical_status'], 'Discarded')
        self.assertEqual(data['excluded']['total'], 0)

    def test_legacy_discarded_restore_returns_honest_error(self):
        self.store.rows = [row(1, 'Discarded')]
        with self.assertRaises(server.APIError) as exc:
            self.decide('tracker_1', 'new')
        self.assertEqual(exc.exception.code, 'closed_history_not_restorable')
        self.assertEqual(self.store.rows[0]['status'], 'Discarded')

    def test_restored_fresh_sidecar_survives_export_omission(self):
        self.decide()
        self.export([])
        self.decide(status='new')
        self.store.refill_recommendations()
        self.assertIn('li_1', self.ids())

    def test_newer_expired_export_overrides_saved_fresh_sidecar(self):
        self.decide()
        expired = candidate(1)
        expired.update(liveness='expired', dashboard_eligible=False)
        self.export([expired])
        self.decide(status='new')
        self.assertNotIn('li_1', self.ids())

    def test_decision_display_date_is_separate_from_revision(self):
        self.decide()
        pending = self.store.dashboard()['pending']['items'][0]
        self.assertEqual(pending['decided_at'], pending['updated_at'])
        self.assertIn('T', pending['decided_at'])

    def test_existing_exclusion_calls_canonical_skip(self):
        self.store.rows = [row(1)]
        self.decide(status='excluded')
        self.assertEqual(self.store.calls, [('set', 1, 'SKIP')])
        self.decide(status='new')
        self.assertEqual(self.store.rows[0]['status'], 'Evaluated')

    def test_pending_is_separate_from_canonical_evaluated(self):
        self.store.rows = [row(1)]
        self.decide()
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.store.rows[0]['status'], 'Evaluated')

    def test_untracked_applied_creates_one_canonical_row_and_retry_is_idempotent(self):
        self.decide(status='applied')
        self.decide(status='applied')
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(len(self.store.dashboard()['applications']), 1)
        self.assertEqual(self.store.calls, [('add', 1)])

    def test_application_cannot_be_silently_reset_to_new(self):
        self.store.rows = [row(1, 'Applied')]
        with self.assertRaises(server.APIError) as exc:
            self.decide(status='new')
        self.assertEqual(exc.exception.status, 409)

    def test_optimistic_conflict_preserves_previous_decision(self):
        self.decide(expected_updated_at='')
        with self.assertRaises(server.APIError) as exc:
            self.decide(status='excluded', expected_updated_at='')
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(self.store.decision_data()['decisions']['li_1']['status'], 'pending')

    def test_concurrent_decisions_do_not_lose_records(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.decide(f'li_{n}'), range(1, 9)))
        self.assertEqual(len(self.store.decision_data()['decisions']), 8)
        self.assertEqual(self.c.decisions.stat().st_mode & 0o777, 0o600)

    def test_corrupt_decision_file_fails_closed(self):
        self.c.decisions.write_text('{broken')
        with self.assertRaises(server.APIError) as exc:
            self.store.dashboard()
        self.assertEqual(exc.exception.status, 503)
        self.assertEqual(self.c.decisions.read_text(), '{broken')

    def test_unknown_job_cannot_create_arbitrary_record(self):
        with self.assertRaises(server.APIError) as exc:
            self.decide('/etc/passwd')
        self.assertEqual(exc.exception.status, 404)
        self.assertFalse(self.c.decisions.exists())

    def test_partial_save_is_visible_and_retry_recovers_without_duplicate(self):
        original_write = server.atomic_json
        def fail_decision_write(path, value):
            if path == self.c.decisions:
                raise OSError('synthetic disk failure')
            return original_write(path, value)
        with mock.patch.object(server, 'atomic_json', side_effect=fail_decision_write):
            with self.assertRaises(server.APIError) as exc:
                self.decide(status='applied')
        self.assertTrue(exc.exception.details['partial_commit'])
        self.assertEqual(len(self.store.rows), 1)
        self.assertTrue(self.store.dashboard()['warnings'])
        with self.assertRaises(server.APIError) as blocked:
            self.decide('li_2')
        self.assertEqual(blocked.exception.code, 'unfinished_change')
        self.decide(status='applied')
        self.assertEqual(len(self.store.rows), 1)
        self.assertFalse((self.c.data / 'dashboard-write-journal.json').exists())

    def test_safe_merge_refusal_does_not_block_subsequent_decisions(self):
        with mock.patch.object(self.store, 'add_applied', side_effect=server.APIError(409, 'ambiguous_application_merge', 'Ambiguous synthetic merge')):
            with self.assertRaises(server.APIError) as exc:
                self.decide(status='applied')
        self.assertFalse(exc.exception.details.get('partial_commit'))
        self.assertFalse((self.c.data / 'dashboard-write-journal.json').exists())
        self.decide('li_2', 'pending')
        self.assertEqual(self.store.dashboard()['pending']['total'], 1)

    def test_canonical_lifecycle_update_preserves_history_category(self):
        self.store.rows = [row(1, 'Applied')]
        self.store.lifecycle('1', {'status': 'Interview', 'expected_updated_at': self.store.row_revision(self.store.rows[0])})
        self.assertEqual(self.store.dashboard()['applications'][0]['canonical_status'], 'Interview')

    def test_unsafe_urls_are_not_exposed(self):
        job = candidate(1)
        job['url'] = 'javascript:alert(1)'
        self.export([job])
        self.assertEqual(self.store.dashboard()['sites'][0]['jobs'][0]['url'], '')


class HTTPTests(unittest.TestCase):
    export = StoreTests.export

    def setUp(self):
        StoreTests.setUp(self)
        self.c.port = 0
        self.c.host = '127.0.0.1'
        self.c.origin = 'https://career.aplz.net'
        self.c.dev = False
        self.c.password_hash = server.hash_password('temporary-test-password')
        self.httpd = server.AppServer(self.c, self.store)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=3)
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw)
        connection.close()
        return result

    def auth(self):
        status, headers, body = self.request('POST', '/api/login', {'password': 'temporary-test-password'}, {'Origin': self.c.origin})
        self.assertEqual(status, 200)
        return {'Cookie': headers['Set-Cookie'].split(';')[0], 'Origin': self.c.origin, 'X-CSRF-Token': body['csrf_token']}

    def test_auth_required_for_all_private_data(self):
        for path in ['/api/dashboard', '/api/decisions?status=pending']:
            self.assertEqual(self.request('GET', path)[0], 401)
        self.assertEqual(self.request('GET', '/api/session')[2], {'authenticated': False})

    def test_authenticated_mutation_and_persistent_cookie_security(self):
        status, headers, data = self.request('POST', '/api/login', {'password': 'temporary-test-password'}, {'Origin': self.c.origin})
        self.assertEqual(status, 200)
        for flag in ('HttpOnly', 'Secure', 'SameSite=Strict'):
            self.assertIn(flag, headers['Set-Cookie'])
        auth = {'Cookie': headers['Set-Cookie'].split(';')[0], 'Origin': self.c.origin, 'X-CSRF-Token': data['csrf_token']}
        result = self.request('POST', '/api/decisions', {'id': 'li_1', 'status': 'pending', 'expected_updated_at': ''}, auth)
        self.assertEqual(result[0], 200)
        self.assertEqual(result[2]['dashboard']['pending']['total'], 1)

    def test_cross_origin_and_missing_csrf_cannot_mutate(self):
        auth = self.auth()
        bad_origin = dict(auth, Origin='https://other.example')
        self.assertEqual(self.request('POST', '/api/decisions', {'id': 'li_1', 'status': 'pending'}, bad_origin)[0], 403)
        auth.pop('X-CSRF-Token')
        self.assertEqual(self.request('POST', '/api/decisions', {'id': 'li_1', 'status': 'pending'}, auth)[0], 403)
        self.assertFalse(self.c.decisions.exists())

    def test_logout_invalidates_session(self):
        auth = self.auth()
        self.assertEqual(self.request('POST', '/api/logout', {}, auth)[0], 200)
        self.assertEqual(self.request('GET', '/api/dashboard', headers=auth)[0], 401)

    def test_bad_password_and_rate_limit(self):
        self.assertEqual(self.request('POST', '/api/login', {'password': 'wrong'}, {'Origin': self.c.origin})[0], 401)
        self.httpd.attempts['global'] = [__import__('time').monotonic()] * 20
        self.assertEqual(self.request('POST', '/api/login', {'password': 'wrong'}, {'Origin': self.c.origin})[0], 429)

    def test_no_private_files_or_traversal(self):
        for path in ['/.env', '/%2e%2e/server.py', '/dashboard-candidates.json', '/assets/test.map']:
            self.assertEqual(self.request('GET', path)[0], 404)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_csp_hashes_only_exact_inline_scripts_without_unsafe_inline(self):
        import base64, hashlib
        inline = 'window.__bootstrap = "테스트 &value";\n'
        html = '<html><script>' + inline + '</script><script src="/assets/main.js">ignored</script></html>'
        self.c.static.joinpath('index.html').write_text(html)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        policy = headers['Content-Security-Policy']
        script_policy = next(part.strip() for part in policy.split(';') if part.strip().startswith('script-src'))
        digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
        self.assertEqual(script_policy, "script-src 'self' 'sha256-" + digest + "'")
        self.assertNotIn('unsafe-inline', script_policy)
        self.assertNotIn(base64.b64encode(hashlib.sha256(b'ignored').digest()).decode(), script_policy)

    def test_size_and_pagination_validation(self):
        auth = self.auth()
        status = self.request('POST', '/api/decisions', {'id': 'li_1', 'status': 'pending', 'note': 'x' * 9000}, auth)[0]
        self.assertEqual(status, 413)
        self.assertEqual(self.request('GET', '/api/decisions?status=pending&offset=-1', headers=auth)[0], 400)

    def test_archive_endpoint_filters_site_and_status_independently(self):
        self.store.decide({'id': 'li_1', 'status': 'pending'})
        self.store.decide({'id': 'li_2', 'status': 'excluded'})
        auth = self.auth()
        status, headers, page = self.request('GET', '/api/decisions?status=pending&site=wanted&offset=0&limit=5', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual([job['id'] for job in page['items']], ['li_1'])
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['site'], 'wanted')
        self.assertEqual(page['sources'], [{'id': 'wanted', 'name': '원티드', 'total': 1}])
        self.assertEqual(self.request('GET', '/api/decisions?status=excluded&site=saramin&offset=0&limit=5', headers=auth)[2]['total'], 0)
        self.assertEqual(self.request('GET', '/api/decisions?status=excluded&site=all&offset=0&limit=5', headers=auth)[2]['items'][0]['id'], 'li_2')
        self.assertEqual(self.request('GET', '/api/decisions?status=pending', headers=auth)[2]['offset'], 5)
        self.assertEqual(self.request('GET', '/api/decisions?status=pending&site=%00', headers=auth)[0], 400)


if __name__ == '__main__':
    unittest.main()
