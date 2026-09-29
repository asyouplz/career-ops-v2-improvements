import assert from 'node:assert/strict';
import test from 'node:test';
import { createRefreshQueue } from '../lib/refresh-queue.ts';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('focus, interval, and refresh share one in-flight read', async () => {
  const queue = createRefreshQueue();
  const gate = deferred();
  let calls = 0;
  const read = () => { calls++; return gate.promise; };
  const first = queue.run(1, read);
  const second = queue.run(1, read);
  assert.equal(first, second);
  gate.resolve('current');
  assert.deepEqual(await Promise.all([first, second]), ['current', 'current']);
  assert.equal(calls, 1);
});

test('a post-mutation read waits for the obsolete read without sharing its result', async () => {
  const queue = createRefreshQueue();
  const gate = deferred();
  const order = [];
  const old = queue.run(1, async () => { order.push('old'); return gate.promise; });
  const fresh = queue.run(2, async () => { order.push('fresh'); return 'new state'; });
  const same = queue.run(2, async () => { throw Error('duplicate request'); });
  await new Promise(setImmediate);
  assert.deepEqual(order, ['old']);
  gate.resolve('old state');
  assert.equal(await old, 'old state');
  assert.equal(await fresh, 'new state');
  assert.equal(await same, 'new state');
  assert.deepEqual(order, ['old', 'fresh']);
});

test('a failed request does not prevent a later refresh or leak a rejection', async () => {
  const queue = createRefreshQueue();
  await assert.rejects(queue.run(1, async () => { throw Error('synthetic'); }));
  assert.equal(await queue.run(1, async () => 'recovered'), 'recovered');
});
