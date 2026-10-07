const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const HTML_PATH = path.resolve(__dirname, '../web/index.html');
const htmlContent = fs.readFileSync(HTML_PATH, 'utf-8');

// ============================================================================
// 1. Template & Contract Tests (web/index.html)
// ============================================================================

test('UI 模板契约：存在独立的“账单与还款”配置区', () => {
  assert.ok(htmlContent.includes('账单与还款'), '必须包含“账单与还款”标题');
  assert.ok(htmlContent.includes('账单日'), '必须包含“账单日”标签');
  assert.ok(htmlContent.includes('还款规则'), '必须包含“还款规则”标签');
});

test('UI 模板契约：账单日始终为只读账户事实', () => {
  const start = htmlContent.indexOf('<!-- 账单日：账户事实，只读展示 -->');
  const end = htmlContent.indexOf('<!-- 还款规则 -->', start);
  assert.ok(start > 0 && end > start, '未能定位账单日展示区域');
  const snippet = htmlContent.slice(start, end);

  assert.ok(snippet.includes('账单日'), '必须显示“账单日”标签');
  assert.ok(
    snippet.includes("{{ editingCard.statement_day ? (editingCard.statement_day + ' 日') : '待识别' }}"),
    '账单日应以“N 日”纯文本展示，缺失时显示“待识别”'
  );
  assert.ok(!snippet.includes('修改'), '账单日区域不得提供修改入口');
  assert.ok(!htmlContent.includes('isEditingStatementDay'), '不得保留账单日编辑状态');
  assert.ok(!htmlContent.includes('cancelEditStatementDay'), '不得保留账单日取消编辑函数');
  assert.ok(!htmlContent.includes('v-model.number="editCardForm.statement_day"'), '不得存在账单日输入控件');
});

test('UI 模板契约：不得包含废弃与内部实现的多余文案', () => {
  const forbiddenPhrases = [
    '来自最近真实账单',
    '手动设置的还款规则将优先用于免息期预测',
    '自动获取',
    '固定日期',
    'confidence score',
    'multiple_known_rules',
    'statement_day_map'
  ];

  const modalStart = htmlContent.indexOf('<!-- Edit Limit Modal -->');
  const modalEnd = htmlContent.indexOf('<!-- Delete Card Modal');
  assert.ok(modalStart > 0 && modalEnd > modalStart, '未能定位卡片设置弹窗代码段');
  const modalSnippet = htmlContent.slice(modalStart, modalEnd);

  for (const phrase of forbiddenPhrases) {
    assert.ok(!modalSnippet.includes(phrase), `卡片设置弹窗内不得出现多余/内部文案: "${phrase}"`);
  }
});

test('UI 模板契约：还款规则选项文案精确匹配', () => {
  const modalStart = htmlContent.indexOf('<!-- Edit Limit Modal -->');
  const modalEnd = htmlContent.indexOf('<!-- Delete Card Modal');
  const modalSnippet = htmlContent.slice(modalStart, modalEnd);

  assert.ok(modalSnippet.includes('自动识别'), '必须提供“自动识别”选项');
  assert.ok(modalSnippet.includes('固定还款日'), '必须提供“固定还款日”选项');
  assert.ok(modalSnippet.includes('账单日 + N 天'), '必须提供“账单日 + N 天”选项');
});

test('UI 架构审计：WebUI 绝不自行根据 statement/bank_code 倒推还款规则', () => {
  const autoFnStart = htmlContent.indexOf('const autoRecognizedRule = computed(() => {');
  assert.ok(autoFnStart > 0, '未找到 autoRecognizedRule 定义');
  const autoFnEnd = htmlContent.indexOf('});', autoFnStart);
  const autoFnSource = htmlContent.slice(autoFnStart, autoFnEnd);

  assert.ok(!autoFnSource.includes('statements.value'), 'autoRecognizedRule 不得扫描 statements.value');
  assert.ok(!autoFnSource.includes('statement_date'), 'autoRecognizedRule 不得基于 statement_date 自行计算');
  assert.ok(!autoFnSource.includes('due_date'), 'autoRecognizedRule 不得基于 due_date 自行计算');

  assert.ok(!autoFnSource.includes('bank_code'), 'autoRecognizedRule 不得硬编码 bank_code 规则');
  assert.ok(!autoFnSource.includes('ICBC'), '不得硬编码 ICBC');
  assert.ok(!autoFnSource.includes('ABC'), '不得硬编码 ABC');
  assert.ok(!autoFnSource.includes('CMB'), '不得硬编码 CMB');
});

// ============================================================================
// 2. Logic & Presentation Mapping Tests (真实生产函数抽取与调用)
// ============================================================================

const sStart = htmlContent.indexOf('function selectDueRuleType(type) {');
const sEnd = htmlContent.indexOf('const activeFeeSchedulePicker = ref(null);', sStart);
assert.ok(sStart > 0 && sEnd > sStart, '未能定位 selectDueRuleType / autoRecognizedRule 源码');
const ruleSource = htmlContent.slice(sStart, sEnd);

const saveStart = htmlContent.indexOf('async function saveCardDetails() {');
const saveEnd = htmlContent.indexOf('async function openDeleteCardModal', saveStart);
assert.ok(saveStart > 0 && saveEnd > saveStart, '未能定位 saveCardDetails 源码');
const saveSource = htmlContent.slice(saveStart, saveEnd);

function evaluateAutoRecognizedRule(card, ifEstimate) {
  const sandbox = {
    computed: fn => ({ get value() { return fn(); } }),
    editingCard: { value: card },
    cardInterestFree: { value: card ? { [card.id]: ifEstimate } : {} },
    editCardForm: { value: {} }
  };
  return vm.runInNewContext(`${ruleSource}; autoRecognizedRule.value`, sandbox);
}

test('DTO 消费：后端返回 card_history 证据时正确映射展示 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c1', statement_day: 18 };
  const ifEstimate = {
    available: true,
    estimate: {
      rule_source: 'card_history',
      resolved_rule_type: 'days_after_statement',
      resolved_rule_value: 20,
      history_evidence_count: 4,
      last_verified_date: '2026-09-18'
    }
  };
  const res = evaluateAutoRecognizedRule(card, ifEstimate);
  assert.equal(res.status, 'recognized');
  assert.equal(res.header, '已识别当前还款规则');
  assert.equal(res.ruleText, '账单日后 20 天');
  assert.equal(res.statementCount, 4);
  assert.equal(res.lastVerifiedDate, '2026-09-18');
});

test('DTO 消费：后端返回 fixed_day 历史规则时正确映射展示 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c2', statement_day: 20 };
  const ifEstimate = {
    available: true,
    estimate: {
      rule_source: 'card_history',
      resolved_rule_type: 'fixed_day',
      resolved_rule_value: 10,
      history_evidence_count: 3,
      last_verified_date: '2026-09-20'
    }
  };
  const res = evaluateAutoRecognizedRule(card, ifEstimate);
  assert.equal(res.status, 'recognized');
  assert.equal(res.header, '已识别当前还款规则');
  assert.equal(res.ruleText, '固定还款日：每月 10 日');
  assert.equal(res.statementCount, 3);
  assert.equal(res.lastVerifiedDate, '2026-09-20');
});

test('DTO 消费：后端返回 bank_billing_rule 时绝不显示伪造历史证据 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c3', statement_day: 15 };
  const ifEstimate = {
    available: true,
    estimate: {
      rule_source: 'bank_billing_rule',
      resolved_rule_type: 'days_after_statement',
      resolved_rule_value: 18,
      history_evidence_count: null,
      last_verified_date: null
    }
  };
  const res = evaluateAutoRecognizedRule(card, ifEstimate);
  assert.equal(res.status, 'recognized');
  assert.equal(res.header, '已确定当前还款规则');
  assert.equal(res.ruleText, '账单日后 18 天');
  assert.equal(res.statementCount, null, 'bank_billing_rule 绝无历史期数');
  assert.equal(res.lastVerifiedDate, null);
});

test('DTO 消费：后端返回工行固定25日时纯由后端 DTO 决定，前端无特殊分支 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c4', statement_day: 1 };
  const ifEstimate = {
    available: true,
    estimate: {
      rule_source: 'bank_billing_rule',
      resolved_rule_type: 'fixed_day',
      resolved_rule_value: 25,
      history_evidence_count: null,
      last_verified_date: null
    }
  };
  const res = evaluateAutoRecognizedRule(card, ifEstimate);
  assert.equal(res.status, 'recognized');
  assert.equal(res.header, '已确定当前还款规则');
  assert.equal(res.ruleText, '固定还款日：每月 25 日');
  assert.equal(res.statementCount, null);
});

test('DTO 消费：后端 unavailable 时显示未识别 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c5', statement_day: 12 };
  const reasons = ['unresolved_due_date_rule', 'ambiguous_due_date_rule', 'conflicting_due_date_rule'];

  for (const reason of reasons) {
    const ifEstimate = {
      available: false,
      reason: reason,
      estimate: null
    };
    const res = evaluateAutoRecognizedRule(card, ifEstimate);
    assert.equal(res.status, 'unrecognized', `原因 ${reason} 应展示为 unrecognized`);
  }
});

test('规则切换：切换选项时不会残留上一个 rule_value (生产 selectDueRuleType)', () => {
  const editingCard = {
    value: { id: 'c1', statement_day: 18, due_date_rule_type: 'observed_only', due_date_rule_value: null }
  };
  const editCardForm = {
    value: { due_date_rule_type: 'observed_only', due_date_rule_value: null }
  };
  const sandbox = {
    computed: fn => ({ get value() { return fn(); } }),
    editCardForm,
    editingCard,
    cardInterestFree: { value: {} }
  };
  vm.createContext(sandbox);
  vm.runInContext(ruleSource, sandbox);

  sandbox.selectDueRuleType('fixed_day');
  assert.equal(editCardForm.value.due_date_rule_type, 'fixed_day');
  assert.equal(editCardForm.value.due_date_rule_value, null);
  editCardForm.value.due_date_rule_value = 10;

  sandbox.selectDueRuleType('days_after_statement');
  assert.equal(editCardForm.value.due_date_rule_type, 'days_after_statement');
  assert.equal(editCardForm.value.due_date_rule_value, null, '切换规则选项不得残留前一个 rule_value');

  editCardForm.value.due_date_rule_value = 20;

  sandbox.selectDueRuleType('observed_only');
  assert.equal(editCardForm.value.due_date_rule_type, 'observed_only');
  assert.equal(editCardForm.value.due_date_rule_value, null);
});

async function runProductionSave(formValues, cardValues) {
  let capturedPayload = null;
  let toastMsg = null;
  const saveSandbox = {
    editingCard: { value: cardValues },
    editCardForm: { value: formValues },
    api: async (url, options) => {
      capturedPayload = JSON.parse(options.body);
      return {};
    },
    showToast: msg => { toastMsg = msg; },
    loadAll: async () => {}
  };
  vm.createContext(saveSandbox);
  vm.runInContext(saveSource, saveSandbox);
  await saveSandbox.saveCardDetails();
  return { payload: capturedPayload, toast: toastMsg };
}

test('保存边界：Card Settings 永不提交 statement_day (生产 saveCardDetails)', async () => {
  const form = {
    statement_day: 25,
    due_date_rule_type: 'observed_only',
    due_date_rule_value: null,
    hidden: false,
    status: 'active'
  };
  const card = { id: 'c1', annual_fee_source: null };
  const { payload } = await runProductionSave(form, card);
  assert.equal(payload.statement_day, undefined, '即使表单对象带有 statement_day，也不得进入 PATCH');
  assert.equal(payload.due_date_rule_type, 'observed_only');
  assert.equal(payload.due_date_rule_value, null);
});

test('保存与回显：fixed_day 与 days_after_statement 正确保存 (生产 saveCardDetails)', async () => {
  const card = { id: 'c1', due_date_rule_type: 'observed_only', due_date_rule_value: null };
  const formFixed = {
    due_date_rule_type: 'fixed_day',
    due_date_rule_value: 28,
    hidden: false,
    status: 'active'
  };
  const resFixed = await runProductionSave(formFixed, card);
  assert.equal(resFixed.payload.due_date_rule_type, 'fixed_day');
  assert.equal(resFixed.payload.due_date_rule_value, 28);
  assert.equal(resFixed.payload.statement_day, undefined);

  const formDays = {
    due_date_rule_type: 'days_after_statement',
    due_date_rule_value: 20,
    hidden: false,
    status: 'active'
  };
  const resDays = await runProductionSave(formDays, card);
  assert.equal(resDays.payload.due_date_rule_type, 'days_after_statement');
  assert.equal(resDays.payload.due_date_rule_value, 20);
});

test('保存与回显：observed_only 保持自动识别且 value 为 null (生产 saveCardDetails)', async () => {
  const card = { id: 'c1', annual_fee_source: null };
  const form = {
    due_date_rule_type: 'observed_only',
    due_date_rule_value: null,
    hidden: false,
    status: 'active'
  };
  const res = await runProductionSave(form, card);
  assert.equal(res.payload.due_date_rule_type, 'observed_only');
  assert.equal(res.payload.due_date_rule_value, null);
  assert.equal(res.payload.statement_day, undefined);
});

test('DTO 消费：后端返回 product_override 时映射为已确定规则且不含伪证据 (生产 autoRecognizedRule)', () => {
  const card = { id: 'c_njcb', statement_day: 20 };
  const ifEstimate = {
    available: true,
    estimate: {
      rule_source: 'product_override',
      resolved_rule_type: 'days_after_statement',
      resolved_rule_value: 25,
      history_evidence_count: null,
      last_verified_date: null
    }
  };
  const res = evaluateAutoRecognizedRule(card, ifEstimate);
  assert.equal(res.status, 'recognized');
  assert.equal(res.header, '已确定当前还款规则');
  assert.equal(res.ruleText, '账单日后 25 天');
  assert.equal(res.statementCount, null, 'product_override 不得伪造历史期数');
  assert.equal(res.lastVerifiedDate, null);
});
