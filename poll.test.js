const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(new URL('./poll.js', `file://${__filename}`), 'utf8');
function harness(fetchImpl) {
  const current = {};
  current.main = { id: 'old-main', replaceWith(node) { current.main = node; } };
  current.foot = { id: 'old-foot', replaceWith(node) { current.foot = node; } };
  const timers = [];
  const timeouts = [];
  const warnings = [];
  const document = { querySelector(selector) { return selector === 'main' ? current.main : current.foot; } };
  const window = {
    setInterval(fn, ms) { timers.push({ fn, ms }); },
    setTimeout(fn, ms) { const timeout = { fn, ms, active: true }; timeouts.push(timeout); return timeout; },
    clearTimeout(timeout) { timeout.active = false; },
  };
  class DOMParser {
    parseFromString(html) {
      if (!html.includes('new-main')) throw new Error('bad page');
      return { querySelector(selector) { return selector === 'main' ? { id: 'new-main' } : { id: 'new-foot' }; } };
    }
  }
  const context = { fetch: fetchImpl, document, window, DOMParser, AbortController, console: { warn(...args) { warnings.push(args); } } };
  vm.runInNewContext(source, context);
  return { current, timers, timeouts, warnings };
}
const ok = (text = 'new-main') => ({ ok: true, text: async () => text });

test('starts immediately without cache and schedules 60000ms refresh that updates data', async () => {
  let calls = 0;
  const h = harness(async (url, options) => {
    calls++;
    assert.equal(url, '/');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.headers.Accept, 'text/html');
    assert.ok(options.signal instanceof AbortSignal);
    return ok();
  });
  assert.equal(calls, 1);
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, 60000);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.current.main.id, 'new-main');
  assert.equal(h.current.foot.id, 'new-foot');
  assert.equal(h.timeouts[0].active, false);
  await h.timers[0].fn();
  assert.equal(calls, 2);
});

test('retains old page on failure and retries successfully on next timer', async () => {
  let calls = 0;
  const h = harness(async () => { calls++; return calls === 1 ? { ok: false, status: 503 } : ok(); });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.current.main.id, 'old-main');
  assert.equal(h.warnings.length, 1);
  await h.timers[0].fn();
  assert.equal(calls, 2);
  assert.equal(h.current.main.id, 'new-main');
});

test('does not overlap requests while a refresh is pending', async () => {
  let resolveFirst;
  let calls = 0;
  const h = harness(() => { calls++; return new Promise(resolve => { resolveFirst = resolve; }); });
  assert.equal(calls, 1);
  await h.timers[0].fn();
  assert.equal(calls, 1);
  resolveFirst(ok());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.current.main.id, 'new-main');
});

test('aborts a stalled request after 15000ms and recovers on the next cycle', async () => {
  let calls = 0;
  const h = harness((_url, { signal }) => {
    calls++;
    if (calls > 1) return Promise.resolve(ok());
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
  });
  assert.equal(h.timeouts[0].ms, 15000);
  h.timeouts[0].fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.current.main.id, 'old-main');
  assert.equal(h.warnings.length, 1);
  await h.timers[0].fn();
  assert.equal(calls, 2);
  assert.equal(h.current.main.id, 'new-main');
});
