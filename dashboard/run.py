#!/usr/bin/env python3
"""Start the private dashboard using this checkout and local-only settings."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'dashboard/backend'
SETTINGS = ROOT / 'config/dashboard.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--set-password', action='store_true')
    args, remaining = parser.parse_known_args()
    if args.set_password:
        if remaining:
            parser.error('Password values must be entered at the prompt, not in command arguments.')
        first = getpass.getpass('Dashboard password: ')
        if not first or first != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords must be nonempty and match. Settings were not changed.')
        sys.path.insert(0, str(BACKEND))
        from server import atomic_json, hash_password
        settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
        settings['password_hash'] = hash_password(first)
        atomic_json(SETTINGS, settings)
        print('Saved the password hash in ignored local config/dashboard.json.')
        return 0
    env = os.environ.copy()
    if SETTINGS.exists() and not env.get('DASHBOARD_PASSWORD_HASH'):
        env['DASHBOARD_PASSWORD_HASH'] = json.loads(SETTINGS.read_text()).get('password_hash', '')
    if not env.get('DASHBOARD_PASSWORD_HASH') and env.get('DASHBOARD_DEV_NO_AUTH') != '1':
        raise SystemExit('Run npm run dashboard:password before starting the dashboard.')
    engine = Path(env.get('CAREER_OPS_ROOT', ROOT / 'engine')).resolve()
    data = Path(env.get('DASHBOARD_DATA_DIR', engine / 'data')).expanduser().resolve()
    tracker = Path(env.get('CAREER_OPS_TRACKER', data / 'applications.md')).expanduser()
    static = Path(env.get('DASHBOARD_STATIC_DIR', ROOT / 'dashboard/web/dist/client')).resolve()
    if not (static / 'index.html').is_file():
        raise SystemExit('Build the dashboard first: npm run dashboard:install && npm run dashboard:build')
    if not tracker.is_file():
        raise SystemExit('Run npm run setup to create the initial local records.')
    env.setdefault('CAREER_OPS_ROOT', str(engine))
    env.setdefault('DASHBOARD_STATIC_DIR', str(static))
    env.setdefault('DASHBOARD_MAIL_SYNC_COMMAND', json.dumps([
        sys.executable, str(BACKEND / 'mail-sync/worker.py'),
        '--project-root', str(engine), '--data-dir', str(data),
        '--ingest-script', str(BACKEND / 'mail_store.py'), '--apply',
    ]))
    return subprocess.call([sys.executable, str(BACKEND / 'server.py'), *remaining], env=env)


if __name__ == '__main__':
    raise SystemExit(main())
