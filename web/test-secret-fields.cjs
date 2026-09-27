const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');

const html = readFileSync(`${__dirname}/index.html`, 'utf8');
const fieldSource = html.slice(html.indexOf('const SecretField ='), html.indexOf('function useSavedSecrets()'));
const SecretField = new Function('ref', `${fieldSource}; return SecretField;`)(value => ({ value }));
const source = html.slice(html.indexOf('function useSavedSecrets()'), html.indexOf('const IconDashboard'));
const useSavedSecrets = new Function('ref', `${source}; return useSavedSecrets;`)(value => ({ value }));

test('all 14 credential slots share the eye toggle', async () => {
  assert.equal((html.match(/<secret-field\b/g) || []).length, 14);
  let calls = 0;
  const field = SecretField.setup({ modelValue: '', reveal: async () => { calls++; return true; } });
  await field.toggle();
  assert.equal(field.visible.value, true);
  await field.toggle();
  assert.equal(field.visible.value, false);
  assert.equal(calls, 1);
});

test('saved secrets change only after successful save, including replacement and deletion', () => {
  const fields = useSavedSecrets();
  fields.load({ password: 'old' }, ['password']);
  assert.deepEqual(fields.changes({ password: 'old' }), {});
  assert.deepEqual(fields.changes({ password: '' }), { password: '' });
  assert.deepEqual(fields.configured.value, ['password']);
  fields.commit({ password: '' });
  assert.deepEqual(fields.configured.value, []);
  fields.commit({ password: 'new' });
  assert.deepEqual(fields.configured.value, ['password']);
  assert.deepEqual(fields.changes({ password: 'new' }), {});
});

test('imap password field binds reveal callback', () => {
  assert.ok(html.includes(':reveal="revealImapPassword"'));
});
