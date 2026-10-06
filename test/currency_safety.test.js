const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const HTML_PATH = path.resolve(__dirname, '../web/index.html');
const htmlContent = fs.readFileSync(HTML_PATH, 'utf-8');

// ============================================================================
// 1. Static contract tests on web/index.html source
// ============================================================================

test('模板契约：总待还卡片使用按币种分组的展示，而非混币标量', () => {
  assert.ok(
    htmlContent.includes('totalOutstandingDisplay'),
    '必须存在 totalOutstandingDisplay 分组展示计算属性'
  );
  assert.ok(
    !htmlContent.includes("formatMoney(totalOutstandingMinor) : '••••••'"),
    '总待还不得再直接渲染混币标量 totalOutstandingMinor'
  );
});

test('模板契约：七日待还卡片使用按币种分组的展示', () => {
  assert.ok(
    htmlContent.includes('sevenDaysDueDisplay'),
    '必须存在 sevenDaysDueDisplay 分组展示计算属性'
  );
  assert.ok(
    !htmlContent.includes("formatMoney(sevenDaysDueTotalMinor) : '••••••'"),
    '七日待还不得再直接渲染混币标量 sevenDaysDueTotalMinor'
  );
});

test('源码契约：不得存在 CNY + USD 混币求和表达式', () => {
  const start = htmlContent.indexOf('const totalOutstandingMinor = computed(() => {');
  assert.ok(start > 0, '未找到 totalOutstandingMinor 定义');
  const end = htmlContent.indexOf('});', start);
  const source = htmlContent.slice(start, end);
  assert.ok(
    source.includes('codes.length === 1'),
    'totalOutstandingMinor 仅在单币种时返回标量'
  );
  assert.ok(
    source.includes('return null'),
    '多币种时 totalOutstandingMinor 必须为 null（不得当 0）'
  );
});

test('源码契约：七日待还按 statement currency 分组，manual 计入 CNY', () => {
  const start = htmlContent.indexOf('const sevenDaysDueByCurrency = computed(() => {');
  assert.ok(start > 0, '未找到 sevenDaysDueByCurrency 定义');
  const end = htmlContent.indexOf('});', start);
  const source = htmlContent.slice(start, end);
  assert.ok(
    source.includes("c.currency || 'CNY'"),
    '信用卡待还必须按 statement currency 分桶'
  );
  assert.ok(
    source.includes("totals['CNY']"),
    '手动账单必须计入 CNY 桶'
  );
});

test('源码契约：优先复用 backend due.totals_by_currency', () => {
  const start = htmlContent.indexOf('const totalOutstandingByCurrency = computed(() => {');
  assert.ok(start > 0, '未找到 totalOutstandingByCurrency 定义');
  const end = htmlContent.indexOf('});', start);
  const source = htmlContent.slice(start, end);
  assert.ok(
    source.includes('totals_by_currency'),
    '必须优先复用 backend 的 totals_by_currency'
  );
});

test('TEMPLATE_USED_CURRENCY_COMPUTEDS_EXPOSED = PASS: setup() 显式暴露模板所使用的全部币种计算属性', () => {
  const mountIdx = htmlContent.lastIndexOf(".mount('#app')");
  assert.ok(mountIdx > 0, "未找到 .mount('#app')");
  const returnIdx = htmlContent.lastIndexOf("return {", mountIdx);
  assert.ok(returnIdx > 0, "未找到 setup return 代码块");
  const endReturnIdx = htmlContent.indexOf("};", returnIdx);
  assert.ok(endReturnIdx > returnIdx, "未找到 setup return 闭合");
  const returnBlock = htmlContent.slice(returnIdx + 8, endReturnIdx);
  const returnedProps = new Set();
  for (const line of returnBlock.split('\n')) {
    const trimmed = line.trim().replace(/,$/, '');
    if (!trimmed || trimmed.startsWith('//') || trimmed.startsWith('/*')) continue;
    const propName = trimmed.split(/[\s:]/)[0];
    if (propName) returnedProps.add(propName);
  }

  assert.ok(returnedProps.has('totalOutstandingDisplay'), 'setup() return 必须包含 totalOutstandingDisplay');
  assert.ok(returnedProps.has('totalOutstandingCurrency'), 'setup() return 必须包含 totalOutstandingCurrency');
  assert.ok(returnedProps.has('sevenDaysDueByCurrency'), 'setup() return 必须包含 sevenDaysDueByCurrency');
  assert.ok(returnedProps.has('sevenDaysDueDisplay'), 'setup() return 必须包含 sevenDaysDueDisplay');
  assert.ok(returnedProps.has('totalOutstandingMinor'), 'setup() return 必须保留 totalOutstandingMinor');
  assert.ok(returnedProps.has('sevenDaysDueTotalMinor'), 'setup() return 必须保留 sevenDaysDueTotalMinor');
});

// ============================================================================
// 2. Behavioral tests: 真实生产计算属性在 VM 中执行
// ============================================================================

const fStart = htmlContent.indexOf('function formatMoney(minor) {');
const fEnd = htmlContent.indexOf('function getBankCode(bankName) {', fStart);
const cStart = htmlContent.indexOf('function formatMoneyWithCurrency(minor, currency, isMixed = false) {');
const cEnd = htmlContent.indexOf('const showCreditLimitShareModal = ref(false);', cStart);
assert.ok(fStart > 0 && fEnd > fStart && cStart > 0 && cEnd > cStart, '定位生产币种计算属性源码');

const prodCode = htmlContent.slice(fStart, fEnd) + '\n' + htmlContent.slice(cStart, cEnd) +
  '\n; ({ totalOutstandingByCurrency, totalOutstandingMinor, totalOutstandingDisplay, totalOutstandingCurrency, sevenDaysDueCards, sevenDaysDueByCurrency, sevenDaysDueTotalMinor, sevenDaysDueDisplay });';

function evaluateCurrencyState(dashboardVal) {
  const sandbox = {
    computed: fn => ({ get value() { return fn(); } }),
    ref: v => ({ value: v }),
    dashboard: { value: dashboardVal },
    markedPaidRecords: { value: [] },
    manualMarkedPaidRecords: { value: [] },
    currentMonthString: '2026-09',
    showCreditLimit: { value: true },
    showSevenDaysDue: { value: true },
    showTotalDue: { value: true },
    localStorage: { setItem: () => {} }
  };
  const res = vm.runInNewContext(prodCode, sandbox);
  return {
    totalOutstandingByCurrency: { ...res.totalOutstandingByCurrency.value },
    totalOutstandingMinor: res.totalOutstandingMinor.value,
    totalOutstandingDisplay: res.totalOutstandingDisplay.value,
    totalOutstandingCurrency: res.totalOutstandingCurrency.value,
    sevenDaysDueByCurrency: { ...res.sevenDaysDueByCurrency.value },
    sevenDaysDueTotalMinor: res.sevenDaysDueTotalMinor.value,
    sevenDaysDueDisplay: res.sevenDaysDueDisplay.value,
  };
}

test('行为：CNY + USD dashboard 不产生混币数字总额 (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: null,
      totals_by_currency: { CNY: 120000, USD: 30000 },
      cards: [
        { remaining_minor: 120000, currency: 'CNY', days_left: 2 },
        { remaining_minor: 30000, currency: 'USD', days_left: 4 }
      ]
    },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.totalOutstandingMinor, null, '混币场景不得产生单一数字总额');
  assert.ok(!res.totalOutstandingDisplay.includes('1,500'), '不得出现 1500.00 混币总额');
  assert.ok(res.totalOutstandingDisplay.includes('1,200.00'), '必须分组显示 CNY 金额');
  assert.ok(res.totalOutstandingDisplay.includes('300.00 USD'), '必须分组显示 USD 金额');
});

test('行为：mixed backend total_remaining_minor = null 时 UI 仍显示各币种待还 (生产 computed)', () => {
  const dashboard = {
    due: { total_remaining_minor: null, totals_by_currency: { CNY: 50000, USD: 80000 }, cards: [] },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.ok(res.totalOutstandingDisplay.includes('500.00'), 'CNY 待还不得被隐藏');
  assert.ok(res.totalOutstandingDisplay.includes('800.00 USD'), 'USD 待还不得被隐藏');
});

test('行为：manual CNY + 信用卡 CNY 可在同一 CNY 桶内相加 (生产 computed)', () => {
  const dashboard = {
    due: { total_remaining_minor: 120000, totals_by_currency: { CNY: 120000 }, cards: [{ remaining_minor: 120000, currency: 'CNY' }] },
    manual_due: { total_remaining_minor: 50000, items: [{ amount_minor: 50000 }] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.totalOutstandingByCurrency['CNY'], 170000, '同币种允许桶内相加');
  assert.equal(res.totalOutstandingMinor, 170000);
  assert.equal(res.totalOutstandingDisplay, '1,700.00', '单币种保持现有 UX（纯金额展示）');
});

test('行为：USD 独立分桶，不与 CNY manual 相加 (生产 computed)', () => {
  const dashboard = {
    due: { total_remaining_minor: null, totals_by_currency: { USD: 30000 }, cards: [{ remaining_minor: 30000, currency: 'USD' }] },
    manual_due: { total_remaining_minor: 20000, items: [{ amount_minor: 20000 }] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.totalOutstandingByCurrency['CNY'], 20000, 'manual 归入 CNY 桶');
  assert.equal(res.totalOutstandingByCurrency['USD'], 30000, 'USD 独立分桶');
  assert.equal(res.totalOutstandingMinor, null, '混币不得有单一总额');
  assert.ok(!res.totalOutstandingDisplay.includes('500.00'), '不得出现 500.00 混币总额');
  assert.ok(res.totalOutstandingDisplay.includes('200.00') && res.totalOutstandingDisplay.includes('300.00 USD'));
});

test('行为：fallback（无 totals_by_currency）按 card.currency 分组 (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: null,
      cards: [
        { remaining_minor: 10000, currency: 'CNY', days_left: 3 },
        { remaining_minor: 25000, currency: 'USD', days_left: 5 }
      ]
    },
    manual_due: { total_remaining_minor: 3000, items: [{ amount_minor: 3000 }] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.deepEqual(res.totalOutstandingByCurrency, { CNY: 13000, USD: 25000 });
});

test('行为：七日待还 CNY 卡 + USD 卡不混加 (生产 computed)', () => {
  const dashboard = {
    due: {
      cards: [
        { remaining_minor: 10000, currency: 'CNY', days_left: 3 },
        { remaining_minor: 25000, currency: 'USD', days_left: 5 }
      ]
    },
    manual_due: { items: [{ amount_minor: 2000, days_left: 2 }] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.deepEqual(res.sevenDaysDueByCurrency, { CNY: 12000, USD: 25000 }, 'manual 计入 CNY，USD 独立');
  assert.equal(res.sevenDaysDueTotalMinor, null, '七日混币不得有单一总额');
  assert.ok(res.sevenDaysDueDisplay.includes('120.00') && res.sevenDaysDueDisplay.includes('250.00 USD'));
  assert.ok(!res.sevenDaysDueDisplay.includes('370'), '不得出现 370.00 混币总额');
});

test('行为：七日待还单币种 CNY 保持现有 UX (生产 computed)', () => {
  const dashboard = {
    due: { cards: [{ remaining_minor: 10000, currency: 'CNY', days_left: 3 }] },
    manual_due: { items: [{ amount_minor: 2000, days_left: 2 }] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.sevenDaysDueTotalMinor, 12000, '单币种 CNY 内允许相加');
  assert.equal(res.sevenDaysDueDisplay, '120.00');
});

test('行为：空数据零兜底不崩溃 (生产 computed)', () => {
  const res = evaluateCurrencyState({ due: { cards: [] }, manual_due: { items: [] } });
  assert.equal(res.totalOutstandingMinor, 0, '空集按 0 处理（无待还，非混币隐藏）');
  assert.equal(res.totalOutstandingDisplay, '0.00');
});

test('single CNY -> existing UX (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: 120000,
      totals_by_currency: { CNY: 120000 },
      cards: [{ remaining_minor: 120000, currency: 'CNY', days_left: 2 }]
    },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.totalOutstandingMinor, 120000);
  assert.equal(res.totalOutstandingCurrency, 'CNY');
  assert.equal(res.totalOutstandingDisplay, '1,200.00', '单 CNY totalOutstandingDisplay 应为纯金额');
  assert.ok(!res.totalOutstandingDisplay.includes('¥'), '单 CNY totalOutstandingDisplay 内部不得带 ¥（避免双 ¥）');
  assert.ok(!res.totalOutstandingDisplay.includes('CNY'), '单 CNY totalOutstandingDisplay 不追加 CNY 后缀');
});

test('mixed CNY+USD -> both currencies identifiable (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: null,
      totals_by_currency: { CNY: 120000, USD: 30000 },
      cards: [
        { remaining_minor: 120000, currency: 'CNY', days_left: 2 },
        { remaining_minor: 30000, currency: 'USD', days_left: 4 }
      ]
    },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.ok(res.totalOutstandingDisplay.includes('¥1,200.00'), '总待还 CNY 金额必须明确标识币种 (¥1,200.00)');
  assert.ok(res.totalOutstandingDisplay.includes('300.00 USD'), '总待还 USD 金额必须明确标识币种 (300.00 USD)');
  assert.ok(res.sevenDaysDueDisplay.includes('¥1,200.00'), '七日待还 CNY 金额必须明确标识币种 (¥1,200.00)');
  assert.ok(res.sevenDaysDueDisplay.includes('300.00 USD'), '七日待还 USD 金额必须明确标识币种 (300.00 USD)');
});

test('mixed display -> no grand total (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: null,
      totals_by_currency: { CNY: 120000, USD: 30000 },
      cards: [
        { remaining_minor: 120000, currency: 'CNY', days_left: 2 },
        { remaining_minor: 30000, currency: 'USD', days_left: 4 }
      ]
    },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.equal(res.totalOutstandingMinor, null, '混币总待还 scalar 必须为 null');
  assert.ok(!res.totalOutstandingDisplay.includes('1,500'), '总待还展示不得包含混币加总 1500');
  assert.equal(res.sevenDaysDueTotalMinor, null, '七日待还混币 scalar 必须为 null');
  assert.ok(!res.sevenDaysDueDisplay.includes('1,500'), '七日待还展示不得包含混币加总 1500');
});

test('mixed display -> no unlabeled CNY amount (生产 computed)', () => {
  const dashboard = {
    due: {
      total_remaining_minor: null,
      totals_by_currency: { CNY: 120000, USD: 30000 },
      cards: [
        { remaining_minor: 120000, currency: 'CNY', days_left: 2 },
        { remaining_minor: 30000, currency: 'USD', days_left: 4 }
      ]
    },
    manual_due: { total_remaining_minor: 0, items: [] }
  };
  const res = evaluateCurrencyState(dashboard);
  assert.ok(
    !res.totalOutstandingDisplay.match(/(^|\s)1,200\.00(\s|$)/),
    '混币总待还中 CNY 金额不得作为未标记数字呈现'
  );
  assert.ok(res.totalOutstandingDisplay.includes('¥1,200.00'), 'CNY 必须带有明确币种前缀 ¥');
  assert.ok(
    !res.sevenDaysDueDisplay.match(/(^|\s)1,200\.00(\s|$)/),
    '七日待还混币中 CNY 金额不得作为未标记数字呈现'
  );
  assert.ok(res.sevenDaysDueDisplay.includes('¥1,200.00'), '七日待还 CNY 必须带有明确币种前缀 ¥');
});
