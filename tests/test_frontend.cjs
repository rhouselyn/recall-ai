// Run with node tests/test_frontend.cjs; no browser or packages required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function frontend() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', innerHTML: '', disabled: false, files: [],
      options: [], add(option) { this.options.push(option); }, addEventListener() {}, replaceChildren() {}, insertAdjacentHTML() {},
      showModal() { this.open = true; }, close() { this.open = false; },
      querySelector() { return element('dialog-error'); },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element, addEventListener() {} },
    Option: function (label, value) { this.label = label; this.value = value; }, URLSearchParams, setTimeout, clearTimeout, setInterval() {},
    FileReader: class { readAsDataURL() { this.result = 'data:text/csv;base64,ZmFrZQ=='; this.onload(); } },
    fetch(url) { return new Promise((resolve, reject) => requests.push({ url, resolve, reject })); },
  });
  vm.runInContext(read('frontend/app.js'), context);
  const api = vm.runInContext('({renderLibrary, showImport, showAccount, openLoginPages})', context);
  const reply = (request, title, cursor = null) => request.resolve({ ok: true, json: async () => ({
    items: [{ item_id: title, title, platform: 'web', sources: [] }], next_cursor: cursor,
  }) });
  element('library-query').value = 'old';
  const old = api.renderLibrary();
  element('library-query').value = 'new';
  const current = api.renderLibrary();
  reply(requests[2], 'new'); await current;
  reply(requests[1], 'old'); await old;
  assert.match(element('library-list').innerHTML, />new</);
  assert.doesNotMatch(element('library-list').innerHTML, />old</);

  const first = api.renderLibrary(); reply(requests[3], 'first', 'cursor'); await first;
  const more = api.renderLibrary(true);
  requests[4].resolve({ ok: false, json: async () => ({error: {code: 'STALE_CURSOR'}}) });
  await tick();
  assert.ok(!requests[5].url.includes('cursor='));
  reply(requests[5], 'fresh'); await more;
  assert.match(element('library-list').innerHTML, />fresh</);

  api.showImport();
  element('import-input').files = [{size: 4}];
  const button = element('do-import');
  const event = {currentTarget: button};
  const importing = button.onclick(event);
  event.currentTarget = null; // DOM clears currentTarget when dispatch finishes.
  await tick();
  requests[6].reject(new Error('Import failed'));
  await importing;
  assert.equal(button.disabled, false);
  assert.equal(element('dialog-error').textContent, 'Import failed');

  api.showAccount('douyin');
  assert.match(element('dialog-content').innerHTML, /打开登录页/);
  assert.doesNotMatch(element('dialog-content').innerHTML, /account-user|account-collections/);
  api.showAccount('zhihu');
  assert.match(element('dialog-content').innerHTML, /指定知乎收藏夹（可选）/);
  api.showAccount('github');
  assert.match(element('dialog-content').innerHTML, /Starring/);
  api.showAccount('chrome');
  assert.match(element('dialog-content').innerHTML, /启用本机自动扫描/);
  const bulkButton = element('login-pending');
  const bulk = api.openLoginPages(bulkButton);
  assert.equal(bulkButton.disabled, true);
  assert.equal(requests[7].url, '/api/connect/pending');
  requests[7].resolve({ok: true, json: async () => ({jobs: {}, errors: {zhihu: '无法启动'}})});
  await tick();
  requests[8].resolve({ok: true, json: async () => ({sources: []})});
  await bulk;
  assert.equal(bulkButton.disabled, false);
  assert.match(element('login-progress').textContent, /知乎：无法启动/);
  for (const id of ['chrome', 'edge']) {
    assert.ok(element('library-source').options.some(option => option.value === id));
    element('library-source').value = id;
    const filtered = api.renderLibrary();
    assert.equal(new URL(requests.at(-1).url, 'http://localhost').searchParams.get('browser'), id);
    reply(requests.at(-1), id);
    await filtered;
  }
}

async function extension() {
  const state = {sourceId: 'chrome-live', profileUuid: 'profile', version: 0, pending: false};
  const sends = [];
  const timers = [];
  const listener = () => ({addListener() {}});
  const context = vm.createContext({
    console, navigator: {userAgent: 'Chrome'}, crypto: {randomUUID: () => 'profile'},
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
    chrome: {
      storage: {local: {get: async () => ({...state}), set: async values => Object.assign(state, values)}},
      bookmarks: {getTree: async () => [{id: '0', title: '', children: []}],
        ...Object.fromEntries(['onCreated','onRemoved','onChanged','onMoved','onChildrenReordered','onImportEnded'].map(k => [k, listener()]))},
      alarms: {onAlarm: listener()},
      runtime: {onInstalled: listener(), onStartup: listener(), onMessage: listener(),
        sendNativeMessage: (_host, message) => new Promise(resolve => sends.push({message, resolve}))},
    },
  });
  vm.runInContext(read('extensions/bookmarks/service_worker.js'), context);
  const api = vm.runInContext('({sendTree})', context);
  const sending = api.sendTree(); await tick();
  await api.sendTree(); // A second bookmark event arrives during native I/O.
  assert.equal(sends[0].message.accept_empty, true);
  sends[0].resolve({ok: true, version: 1}); await sending;
  assert.equal(timers.length, 1);
  timers[0](); await tick();
  assert.equal(sends.length, 2);
  assert.equal(sends[1].message.version, 2);
  sends[1].resolve({ok: true, version: 2}); await tick();
  assert.equal(state.pending, false);
}

(async () => {
  await frontend(); await extension();
  console.log('Frontend race, stale cursor, retry button and bookmark event regressions passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
