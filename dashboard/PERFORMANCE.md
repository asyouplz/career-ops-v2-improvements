# Dashboard response time

The dashboard shares immutable URL/alias transformations through bounded caches.
Each snapshot builds its own tracker, decision, and current-export lookup indexes;
mutable state is never retained between snapshots. Posting conflicts, ambiguous
matches, canonical revision checks, and evidence expiry retain their original rules.

A move and its undo build at most two complete views, before and after the write.
The canonical writer, backups, atomic JSON writes, fsync, and recovery journals are
preserved. The committed movement is recorded before the final view is built, and
the undo deadline is created after that view, so rendering does not consume it.

The client shares in-flight reads of the same revision, drains older reads before
a newer read, and ignores stale results after a mutation. Focus and visibility
refreshes use a 15-second freshness window; hidden pages skip periodic refreshes.
Writes remain serialized while tabs, filters, search, and scrolling stay available.

Only hashed assets under `/_next/static/` receive an immutable one-year cache.
HTML uses `no-cache`; private API responses continue to use `no-store`.

## Validation

```sh
npm ci --ignore-scripts
python3 dashboard/backend/tests/run_checks.py
npm --prefix dashboard/web ci --ignore-scripts
npm --prefix dashboard/web run typecheck
npm --prefix dashboard/web run lint
node --experimental-strip-types --test dashboard/web/tests/refresh-queue.test.mjs
npm --prefix dashboard/web run build
```

Backend tests use generated temporary data and local HTTP listeners. They include
the actual bundled canonical CLI as well as concurrent changes, mail/manual
conflicts, recommendation retention, evidence expiry, auth/CSRF, journal failures,
snapshot counts, static cache boundaries, and undo timing. No production records
or credentials are needed.

## Measurements and operational checks

`career_request` logs include only normalized route names, method, status, total
time, snapshot count/time, lock wait, atomic write count/time, and canonical CLI
time. They omit job IDs, query values, request bodies, company/mail text, and all
tokens. `Server-Timing` reports the measured components for network inspection.

The initial isolated comparison with 954 candidates and 70 tracker rows measured
dashboard calculation at 4.26s before / 0.12s after (median), and one unlinked
exclusion at 21.27s before / 0.30s after. The complete dashboard output matched
before and after the move, apart from response time fields. These measurements
exclude network and browser work; linked canonical mutations have additional
CLI cost. Measure those separately before setting an end-to-end latency target.

No completed-dashboard cache or lock removal was introduced. A future view cache
must account for external writers, collection changes, evidence expiry, and mail
progress. The current calculation is already below the initial one-second target.
