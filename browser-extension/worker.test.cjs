const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

async function fixture({win = {}, tab = {}, settings = {}} = {}) {
  const sent = [];
  const event = () => ({addListener() {}});
  const context = vm.createContext({
    navigator: {userAgent: 'Edg/140'}, URL, Date, AbortSignal,
    setInterval() {},
    fetch: async (url, request) => {sent.push(JSON.parse(request.body)); return {ok: true};},
    chrome: {
      storage: {local: {get: async () => ({token: 'test', enabled: true, ...settings}), set: async () => {}}, onChanged: event()},
      windows: {getLastFocused: async () => ({id: 1, focused: true, incognito: false, ...win}), onFocusChanged: event()},
      tabs: {query: async () => [{active: true, status: 'complete', incognito: false, title: 'Article',
        url: 'https://user:password@example.com/a?secret=1#private', ...tab}], onActivated: event(), onUpdated: event(), onRemoved: event()},
      runtime: {onInstalled: event(), onStartup: event(), openOptionsPage() {}},
      alarms: {create() {}, onAlarm: event()}, action: {onClicked: event()},
    },
  });
  vm.runInContext(fs.readFileSync(__dirname + '/worker.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return sent;
}

test('normal foreground URL is sanitized before sending', async () => {
  const [payload] = await fixture();
  assert.equal(payload.url, 'https://example.com/a');
  assert.equal(payload.browser, 'edge');
  assert.equal(payload.incognito, false);
});

test('private, background, loading, excluded and disabled pages send no content', async () => {
  for (const config of [
    {win: {incognito: true}}, {win: {focused: false}}, {tab: {incognito: true}},
    {tab: {status: 'loading'}}, {tab: {url: 'file:///private'}},
    {settings: {excludedDomains: ['example.com']}}, {settings: {enabled: false}},
  ]) {
    const [payload] = await fixture(config);
    assert.equal(payload.focused, false);
    assert.equal(payload.url, undefined);
    assert.equal(payload.title, undefined);
  }
});

test('not paired means no network call', async () => {
  assert.deepEqual(await fixture({settings: {token: ''}}), []);
});
