"""Mail sync status uses synthetic local state and temporary advisory locks."""
import fcntl
import json
import unittest
from unittest import mock

import server
import test_server


class MailSyncStatusTests(unittest.TestCase):
    setUp = test_server.StoreTests.setUp
    export = test_server.StoreTests.export

    def save(self, **state):
        server.atomic_json(self.c.data / 'mail-sync-state.json', state)

    def test_unlocked_running_state_is_interrupted_after_server_restart(self):
        self.save(status='running', processed_threads=110, total_threads=119, remaining_known_threads=9, phase_detail='read_known_threads')
        result = self.store.mail_sync_status()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['is_running'])
        self.assertEqual(result['error_code'], 'sync_interrupted')
        self.assertEqual(result['processed_threads'], 110)
        self.assertEqual(result['remaining_known_threads'], 9)
        self.assertEqual(json.loads((self.c.data / 'mail-sync-state.json').read_text())['status'], 'running')
        self.assertFalse((self.c.data / '.mail-sync.lock').exists())

    def test_shared_lock_marks_external_worker_running_despite_old_failed_state(self):
        self.save(status='failed', error='private old error', error_code='mail_sync_failed')
        self.store._mail_process = mock.Mock()
        self.store._mail_process.poll.return_value = 1
        with (self.c.data / '.mail-sync.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.store.mail_sync_status()
            self.assertEqual(result['status'], 'running')
            self.assertTrue(result['is_running'])
            self.assertIsNone(result['error'])
            self.assertEqual(result['phase_detail'], 'starting')
            with mock.patch('server.subprocess.Popen') as spawn:
                response = self.store.start_mail_sync()
                self.assertFalse(response['accepted'])
                spawn.assert_not_called()

    def test_legacy_failed_checkpoint_exposes_counts_and_sanitizes_diagnostic(self):
        self.save(status='failed', run_id='legacy', phase='read', processed_threads=0, error='Expected exactly one verified connector response: private@example.test /private/log')
        server.atomic_json(self.c.data / 'mail-sync-checkpoint.json', {'version': 1, 'run_id': 'legacy', 'window_start': '2026-09-01T00:00:00Z', 'window_end': '2026-09-08T00:00:00Z', 'windows': [{'done': True}], 'phase': 'read', 'message_ids': ['m1'], 'read_message_ids': ['m1', 'extra'], 'read_thread_ids': [f't{i}' for i in range(110)], 'known_thread_ids': [f't{i}' for i in range(119)]})
        result = self.store.mail_sync_status()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'connector_response_invalid')
        self.assertEqual((result['processed_threads'], result['total_threads'], result['remaining_known_threads']), (110, 119, 9))
        self.assertEqual(result['phase_detail'], 'read_known_threads')
        self.assertEqual(result['phase_label'], '기존 지원 대화 재확인')
        self.assertEqual(result['window_start'], '2026-09-01T00:00:00Z')
        self.assertEqual(result['progress']['read_target_messages'], 1)
        public = json.dumps(result)
        for private in ['private@example.test', '/private/log', 'verified connector response']:
            self.assertNotIn(private, public)

    def test_private_diagnostic_and_unknown_error_code_never_pass_through(self):
        self.save(status='failed', error='raw mail body', error_diagnostic='raw mail body', error_code='raw-mail-body', arbitrary_secret='secret')
        result = self.store.mail_sync_status()
        self.assertEqual(result['error_code'], 'mail_body_unreadable')
        self.assertNotIn('raw mail body', json.dumps(result))
        self.assertNotIn('error_diagnostic', result)
        self.assertNotIn('arbitrary_secret', result)

    def test_launch_grace_is_queued_then_unlocked_process_is_interrupted(self):
        self.store._mail_process = mock.Mock()
        self.store._mail_process.poll.return_value = None
        self.store._mail_launch_at = 100
        with mock.patch('server.time.monotonic', return_value=105):
            result = self.store.mail_sync_status()
            self.assertEqual(result['status'], 'queued')
            self.assertFalse(result['is_running'])
        with mock.patch('server.time.monotonic', return_value=111):
            self.assertEqual(self.store.mail_sync_status()['error_code'], 'sync_interrupted')

    def test_complete_state_stays_complete_and_read_does_not_create_lock(self):
        self.save(status='complete', processed_threads=119, total_threads=119, phase_detail='complete', phase_label='동기화 완료', error='obsolete error')
        before = {path.name: path.read_bytes() for path in self.c.data.iterdir() if path.is_file()}
        result = self.store.mail_sync_status()
        self.assertEqual(result['status'], 'complete')
        self.assertIsNone(result['error'])
        self.assertFalse(result['is_running'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.c.data.iterdir() if path.is_file()})

    def test_different_run_checkpoint_never_overrides_current_progress(self):
        self.save(status='failed', run_id='current', processed_threads=3)
        server.atomic_json(self.c.data / 'mail-sync-checkpoint.json', {'version': 1, 'run_id': 'old', 'read_thread_ids': ['a'] * 10})
        self.assertEqual(self.store.mail_sync_status()['processed_threads'], 3)


if __name__ == '__main__': unittest.main()
