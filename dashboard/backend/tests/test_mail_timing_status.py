"""Read-only duration APIs keep fresh and resumed run measurements separate."""
import datetime as dt
import fcntl
import json
import unittest
from unittest import mock

import server
import test_server


def iso(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


class MailTimingStatusTests(unittest.TestCase):
    setUp = test_server.StoreTests.setUp
    export = test_server.StoreTests.export

    def test_running_elapsed_advances_from_runtime_anchor_without_writing_files(self):
        server.atomic_json(self.c.data / 'mail-sync-state.json', {'status': 'running', 'started_at': iso(997), 'phase_started_at': iso(999), 'timing_updated_at': iso(1000), 'elapsed_seconds': 3, 'phase_elapsed_seconds': 1, 'duration_seconds': None, 'resumed': True})
        before = (self.c.data / 'mail-sync-state.json').read_bytes()
        with (self.c.data / '.mail-sync.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch('server.time.time', return_value=1010):
                result = self.store.mail_sync_status()
        self.assertEqual(result['elapsed_seconds'], 13)
        self.assertEqual(result['phase_elapsed_seconds'], 11)
        self.assertIsNone(result['duration_seconds'])
        self.assertTrue(result['resumed'])
        self.assertEqual(before, (self.c.data / 'mail-sync-state.json').read_bytes())

    def test_finished_or_interrupted_elapsed_does_not_keep_growing(self):
        for status in ('complete', 'running'):
            with self.subTest(status=status):
                state = {'status': status, 'started_at': iso(997), 'timing_updated_at': iso(1000), 'updated_at': iso(1000), 'elapsed_seconds': 3, 'phase_elapsed_seconds': 1}
                if status == 'complete': state.update(completed_at=iso(1000), duration_seconds=3)
                server.atomic_json(self.c.data / 'mail-sync-state.json', state)
                with mock.patch('server.time.time', return_value=5000):
                    result = self.store.mail_sync_status()
                self.assertEqual(result['elapsed_seconds'], 3)
                self.assertEqual(result['phase_elapsed_seconds'], 1)
                self.assertEqual(result['duration_seconds'], 3 if status == 'complete' else None)

    def test_last_full_run_excludes_resume_unknown_preview_and_failed(self):
        history = self.c.data / 'mail-sync-history.jsonl'
        entries = [
            {'run_id': 'full', 'status': 'complete', 'resumed': False, 'run_mode': 'fresh', 'duration_seconds': 900, 'total_threads': 119, 'completed_at': iso(1000)},
            {'run_id': 'resume', 'status': 'complete', 'resumed': True, 'run_mode': 'resume', 'duration_seconds': 24},
            {'run_id': 'unknown', 'status': 'complete', 'duration_seconds': 20},
            {'run_id': 'preview', 'status': 'preview', 'resumed': False, 'run_mode': 'fresh', 'duration_seconds': 7},
            {'run_id': 'failed', 'status': 'failed', 'resumed': False, 'run_mode': 'fresh', 'duration_seconds': 6},
        ]
        history.write_text(''.join(json.dumps(row) + '\n' for row in entries) + '{interrupted append')
        result = self.store.mail_sync_status()
        self.assertEqual(result['last_full_run']['run_id'], 'full')
        self.assertEqual(result['last_full_run']['duration_seconds'], 900)
        self.assertEqual(result['last_full_run']['total_threads'], 119)
        self.assertEqual(result['last_resumed_run']['duration_seconds'], 24)
        self.assertEqual(result['last_run']['run_id'], 'unknown')

    def test_unknown_history_has_no_last_full_duration_and_cache_refreshes(self):
        history = self.c.data / 'mail-sync-history.jsonl'
        history.write_text(json.dumps({'status': 'complete', 'duration_seconds': 24}) + '\n')
        self.assertIsNone(self.store.mail_sync_status()['last_full_run'])
        with history.open('a') as stream:
            stream.write(json.dumps({'status': 'complete', 'duration_seconds': 601, 'resumed': False, 'run_mode': 'fresh'}) + '\n')
        self.assertEqual(self.store.mail_sync_status()['last_full_run']['duration_seconds'], 601)

    def test_legacy_timestamps_measure_only_known_interval_without_guessing_resume(self):
        server.atomic_json(self.c.data / 'mail-sync-state.json', {'status': 'complete', 'started_at': iso(1000), 'completed_at': iso(1024)})
        result = self.store.mail_sync_status()
        self.assertEqual(result['duration_seconds'], 24)
        self.assertIsNone(result['resumed'])
        self.assertIsNone(result['last_full_run'])


if __name__ == '__main__': unittest.main()
