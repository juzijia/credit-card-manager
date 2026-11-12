const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');

function setup(handler) {
  const calls = [];
  const context = {
    uploadStatus: { value: '' }, uploadError: { value: false },
    uploadBusy: { value: false }, pendingImport: { value: null },
    api: async (path, options) => { calls.push({ path, options }); return handler(calls.length); },
    showToast() {}, loadAll: async () => {}, encodeURIComponent,
    FormData: class { constructor() { this.items = []; } append(key, value) { this.items.push([key, value]); } },
  };
  const start = html.indexOf('function formatAmount(minor) {');
  const end = html.indexOf('function handleFileSelect(', start);
  vm.runInNewContext(html.slice(start, end), context);
  return { ...context, calls };
}
function conflict(code = 'IMPORT_CONFIRMATION_REQUIRED', differences = []) {
  return Object.assign(new Error('账单有变化'), { status: 409, detail: {
    code, message: '账单有变化', confirmation_token: code === 'IMPORT_CONFIRMATION_REQUIRED' ? 'a'.repeat(64) : null,
    differences,
  }});
}
const file = { name: 'bill.eml' };
const diff = { bank_code: 'CMB', card_last4: '1234', currency: 'CNY',
  old: { total_due_minor: 10000, minimum_due_minor: null, due_date: '2026-09-20' },
  new: { total_due_minor: 15000, minimum_due_minor: 0, due_date: '2026-09-21' },
  transactions_added: [{ transaction_date: '2026-08-10', posting_date: '2026-08-11',
    amount_minor: 5000, currency: 'CNY', raw_description: '新消费', card_last4: '1234' }],
};

test('api preserves structured conflict detail and string error messages', async () => {
  const detail = conflict().detail;
  const context = { csrfToken: { value: '' }, needLogin: { value: false },
    fetch: async () => ({ ok: false, status: 409, json: async () => ({ detail }) }) };
  const start = html.indexOf('async function api(');
  const end = html.indexOf('// Card Dictionary', start);
  vm.runInNewContext(html.slice(start, end), context);
  await assert.rejects(context.api('/imports/eml'), error => error.status === 409 && error.detail === detail && error.message === detail.message);
  context.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: 'invalid file' }) });
  await assert.rejects(context.api('/imports/eml'), /invalid file/);
});

test('preview waits for explicit confirmation and resends the same file and token', async () => {
  const ctx = setup(n => { if (n === 1) throw conflict(undefined, [diff]); return { corrected: true }; });
  await ctx.uploadFile(file);
  assert.equal(ctx.calls.length, 1);
  assert.equal(ctx.pendingImport.value.file, file);
  for (const text of ['100.00', '150.00', '新消费', '尾号 1234', '入账 2026-08-11', '未知/未提供 -> 0.00']) {
    assert.ok(ctx.uploadStatus.value.includes(text), text);
  }
  await ctx.confirmImportCorrection();
  assert.equal(ctx.calls.length, 2);
  assert.ok(ctx.calls[1].path.includes('confirmation_token=' + 'a'.repeat(64)));
  assert.equal(ctx.calls[1].options.body.items[0][1], file);
  assert.equal(ctx.pendingImport.value, null);
  assert.ok(ctx.uploadStatus.value.includes('更正成功'));
});

test('cancelled preview never sends an update', async () => {
  const ctx = setup(() => { throw conflict(undefined, [diff]); });
  await ctx.uploadFile(file);
  ctx.cancelImportCorrection();
  await ctx.confirmImportCorrection();
  assert.equal(ctx.calls.length, 1);
  assert.equal(ctx.pendingImport.value, null);
  assert.ok(ctx.uploadStatus.value.includes('已取消'));
});

test('large differences show every transaction and currency without truncation', async () => {
  const added = Array.from({ length: 100 }, (_, i) => ({ ...diff.transactions_added[0], raw_description: `消费 ${i}` }));
  const ctx = setup(() => { throw conflict(undefined, [{ ...diff, transactions_added: added }]); });
  await ctx.uploadFile(file);
  for (let i = 0; i < 100; i++) assert.ok(ctx.uploadStatus.value.includes(`消费 ${i}`));
  assert.ok(ctx.uploadStatus.value.includes('CNY 50.00'));
});

test('stale confirmation clears pending state and requires a fresh upload', async () => {
  const ctx = setup(n => { throw conflict(n === 1 ? 'IMPORT_CONFIRMATION_REQUIRED' : 'IMPORT_CONFIRMATION_STALE'); });
  await ctx.uploadFile(file);
  await ctx.confirmImportCorrection();
  await ctx.confirmImportCorrection();
  assert.equal(ctx.calls.length, 2);
  assert.equal(ctx.pendingImport.value, null);
  assert.equal(ctx.uploadError.value, true);
  assert.ok(ctx.uploadStatus.value.includes('重新上传'));
});

test('blocked updates show all reasons and cannot be confirmed', async () => {
  const ctx = setup(() => { throw conflict('IMPORT_CORRECTION_BLOCKED', [
    { ...diff, blocked_reason: '人工分类' }, { blocked_reason: '退款关联' },
  ]); });
  await ctx.uploadFile(file);
  await ctx.confirmImportCorrection();
  assert.equal(ctx.calls.length, 1);
  assert.equal(ctx.pendingImport.value, null);
  for (const text of ['人工分类', '退款关联', '新消费']) assert.ok(ctx.uploadStatus.value.includes(text));
});

test('overlapping uploads do not replace the pending source', async () => {
  let release;
  const ctx = setup(() => new Promise(resolve => { release = resolve; }));
  const first = ctx.uploadFile(file);
  await ctx.uploadFile({ name: 'other.pdf' });
  assert.equal(ctx.calls.length, 1);
  release({ deduplicated: true });
  await first;
  assert.equal(ctx.uploadBusy.value, false);
});
