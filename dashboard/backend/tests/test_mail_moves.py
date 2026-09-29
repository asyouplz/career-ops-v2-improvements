"""Mail and move regressions use isolated synthetic stores and messages only."""
import copy
import datetime as dt
import unittest
from unittest import mock

import mail_store
import server
import test_server
from test_server import StoreTests, FakeStore, row, candidate


def event(n='1', kind='proposal_unanswered', company='회사1', role='회계 팀장', **extra):
    value = {'event_id': 'thread1:message' + str(n) + ':' + kind, 'thread_id': 'thread1', 'message_id': 'message' + str(n), 'event_at': '2026-09-01T01:00:00Z', 'company': company, 'role': role, 'kind': kind, 'evidence_text': '합성 메일 근거입니다.', 'mail_url': 'https://mail.google.com/mail/#all/thread1', 'source_name': 'headhunter_mail', 'reason': ''}
    value.update(extra)
    return value


def bundle(*events, **extra):
    return {'version': 1, 'run_id': 'synthetic-run', 'window_start': '2025-09-07T00:00:00Z', 'window_end': '2026-09-07T00:00:00Z', 'complete': True, 'events': list(events), 'errors': [], **extra}


class MailFakeStore(FakeStore):
    def add_canonical_record(self, job, note, rows, status, event_date=None):
        number = max([r['num'] for r in self.rows] + [0]) + 1
        new = row(number, status)
        new.update(company=job['company'], role=job['title'], notes=note + '; dashboard-id:' + job['id'], date=event_date)
        self.rows.append(new)
        self.calls.append(('add', number, status))
        return copy.deepcopy(new)


class FeatureTests(unittest.TestCase):
    def setUp(self):
        # Reuse setup helpers without inheriting and rerunning the old test suite.
        StoreTests.setUp(self)
        self.store = MailFakeStore(self.c)

    export = StoreTests.export

    def job(self, ident='li_1'):
        return self.store.job_context(ident)[0]

    def move(self, status='pending', ident='li_1', **extra):
        return self.store.move({'id': ident, 'status': status, 'expected_updated_at': self.job(ident)['decision_revision'], **extra})

    def test_move_refills_and_undo_restores_note_and_only_once(self):
        self.move(note='다음 주 검토')
        result = self.move('excluded')
        self.assertEqual(self.store.dashboard()['excluded']['total'], 1)
        self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(self.job()['movement_status'], 'pending')
        self.assertEqual(self.job()['note'], '다음 주 검토')
        self.assertEqual(len(self.store.job_history('li_1')['events']), 3)
        with self.assertRaises(server.APIError):
            self.store.undo_move({'token': result['undo_token']})

    def test_move_same_destination_has_no_write_or_undo(self):
        self.move()
        before = self.c.decisions.read_bytes()
        result = self.move()
        self.assertTrue(result['unchanged'])
        self.assertNotIn('undo_token', result)
        self.assertEqual(before, self.c.decisions.read_bytes())

    def test_move_and_undo_cannot_overwrite_later_manual_or_note(self):
        result = self.move()
        self.store.update_note('li_1', {'note': '새 메모', 'expected_updated_at': self.job()['decision_revision']})
        with self.assertRaises(server.APIError) as error:
            self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(error.exception.code, 'undo_conflict')
        self.assertEqual(self.job()['note'], '새 메모')

    def test_note_response_contains_saved_job_and_new_revision(self):
        before = self.job()['decision_revision']
        result = self.store.update_note('li_1', {'note': '저장된 메모', 'expected_updated_at': before})
        self.assertEqual(result['job']['note'], '저장된 메모')
        self.assertNotEqual(result['job']['decision_revision'], before)
        self.assertEqual(result['job']['decision_revision'], self.job()['decision_revision'])

    def test_move_cas_checks_canonical_change_even_when_decision_unchanged(self):
        self.store.rows = [row(1)]
        old = self.job()['decision_revision']
        self.store.rows[0]['status'] = 'Applied'
        with self.assertRaises(server.APIError) as error:
            self.store.move({'id': 'li_1', 'status': 'pending', 'expected_updated_at': old})
        self.assertEqual(error.exception.code, 'decision_conflict')
        self.assertFalse(self.c.decisions.exists())

    def test_undo_expires_after_ten_seconds(self):
        result = self.move()
        with mock.patch('server.time.time', return_value=server.time.time() + 11):
            with self.assertRaises(server.APIError) as error:
                self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(error.exception.code, 'undo_expired')

    def test_application_origin_and_target_are_rejected(self):
        self.store.rows = [row(1, 'Applied')]
        for status in ['pending', 'excluded', 'new']:
            with self.assertRaises(server.APIError):
                self.move(status)
        with self.assertRaises(server.APIError):
            self.move('applied', 'li_2')
        self.assertFalse(self.job('tracker_1')['movable'])

    def test_preapplication_discarded_can_move_to_hold_without_reopening_canonical(self):
        closed = row(1, 'Discarded')
        closed['notes'] = '지원하지 않기로 결정; 이력서 미제출'
        self.store.rows = [closed]
        before = copy.deepcopy(self.store.rows)
        initial = self.job('tracker_1')
        self.assertTrue(initial['movable'])
        self.assertEqual(initial['allowed_moves'], ['pending'])
        moved = self.move('pending', 'tracker_1')
        self.assertEqual(self.store.rows, before)
        self.assertEqual(self.job('tracker_1')['note'], closed['notes'])
        self.store.undo_move({'token': moved['undo_token']})
        self.assertEqual(self.store.rows, before)
        self.assertEqual(self.job('tracker_1')['movement_status'], 'excluded')
        self.assertEqual(self.job('tracker_1')['note'], closed['notes'])

    def test_preapplication_discarded_active_restore_undo_restores_canonical_state(self):
        closed = row(1, 'Discarded')
        closed['notes'] = '검토 당시 마감; https://www.wanted.co.kr/wd/1'
        original_note = closed['notes']
        self.store.rows = [closed]
        self.assertIn('new', self.job('tracker_1')['allowed_moves'])
        result = self.move('new', 'tracker_1')
        self.assertEqual(self.store.rows[0]['status'], 'Evaluated')
        refilled = self.store.refill_recommendations()['dashboard']
        self.assertIn('li_1', [j['id'] for group in refilled['sites'] for j in group['jobs']])
        self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(self.store.rows[0]['status'], 'Discarded')
        self.assertEqual(self.job('tracker_1')['movement_status'], 'excluded')
        self.assertEqual(self.job('tracker_1')['note'], original_note)

    def test_discarded_after_application_remains_immovable(self):
        closed = row(14, 'Discarded')
        closed['notes'] = 'Applied 2026-06-01; 60일 무응답 자동 종료'
        self.store.rows = [closed]
        job = self.job('tracker_14')
        self.assertFalse(job['movable'])
        self.assertEqual(job['allowed_moves'], [])
        with self.assertRaises(server.APIError):
            self.move('pending', 'tracker_14')

    def test_stale_restore_keeps_archive(self):
        self.move()
        old = candidate(1)
        old['liveness'] = 'expired'
        old['dashboard_eligible'] = False
        self.export([old])
        with self.assertRaises(server.APIError) as error:
            self.move('new')
        self.assertEqual(error.exception.code, 'posting_not_active')
        self.assertEqual(self.job()['movement_status'], 'pending')

    def test_allowed_moves_matches_restore_and_application_guards(self):
        self.assertEqual(self.job()['allowed_moves'], ['pending', 'excluded'])
        self.move()
        self.assertEqual(self.job()['allowed_moves'], ['new', 'excluded'])
        closed = candidate(1)
        closed.update(liveness='expired', dashboard_eligible=False)
        self.export([closed])
        self.assertEqual(self.job()['allowed_moves'], ['excluded'])
        self.store.rows = [row(1, 'Applied')]
        self.assertEqual(self.job()['allowed_moves'], [])

    def test_mail_only_unverified_item_cannot_offer_recommendation_destination(self):
        mail_store.ingest(self.store, bundle(event(company='메일 전용 회사')), True)
        job = self.store.dashboard()['pending']['items'][0]
        self.assertEqual(job['allowed_moves'], ['excluded'])

    def test_preview_does_not_write_and_repeat_is_idempotent(self):
        data = bundle(event())
        before = set(self.c.data.iterdir())
        preview = mail_store.ingest(self.store, data)
        self.assertEqual(preview['applied_count'], 1)
        self.assertEqual(before, set(self.c.data.iterdir()))
        first = mail_store.ingest(self.store, data, True)
        second = mail_store.ingest(self.store, data, True)
        self.assertEqual(first['applied_count'], 1)
        self.assertEqual(second['duplicate_count'], 1)
        self.assertEqual(self.store.dashboard()['pending']['total'], 1)

    def test_unanswered_proposal_goes_pending_with_source_and_history(self):
        mail_store.ingest(self.store, bundle(event()), True)
        pending = self.job()
        self.assertEqual(pending['movement_status'], 'pending')
        self.assertEqual(pending['site'], 'wanted')
        self.assertEqual(pending['mail_source'], '헤드헌터 메일')
        self.assertEqual(pending['classification_reason'], '제안 메일 미회신')
        self.assertNotIn('li_1', [j['id'] for g in self.store.dashboard()['sites'] for j in g['jobs']])

    def test_email_only_decline_is_one_exclusion_with_kfc_evidence(self):
        data = bundle(event(kind='declined', company='KFC코리아', role='재무팀장', reason='연봉 조건 불일치'))
        result = mail_store.ingest(self.store, data, True)
        self.assertEqual(result['applied_count'], 1)
        excluded = self.store.dashboard()['excluded']['items']
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]['site'], 'headhunter_mail')
        self.assertEqual(excluded[0]['canonical_status'], 'SKIP')
        self.assertEqual(excluded[0]['classification_reason'], '연봉 조건 불일치')
        history = self.store.job_history(excluded[0]['id'])
        self.assertTrue(any(e['kind'] == 'declined' and 'mail.google.com' in e['url'] for e in history['events']))
        mail_store.ingest(self.store, data, True)
        self.assertEqual(len(self.store.rows), 1)

    def test_mail_applied_stage_distinguishes_intent_documents_and_receipt(self):
        for index, (reason, expected) in enumerate([('지원 의사 전달', '지원 의사 전달'), ('지원 서류 전달', '서류 전달'), ('지원 접수 확인', '접수 확인')], 1):
            data = bundle(event(n=index, kind='applied', reason=reason, event_at=f'2026-09-0{index}T01:00:00Z'))
            mail_store.ingest(self.store, data, True)
            item = self.store.dashboard()['applications'][0]
            self.assertEqual(item['canonical_status'], 'Applied')
            self.assertEqual(item['application_stage_label'], expected)
        self.store.decide({'id': 'li_1', 'status': 'applied'})
        self.assertNotIn('application_stage_label', self.store.dashboard()['applications'][0])

    def test_withdrawn_is_excluded_preserves_application_and_blocks_move(self):
        self.store.rows = [row(1, 'Applied')]
        data = bundle(event(kind='withdrawn', reason='지원철회'))
        mail_store.ingest(self.store, data, True)
        dashboard = self.store.dashboard()
        self.assertEqual(dashboard['applications'], [])
        self.assertEqual(dashboard['excluded']['total'], 1)
        job = dashboard['excluded']['items'][0]
        self.assertEqual(self.store.rows[0]['status'], 'Discarded')
        self.assertTrue(job['ever_applied'])
        self.assertTrue(job['withdrawn'])
        self.assertFalse(job['movable'])
        with self.assertRaises(server.APIError):
            self.move('pending', job['id'])

    def test_employer_response_wait_never_becomes_unanswered_proposal(self):
        self.store.rows = [row(1, 'Applied')]
        result = mail_store.ingest(self.store, bundle(event()), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(len(self.store.dashboard()['applications']), 1)
        self.assertEqual(self.store.dashboard()['pending']['total'], 0)

    def test_old_mail_cannot_overwrite_recent_manual_decision(self):
        self.move('excluded')
        result = mail_store.ingest(self.store, bundle(event()), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(self.job()['movement_status'], 'excluded')
        self.assertEqual(self.store.job_history('li_1')['events'][1]['outcome'], 'older_than_current_decision')

    def test_mail_evidence_invalidates_undo_even_if_status_is_preserved(self):
        result = self.move()
        mail_store.ingest(self.store, bundle(event()), True)
        with self.assertRaises(server.APIError) as error:
            self.store.undo_move({'token': result['undo_token']})
        self.assertEqual(error.exception.code, 'undo_conflict')

    def test_note_only_does_not_block_historical_mail_classification(self):
        self.store.update_note('li_1', {'note': '월요일에 확인', 'expected_updated_at': self.job()['decision_revision']})
        result = mail_store.ingest(self.store, bundle(event(kind='declined')), True)
        self.assertEqual(result['applied_count'], 1)
        self.assertEqual(self.job()['movement_status'], 'excluded')
        self.assertEqual(self.job()['note'], '월요일에 확인')

    def test_uncertain_unapplied_history_does_not_block_manual_moves(self):
        self.move('excluded')
        mail_store.ingest(self.store, bundle(event(kind='applied', needs_review=True)), True)
        self.assertTrue(self.job()['movable'])
        self.assertFalse(self.job()['ever_applied'])

    def test_unconfirmed_new_application_is_reviewable_hold_not_application_lock(self):
        mail_store.ingest(self.store, bundle(event(kind='applied', needs_review=True)), True)
        self.assertEqual(self.job()['movement_status'], 'pending')
        self.assertTrue(self.job()['needs_review'])
        self.assertTrue(self.job()['movable'])
        self.assertFalse(self.store.rows)

    def test_sync_review_count_is_outstanding_after_duplicate_run(self):
        data = bundle(event(needs_review=True))
        mail_store.ingest(self.store, data, True)
        repeated = mail_store.ingest(self.store, data, True)
        self.assertEqual(repeated['review_count'], 0)
        server.atomic_json(self.c.data / 'mail-sync-state.json', {'status': 'completed', 'review_count': repeated['review_count'], 'applied_count': 0})
        sync = self.store.mail_sync_status()
        self.assertEqual(sync['review_count'], 1)
        self.assertEqual(sync['last_run_review_count'], 0)
        self.assertEqual(self.store.dashboard()['mail_sync']['review_count'], 1)

    def test_company_only_match_does_not_exclude_another_role(self):
        mail_store.ingest(self.store, bundle(event(kind='declined', role='영업팀장')), True)
        self.assertEqual(self.job()['movement_status'], 'new')
        excluded = self.store.dashboard()['excluded']['items']
        self.assertEqual(excluded[0]['title'], '영업팀장')

    def test_bilingual_company_receipts_same_role_seconds_apart_reuse_rejected_row(self):
        existing = row(13, 'Rejected')
        existing.update(company='핀다 (FINDA)', role='Finance Team Lead', date='2026-07-14')
        self.store.rows = [existing]
        first = event(n='finda1', kind='applied', company='핀다(FINDA)', role='Finance Team Lead', reason='지원 접수 확인', event_at='2026-07-14T11:46:05Z', thread_id='receipt-one')
        second = event(n='finda2', kind='applied', company='FINDA', role='Finance Team Lead', reason='지원 접수 확인', event_at='2026-07-14T11:46:08Z', thread_id='receipt-two')
        result = mail_store.ingest(self.store, bundle(first, second), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]['status'], 'Rejected')
        history = self.store.job_history('tracker_13')['events']
        self.assertEqual(sum(item['kind'] == 'applied' for item in history), 2)
        linked = self.store.mail_ledger()['events'][second['event_id']]
        self.assertEqual(linked['identity_source'], 'explicit_bilingual_alias')
        self.assertEqual(linked['company'], 'FINDA')
        self.assertIn('핀다 (FINDA)', linked['identity_evidence'])

    def test_bilingual_alias_does_not_match_without_receipt_pair_or_across_role(self):
        existing = row(13, 'Rejected')
        existing.update(company='핀다 (FINDA)', role='Finance Team Lead', date='2026-07-14')
        self.store.rows = [existing]
        bare = event(n='finda2', kind='applied', company='FINDA', role='Finance Team Lead', reason='지원 접수 확인', event_at='2026-07-14T11:46:08Z')
        normalized = mail_store.validate_bundle(bundle(bare))[0]
        args = (self.store, normalized, self.store.mail_ledger(), self.store.decision_data(), self.store.rows, self.store.export_data()['jobs'])
        self.assertIsNone(mail_store.resolve_job(*args)[3])
        peer = mail_store.validate_bundle(bundle(event(n='finda1', kind='applied', company='핀다(FINDA)', role='Finance Team Lead', reason='지원 접수 확인', event_at='2026-07-14T11:46:05Z')))[0]
        normalized['role'] = 'Senior Finance Team Lead'
        self.assertIsNone(mail_store.resolve_job(*args, peer_events=[peer])[3])
        normalized['role'] = 'Finance Team Lead'
        normalized['event_at'] = '2026-07-14T12:00:00.000000Z'
        self.assertIsNone(mail_store.resolve_job(*args, peer_events=[peer])[3])

    def test_reviewed_cross_source_identity_keeps_separate_evidence(self):
        mail = event(kind='declined', role='재무팀장', job_url='https://www.wanted.co.kr/wd/1', identity_source='operator_reviewed', identity_evidence='별도 원문에서 회사와 직무 세부 업무를 대조한 연결 근거')
        mail_store.ingest(self.store, bundle(mail), True)
        job = self.job()
        self.assertEqual(job['movement_status'], 'excluded')
        self.assertEqual(job['site'], 'wanted')
        self.assertEqual(job['mail_source'], '헤드헌터 메일')
        detail = next(e for e in self.store.job_history('li_1')['events'] if e['kind'] == 'declined')
        self.assertEqual(detail['identity_source'], 'operator_reviewed')
        self.assertEqual(detail['identity_evidence'], mail['identity_evidence'])
        self.assertEqual(detail['posting_url'], mail['job_url'])
        self.assertEqual(detail['evidence'], mail['evidence_text'])

    def test_reviewed_tracker_link_preserves_history_only_identity_and_future_replies(self):
        tracked = row(26, 'Rejected')
        tracked.update(company='알스퀘어(RSQUARE)', role='회계/자금 담당 (7년 이상)', date='2026-07-25')
        self.store.rows = [tracked]
        reviewed = event(n='reviewed', kind='applied', company='RSQUARE', role='회계/자금 담당', thread_id='reviewed-thread', event_at='2026-07-25T01:00:00Z', tracker_id='26', apply=False, identity_source='operator_reviewed', identity_evidence='원본 접수 메일과 기존 기록의 회사, 전체 직무, 지원 날짜를 대조했습니다.')
        result = mail_store.ingest(self.store, bundle(reviewed), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertFalse(self.store.decision_data()['decisions'])
        entry = self.store.mail_ledger()['events'][reviewed['event_id']]
        self.assertEqual(entry['tracker_id'], 26)
        self.assertEqual(entry['company'], 'RSQUARE')
        self.assertEqual(entry['role'], '회계/자금 담당')
        later = event(n='later', kind='applied', company='RSQUARE', role='회계/자금 담당', thread_id='reviewed-thread', event_at='2026-07-25T01:00:03Z', reason='지원 접수 확인')
        followup = mail_store.ingest(self.store, bundle(later), True)
        self.assertEqual(followup['applied_count'], 0)
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.rows[0]['status'], 'Rejected')
        self.assertEqual(self.store.mail_ledger()['events'][later['event_id']]['tracker_id'], 26)

    def test_reviewed_tracker_link_requires_evidence_and_rejects_conflicting_posting(self):
        tracked = row(1, 'Rejected')
        tracked['notes'] = 'https://www.wanted.co.kr/wd/1'
        self.store.rows = [tracked]
        with self.assertRaises(server.APIError) as missing:
            mail_store.validate_bundle(bundle(event(tracker_id='1', identity_source='operator_reviewed')))
        self.assertEqual(missing.exception.code, 'reviewed_identity_evidence_missing')
        conflicting = event(kind='applied', tracker_id='1', identity_source='operator_reviewed', identity_evidence='대조 근거', job_url='https://www.wanted.co.kr/wd/900')
        with self.assertRaises(server.APIError) as conflict:
            mail_store.ingest(self.store, bundle(conflicting), True)
        self.assertEqual(conflict.exception.code, 'reviewed_identity_conflict')
        self.assertEqual(self.store.rows[0]['status'], 'Rejected')
        self.assertFalse(self.store.decision_data()['decisions'])

    def test_same_thread_new_concrete_posting_never_reuses_old_link(self):
        first = event(kind='proposal_unanswered', job_url='https://www.wanted.co.kr/wd/1')
        mail_store.ingest(self.store, bundle(first), True)
        old = self.store.decision_data()['decisions']['li_1']
        other = event(n='2', kind='declined', job_url='https://www.wanted.co.kr/wd/900', event_at='2026-09-02T01:00:00Z')
        job, key, previous, row = mail_store.resolve_job(self.store, mail_store.validate_bundle(bundle(other))[0], self.store.mail_ledger(), self.store.decision_data(), self.store.tracker_rows(), self.store.export_data()['jobs'])
        self.assertEqual(job['url'], other['job_url'])
        self.assertNotEqual(key, 'li_1')
        self.assertIsNone(previous)
        self.assertIsNone(row)
        self.assertEqual(self.store.decision_data()['decisions']['li_1'], old)
        self.assertNotEqual(mail_store.event_job(first)['id'], mail_store.event_job(other)['id'])

    def test_ambiguous_same_role_postings_are_held_for_review(self):
        jobs = [candidate(1), candidate(2)]
        jobs[1].update(company='회사1', title='회계 팀장')
        self.export(jobs)
        result = mail_store.ingest(self.store, bundle(event(kind='declined')), True)
        self.assertEqual(result['review_count'], 1)
        self.assertEqual(self.store.rows, [])
        self.assertTrue(self.store.dashboard()['pending']['items'][0]['needs_review'])
        self.assertEqual(self.store.dashboard()['stats']['recommended'], 2)

    def test_older_application_stage_does_not_regress_rejection(self):
        self.store.rows = [row(1, 'Rejected')]
        result = mail_store.ingest(self.store, bundle(event(kind='applied')), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(self.store.rows[0]['status'], 'Rejected')

    def test_unanswered_proposal_does_not_reopen_canonical_user_exclusion(self):
        self.store.rows = [row(1, 'SKIP')]
        result = mail_store.ingest(self.store, bundle(event()), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(self.store.dashboard()['pending']['total'], 0)
        self.assertEqual(self.store.dashboard()['excluded']['total'], 1)

    def test_old_mail_cannot_override_newer_canonical_cli_decision(self):
        self.store.rows = [row(1, 'SKIP')]
        self.c.tracker.with_name('status-log.tsv').write_text('1\t2026-09-05\tEvaluated\tSKIP\tset-status\t\n')
        result = mail_store.ingest(self.store, bundle(event(kind='applied')), True)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(result['changes'][0]['outcome'], 'older_than_canonical_transition')
        self.assertEqual(self.store.rows[0]['status'], 'SKIP')

    def test_partial_failure_is_retryable_without_duplicate_canonical_rows(self):
        data = bundle(event(kind='declined'))
        with mock.patch.object(self.store, 'add_canonical_record', side_effect=server.APIError(503, 'tracker_unavailable', 'test')):
            first = mail_store.ingest(self.store, data, True)
        self.assertFalse(first['ok'])
        self.assertEqual(self.store.mail_ledger()['events'][data['events'][0]['event_id']]['outcome'], 'error')
        second = mail_store.ingest(self.store, data, True)
        self.assertTrue(second['ok'])
        self.assertEqual(len(self.store.rows), 1)

    def test_input_requires_real_time_company_role_and_mail_link(self):
        for change in [{'company': ''}, {'role': ''}, {'evidence_text': ''}, {'event_at': 'yesterday'}, {'mail_url': 'https://example.org'}]:
            with self.assertRaises(server.APIError):
                mail_store.ingest(self.store, bundle(event(**change)), True)
        self.assertFalse(self.c.decisions.exists())


class FeatureHTTPTests(unittest.TestCase):
    setUp = test_server.HTTPTests.setUp
    export = test_server.HTTPTests.export
    shutdown = test_server.HTTPTests.shutdown
    request = test_server.HTTPTests.request
    auth = test_server.HTTPTests.auth

    def test_new_routes_require_auth_and_csrf(self):
        for path in ['/api/mail-sync', '/api/jobs/li_1/history']:
            self.assertEqual(self.request('GET', path)[0], 401)
        auth = self.auth()
        no_csrf = {k: v for k, v in auth.items() if k != 'X-CSRF-Token'}
        for method, path, payload in [('POST', '/api/moves', {'id': 'li_1', 'status': 'pending'}), ('POST', '/api/moves/undo', {'token': 'test'}), ('POST', '/api/mail-sync', {}), ('PATCH', '/api/jobs/li_1/note', {'note': 'test'})]:
            self.assertEqual(self.request(method, path, payload, no_csrf)[0], 403)

    def test_move_note_history_and_undo_http_contract(self):
        auth = self.auth()
        job = self.request('GET', '/api/dashboard', headers=auth)[2]['sites'][0]['jobs'][0]
        self.assertTrue(job['movable'])
        result = self.request('POST', '/api/moves', {'id': job['id'], 'status': 'pending', 'expected_updated_at': job['decision_revision']}, auth)
        self.assertEqual(result[0], 200)
        self.assertIn('undo_expires_at', result[2])
        self.assertEqual(result[2]['dashboard']['pending']['total'], 1)
        self.assertEqual(self.request('GET', '/api/jobs/li_1/history', headers=auth)[2]['events'][0]['kind'], 'move')
        self.assertEqual(self.request('POST', '/api/moves/undo', {'token': result[2]['undo_token']}, auth)[0], 200)
        restored = self.request('GET', '/api/dashboard', headers=auth)[2]['sites'][0]['jobs'][0]
        note = self.request('PATCH', '/api/jobs/li_1/note', {'note': '나중에 확인할 메모', 'expected_updated_at': restored['decision_revision']}, auth)
        self.assertEqual(note[0], 200)
        self.assertEqual(self.request('GET', '/api/jobs/li_1/history', headers=auth)[2]['events'][0]['kind'], 'note')

    def test_sync_configuration_missing_is_explicit(self):
        auth = self.auth()
        self.assertFalse(self.request('GET', '/api/mail-sync', headers=auth)[2]['enabled'])
        self.assertEqual(self.request('POST', '/api/mail-sync', {}, auth)[2]['code'], 'mail_sync_unconfigured')


if __name__ == '__main__':
    unittest.main()
