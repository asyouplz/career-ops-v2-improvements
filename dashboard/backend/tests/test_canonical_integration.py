"""Real canonical CLI integration, exclusively temporary synthetic tracker data."""
import shutil
import tempfile
import unittest
from pathlib import Path

import server
import mail_store
from test_server import candidate


class CanonicalIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='career-dashboard-canonical-test-')
        self.addCleanup(self.temp.cleanup)
        c = self.c = server.Config()
        c.root = Path(__file__).resolve().parents[3] / 'engine'
        c.node = shutil.which('node')
        c.data = Path(self.temp.name)
        c.export = c.data / 'dashboard-candidates.json'
        c.decisions = c.data / 'dashboard-decisions.json'
        c.tracker = c.data / 'applications.md'
        c.gmail_credentials = c.data / 'missing-test-credentials.json'
        c.tracker.write_text('# Synthetic tracker\n\n| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n|---|------|---------|------|-------|--------|-----|--------|-------|\n| 1 | 2026-09-01 | 회사1 | 회계 팀장 | N/A | Evaluated | - | - | synthetic local test |\n')
        self.export([candidate(1), candidate(2)])
        self.store = server.Store(c)

    def export(self, jobs):
        server.atomic_json(self.c.export, {'version': 1, 'generated_at': server.now(), 'source_run_at': server.now(), 'jobs': jobs, 'sources': []})

    def decide(self, ident, status):
        return self.store.decide({'id': ident, 'status': status})

    def test_status_restore_new_application_repeat_and_discarded_lifecycle(self):
        initial = self.store.tracker_rows()[0]
        self.assertEqual(len(initial['raw_hash']), 64)
        self.decide('li_1', 'excluded')
        self.assertEqual(self.store.tracker_rows()[0]['status'], 'SKIP')
        with self.assertRaises(server.APIError) as stale:
            self.store.set_canonical(initial, 'Applied', 'stale synthetic call')
        self.assertEqual(stale.exception.status, 409)
        self.decide('li_1', 'new')
        self.assertEqual(self.store.tracker_rows()[0]['status'], 'Evaluated')
        self.decide('li_1', 'applied')
        self.decide('li_2', 'applied')
        self.decide('li_2', 'applied')
        self.assertEqual(len(self.store.tracker_rows()), 2)
        self.store.lifecycle('2', {'status': 'Interview'})
        self.assertEqual(next(r for r in self.store.tracker_rows() if r['num'] == 2)['status'], 'Interview')
        self.store.lifecycle('2', {'status': 'Discarded'})
        self.assertEqual(len(self.store.dashboard()['applications']), 2)

    def test_ambiguous_fuzzy_merge_is_refused_before_any_tracker_change(self):
        self.decide('li_2', 'applied')
        different = candidate(900)
        different.update(company='회사2', title='회계 팀장')
        self.export([different])
        before = self.c.tracker.read_bytes()
        with self.assertRaises(server.APIError) as ambiguous:
            self.decide('li_900', 'applied')
        self.assertEqual(ambiguous.exception.code, 'ambiguous_application_merge')
        self.assertEqual(self.c.tracker.read_bytes(), before)
        self.assertFalse((self.c.data / 'dashboard-write-journal.json').exists())

    def test_mail_decline_new_row_and_withdrawal_preserve_canonical_history(self):
        def event(number, company, role, kind):
            return {'event_id': 'synthetic:' + str(number), 'thread_id': 'synthetic-thread-' + str(number), 'message_id': 'synthetic-message-' + str(number), 'event_at': '2026-09-01T01:00:00Z', 'company': company, 'role': role, 'kind': kind, 'evidence_text': '합성 검증용 근거', 'mail_url': 'https://mail.google.com/mail/#all/synthetic-' + str(number), 'source_name': 'headhunter_mail'}
        decline = {'version': 1, 'run_id': 'synthetic-decline', 'complete': True, 'events': [event(1, '합성 KFC', '재무팀장', 'declined')]}
        result = mail_store.ingest(self.store, decline, True)
        self.assertTrue(result['ok'])
        declined = next(r for r in self.store.tracker_rows() if r['company'] == '합성 KFC')
        self.assertEqual(declined['status'], 'SKIP')
        self.assertIn('메일 근거에 따른 지원 이력 기록', declined['notes'])
        self.assertNotIn('사용자 대시보드에서 지원 완료', declined['notes'])
        self.decide('li_1', 'applied')
        # A current explicit withdrawal is later than the manual application record.
        withdrawal_event = event(2, '회사1', '회계 팀장', 'withdrawn')
        withdrawal_event['event_at'] = server.now()
        result = mail_store.ingest(self.store, {'version': 1, 'run_id': 'synthetic-withdrawal', 'complete': True, 'events': [withdrawal_event]}, True)
        self.assertTrue(result['ok'])
        closed = next(r for r in self.store.tracker_rows() if r['num'] == 1)
        self.assertEqual(closed['status'], 'Discarded')
        self.assertIn('지원철회', closed['notes'])
        self.assertEqual(len(self.store.dashboard()['applications']), 0)
        self.assertEqual(self.store.dashboard()['excluded']['total'], 2)
        self.assertTrue(any(self.c.data.joinpath('dashboard-backups').iterdir()))

    def test_historical_mail_transition_uses_actual_event_date(self):
        event = {'event_id': 'synthetic:historical', 'thread_id': 'synthetic-thread', 'message_id': 'synthetic-message', 'event_at': '2026-09-01T01:00:00Z', 'company': '회사1', 'role': '회계 팀장', 'kind': 'declined', 'evidence_text': '합성 거절 근거', 'mail_url': 'https://mail.google.com/mail/#all/synthetic-thread', 'source_name': 'headhunter_mail'}
        result = mail_store.ingest(self.store, {'version': 1, 'run_id': 'synthetic-date', 'complete': True, 'events': [event]}, True)
        self.assertTrue(result['ok'])
        log = self.c.tracker.with_name('status-log.tsv').read_text()
        self.assertIn('2026-09-01', log)
        self.assertIn('Evaluated\tSKIP', log)


if __name__ == '__main__':
    unittest.main()
