const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');
const presets = { '126': 'imap.126.com', '163': 'imap.163.com', qq: 'imap.qq.com' };

test('IMAP provider buttons show the selected host and clear selection for a custom host', () => {
  for (const [preset, host] of Object.entries(presets)) {
    const button = html.match(new RegExp(`<button\\b[^>]*@click="applyImapPreset\\('${preset}'\\)"[^>]*>`, 's'))?.[0];
    assert.ok(button, `${preset} preset button exists`);
    const pressed = button.match(/:aria-pressed="([^"]+)"/)?.[1];
    const classBinding = button.match(/:class="([^"]+)"/)?.[1];
    assert.ok(pressed && classBinding, `${preset} exposes a reactive selected state`);

    for (const currentHost of [...Object.values(presets), 'imap.custom.example']) {
      const scope = { editImap: { host: currentHost } };
      const selected = currentHost === host;
      assert.equal(vm.runInNewContext(pressed, scope), selected);
      assert.equal(vm.runInNewContext(classBinding, scope).includes('border-blue-500'), selected);
    }
  }
});
