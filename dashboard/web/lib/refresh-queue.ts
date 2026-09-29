/** Share reads of the same revision and drain obsolete reads before a new one. */
export function createRefreshQueue<T>() {
  let tail: Promise<unknown> = Promise.resolve();
  const pending = new Map<number, Promise<T>>();
  return {
    run(revision: number, read: () => Promise<T>): Promise<T> {
      const existing = pending.get(revision);
      if (existing) return existing;
      const request = tail.catch(() => {}).then(read);
      pending.set(revision, request);
      tail = request;
      void request
        .finally(() => {
          if (pending.get(revision) === request) pending.delete(revision);
        })
        .catch(() => {});
      return request;
    },
  };
}
