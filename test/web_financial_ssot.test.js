const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');
const computed = fn => ({ get value() { return fn(); } });

function evaluateComputed(name, nextName, state) {
  const start = html.indexOf(`const ${name} = computed(() => {`);
  const end = html.indexOf(`const ${nextName} = computed`, start);
  assert.ok(start >= 0 && end > start, `${name} source exists`);
  return vm.runInNewContext(`${html.slice(start, end)}; ${name}.value`, { computed, ...state });
}

test('home interest-free cards consume only backend estimates, including unavailable cards', () => {
  const cards = { value: [
    { id: 'feb', bank_name: 'A', last4: '1001', statement_day: 31 },
    { id: 'march', bank_name: 'B', last4: '1002', statement_day: 31 },
    { id: 'unknown', bank_name: 'C', last4: '1003', statement_day: 31, grace_period_days: 99 },
  ] };
  const cardInterestFree = { value: {
    feb: { available: true, mode: 'future_estimate', estimate: { conservative_interest_free_days: 27, theoretical_max_interest_free_days: 28 } },
    march: { available: true, mode: 'future_estimate', estimate: { conservative_interest_free_days: 30, theoretical_max_interest_free_days: 31 } },
    unknown: { available: false, mode: 'future_estimate', reason: 'unresolved_due_date_rule' },
  } };
  const result = evaluateComputed('interestFreeCards', 'interestFreePreview', { cards, cardInterestFree });
  assert.deepEqual(Array.from(result, item => [item.card_id, item.days]), [
    ['march', 30], ['feb', 27], ['unknown', null],
  ]);
  assert.equal(result[0].maxDays, 31);
  assert.equal(result[2].maxDays, null);
  assert.ok(html.includes("item.days === null ? '暂无可靠估算'"));
  assert.ok(!html.includes('card.grace_period_days || 50'));
});

test('repayment chart consumes paid amounts, not statements or mixed currencies', () => {
  const analytics = { value: {
    repayment_trend: [
      { month: '2026-08', credit_card_paid_minor: 5_000, manual_bill_paid_minor: 2_000 },
      { month: '2026-09', credit_card_paid_minor: 0, manual_bill_paid_minor: 0 },
    ],
    monthly_consumption: [
      { month: '2026-08', bank_usage: [], transaction_count: 1, manual_bill_amount_minor: 90_000 },
      { month: '2026-09', bank_usage: [], transaction_count: 1 },
    ],
  } };
  const analyticsSelectedCardId = { value: '' };
  const analyticsIncludeManualBills = { value: true };
  const state = { analytics, analyticsSelectedCardId, analyticsIncludeManualBills };
  const all = evaluateComputed('trendData', 'selectedAnalyticsSpend', state);
  assert.equal(all[0].value, 7_000);
  assert.equal(all[1].value, 0);
  assert.equal(all[1].hasData, false);

  analyticsSelectedCardId.value = 'card-1';
  const card = evaluateComputed('trendData', 'selectedAnalyticsSpend', state);
  assert.equal(card[0].value, 5_000);
  assert.equal(card[0].manualBillValue, 0);
  assert.ok(html.includes('&repayment_card_id='));
  assert.ok(!html.includes('s.total_due_minor || 0'));
});
