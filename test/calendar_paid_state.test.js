const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');

const start = html.indexOf('const calendarEventsMap = computed(() => {');
const end = html.indexOf('const activeSelectedDate = computed', start);
assert.ok(start > 0 && end > start, 'locate calendarEventsMap in web/index.html');

function getEvents(overrides) {
  const state = {
    computed: fn => ({ get value() { return fn(); } }),
    calendarYear: { value: 2026 },
    calendarMonth: { value: 9 },
    statements: { value: [] },
    cards: { value: [] },
    calendarEstimatesByMonth: { value: {} },
    manualBillsList: { value: [] },
    calendarPeriodMarkedPaidCache: { value: {} },
    manualMarkedPaidRecords: { value: [] },
    getMarkedPaidRecord: () => null,
    getCardArtworkUrl: () => null,
    getBankLogoUrl: () => null,
    ...overrides
  };
  return vm.runInNewContext(`${html.slice(start, end)}; calendarEventsMap.value`, state);
}

test('信用卡账单日历已还状态判断：status 为 paid (真实 calendarEventsMap 生产逻辑)', () => {
  const ev = getEvents({
    statements: { value: [{ id: 's1', bank_code: 'BOC', due_date: '2026-09-20', status: 'paid', remaining_minor: 10000 }] }
  });
  const event = ev['2026-09-20']?.find(e => e.id === 'stmt-due-s1');
  assert.ok(event);
  assert.equal(event.isPaid, true);
  assert.equal(event.statusText, '已还清');
});

test('信用卡账单日历已还状态判断：remaining_minor <= 0 但无 marked-paid 记录 (生产逻辑)', () => {
  const ev = getEvents({
    statements: { value: [
      { id: 's2', bank_code: 'BOC', due_date: '2026-09-20', status: 'issued', remaining_minor: 0 },
      { id: 's3', bank_code: 'BOC', due_date: '2026-09-21', status: 'issued', remaining_minor: -50 }
    ] }
  });
  const e2 = ev['2026-09-20']?.find(e => e.id === 'stmt-due-s2');
  const e3 = ev['2026-09-21']?.find(e => e.id === 'stmt-due-s3');
  assert.ok(e2 && e3);
  assert.equal(e2.isPaid, true);
  assert.equal(e3.isPaid, true);
});

test('信用卡账单日历已还状态判断：存在 marked-paid 兜底记录 (生产逻辑)', () => {
  const ev = getEvents({
    statements: { value: [{ id: 's4', bank_code: 'BOC', due_date: '2026-09-20', status: 'issued', remaining_minor: 5000 }] },
    getMarkedPaidRecord: id => (id === 's4' ? { id: 99, statement_id: 's4' } : null)
  });
  const event = ev['2026-09-20']?.find(e => e.id === 'stmt-due-s4');
  assert.ok(event);
  assert.equal(event.isPaid, true);
});

test('信用卡账单日历已还状态判断：未结清且无 marked 记录 (生产逻辑)', () => {
  const ev = getEvents({
    statements: { value: [{ id: 's5', bank_code: 'BOC', due_date: '2026-09-20', status: 'issued', remaining_minor: 5000 }] }
  });
  const event = ev['2026-09-20']?.find(e => e.id === 'stmt-due-s5');
  assert.ok(event);
  assert.equal(event.isPaid, false);
  assert.equal(event.statusText, '待还款');
});

test('手动账单跨月份 paid 状态隔离：2026-08 已结清而 2026-09 未结清不串状态 (生产逻辑)', () => {
  const bills = [{ id: 101, name: '宽带', bill_type: '宽带', due_day: 15, enabled: true }];
  const cache = {
    '2026-08': [{ bill_id: 101, payment_id: 1001, period: '2026-08' }],
    '2026-09': []
  };

  const evAug = getEvents({
    calendarYear: { value: 2026 },
    calendarMonth: { value: 8 },
    manualBillsList: { value: bills },
    calendarPeriodMarkedPaidCache: { value: cache }
  });
  assert.equal(evAug['2026-08-15']?.find(e => e.id === 'manual-due-101')?.isPaid, true);

  const evSep = getEvents({
    calendarYear: { value: 2026 },
    calendarMonth: { value: 9 },
    manualBillsList: { value: bills },
    calendarPeriodMarkedPaidCache: { value: cache }
  });
  assert.equal(evSep['2026-09-15']?.find(e => e.id === 'manual-due-101')?.isPaid, false);
});
