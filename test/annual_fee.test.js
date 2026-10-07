const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.resolve(__dirname, '../web/index.html'), 'utf8');

const fStart = html.indexOf('function formatMoney(minor) {');
const fEnd = html.indexOf('function getBankCode(bankName) {', fStart);
const p1 = html.indexOf('function isUnconditionalFree(policy, feeMinor) {');
const p2 = html.indexOf('function isCardAnnualFeeConfirmed(cardId) {');
assert.ok(fStart > 0 && fEnd > fStart && p1 > 0 && p2 > p1, 'locate production functions in web/index.html');

const productionCode = html.slice(fStart, fEnd) + '\n' + html.slice(p1, p2);

function evaluateAnnualFee(card, txns = [], stmts = [], fakeDate = null) {
  const sandbox = {
    statements: { value: stmts },
    allTransactions: { value: txns },
  };
  if (fakeDate) {
    const RealDate = Date;
    sandbox.Date = class extends RealDate {
      constructor(...args) {
        if (args.length === 0) super(fakeDate);
        else super(...args);
      }
      static now() { return new Date(fakeDate).getTime(); }
    };
  }
  vm.createContext(sandbox);
  vm.runInContext(productionCode, sandbox);
  return {
    evaluation: sandbox.getAnnualFeeEvaluation(card),
    isUnconditionalFree: sandbox.isUnconditionalFree,
  };
}

test('年费审计：终身免年费卡 (生产代码调用)', () => {
  const card = { id: 'c1', annual_fee_minor: 0, annual_fee_policy: '卡片有效期内无条件免年费' };
  const res = evaluateAnnualFee(card).evaluation;
  assert.equal(res.status, 'free');
  assert.equal(res.isAchieved, true);
  assert.equal(res.badge, '免年费');
});

test('年费审计：刚性不可免年费卡 (生产代码调用)', () => {
  const card = { id: 'c2', annual_fee_minor: 360000, annual_fee_policy: '刚性年费3600元，核卡即收不可减免', annual_fee_charge_date: '08-15' };
  const res = evaluateAnnualFee(card).evaluation;
  assert.equal(res.status, 'rigid');
  assert.equal(res.isAchieved, false);
  assert.equal(res.badge, '刚性必缴');
});

test('年费审计：缺少扣款日，无法开始考核 (生产代码调用)', () => {
  const card = { id: 'c3', annual_fee_minor: 48000, annual_fee_policy: '每年刷卡满6次免次年', annual_fee_charge_date: null };
  const res = evaluateAnnualFee(card).evaluation;
  assert.equal(res.status, 'missing_date');
  assert.equal(res.isAchieved, false);
});

test('年费审计：仅设置刷免费和扣款日，未消费时不应提示已达标 (生产代码调用)', () => {
  const card = { id: 'c4', annual_fee_minor: 36000, annual_fee_policy: '首年免年费，单卡每年刷卡消费满6次免次年', annual_fee_charge_date: '06-12' };
  const res = evaluateAnnualFee(card, [], [], '2026-09-10').evaluation;
  assert.equal(res.status, 'in_progress');
  assert.equal(res.isAchieved, false);
  assert.match(res.badge, /差 6 笔/);
});

test('年费审计：消费达到6笔后，提示预估已达标并附带App核验警示 (生产代码调用)', () => {
  const card = { id: 'c4', annual_fee_minor: 36000, annual_fee_policy: '首年免年费，单卡每年刷卡消费满6次免次年', annual_fee_charge_date: '06-12' };
  const txns = [
    { card_id: 'c4', amount_minor: 1000, transaction_date: '2026-06-15' },
    { card_id: 'c4', amount_minor: 2000, transaction_date: '2026-06-20' },
    { card_id: 'c4', amount_minor: 3000, transaction_date: '2026-07-01' },
    { card_id: 'c4', amount_minor: 4000, transaction_date: '2026-07-15' },
    { card_id: 'c4', amount_minor: 5000, transaction_date: '2026-08-01' },
    { card_id: 'c4', amount_minor: 6000, transaction_date: '2026-08-20' },
  ];
  const res = evaluateAnnualFee(card, txns, [], '2026-09-10').evaluation;
  assert.equal(res.status, 'achieved');
  assert.equal(res.isAchieved, true);
  assert.equal(res.badge, '预估已达标');
  assert.ok(res.warning.includes('银行 App'));
});

test('年费审计：消费金额达标（满9万元） (生产代码调用)', () => {
  const card = { id: 'c5', annual_fee_minor: 48000, annual_fee_policy: '当年消费满9万元免次年', annual_fee_charge_date: '10-01' };
  const txns = [
    { card_id: 'c5', amount_minor: 9500000, transaction_date: '2026-05-01' },
  ];
  const res = evaluateAnnualFee(card, txns, [], '2026-09-10').evaluation;
  assert.equal(res.status, 'achieved');
  assert.equal(res.isAchieved, true);
  assert.equal(res.badge, '预估已达标');
});

test('年费审计：负向与条件性政策不得误判为终身免年费 (生产代码调用)', () => {
  const { isUnconditionalFree } = evaluateAnnualFee({ id: 'c1', annual_fee_minor: 0 });
  // 正向：无条件免年费
  assert.equal(isUnconditionalFree('终身免年费', 10000), true);
  assert.equal(isUnconditionalFree('有效期内免年费', 10000), true);
  assert.equal(isUnconditionalFree('无条件免年费', 10000), true);

  // 负向 / 刚性
  assert.equal(isUnconditionalFree('不免年费', 10000), false);
  assert.equal(isUnconditionalFree('不可减免年费', 10000), false);
  assert.equal(isUnconditionalFree('刚性年费', 10000), false);
  assert.equal(isUnconditionalFree('刚性年费，不免年费', 10000), false);

  // 条件性免年费
  assert.equal(isUnconditionalFree('首年免年费', 10000), false);
  assert.equal(isUnconditionalFree('首年免年费，次年刷满6次免年费', 10000), false);
  assert.equal(isUnconditionalFree('刷卡满5笔免次年年费', 10000), false);
});
