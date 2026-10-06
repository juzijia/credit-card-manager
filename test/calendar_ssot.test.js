const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');

test('calendar keeps account-level statement facts and does not mark forecasts paid', () => {
  const start = html.indexOf('const calendarEventsMap = computed(() => {');
  const end = html.indexOf('const activeSelectedDate = computed', start);
  assert.ok(start > 0 && end > start);
  const statements = { value: [{
    id: 'account', bank_code: 'CCB', card_id: null,
    statement_date: '2026-02-15', due_date: '2026-02-28',
    total_due_minor: 5000, remaining_minor: 5000, status: 'issued',
  }] };
  const cards = { value: [
    { id: 'child-a', bank_code: 'CCB', bank_name: '建设银行', last4: '0183', due_day: 5 },
    { id: 'child-b', bank_code: 'CCB', bank_name: '建设银行', last4: '6571', due_day: 5 },
    { id: 'normal', bank_code: 'ZZZ', bank_name: 'Other', last4: '1001', due_day: 6 },
  ] };
  const state = {
    computed: fn => ({ get value() { return fn(); } }),
    calendarYear: { value: 2026 }, calendarMonth: { value: 2 },
    statements, cards,
    calendarEstimatesByMonth: { value: { '2026-02': [{
      card_id: 'normal', statement_date: '2026-02-15', due_date: '2026-02-28',
    }] } },
    manualBillsList: { value: [] }, calendarPeriodMarkedPaidCache: { value: {} },
    manualMarkedPaidRecords: { value: [] },
    getMarkedPaidRecord: () => null,
    getCardArtworkUrl: () => null,
    getBankLogoUrl: () => null,
  };
  const events = vm.runInNewContext(`${html.slice(start, end)}; calendarEventsMap.value`, state);
  const dues = events['2026-02-28'].filter(event => event.type === 'due');
  assert.equal(dues.length, 2);
  const actual = dues.find(event => event.id === 'stmt-due-account');
  assert.equal(actual.amountMinor, 5000);
  assert.equal(actual.isPaid, false);
  assert.match(actual.subtitle, /账户级账单/);
  assert.equal(actual.last4, '');
  const forecast = dues.find(event => event.id.startsWith('card-due-'));
  assert.equal(forecast.amountMinor, null);
  assert.equal(forecast.isPaid, false);
  assert.match(forecast.subtitle, /估计/);
  assert.ok(!Object.values(events).flat().some(event => event.id.includes('child-a') || event.id.includes('child-b')));
  assert.ok(!html.includes('Math.min(c.due_day, daysInCurMonth)'));
});
