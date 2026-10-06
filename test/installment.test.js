const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
  round2,
  solveMonthlyIRR,
  solveMonthlyIRR_upfront,
  generateSchedule,
  calculateInstallment
} = require(path.resolve(__dirname, '../web/js/installment.js'));

test('固定基准测试：本金 10000, 12 期, 每期费率 0.6%（分期等额收取）', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'fee_rate_per_period',
    feeValue: 0.6,
    feeChargeMode: 'per_period'
  });

  assert.equal(res.valid, true);
  assert.equal(round2(res.feePerPeriod), 60.00);
  assert.equal(round2(res.totalFee), 720.00);
  assert.equal(round2(res.totalRepayment), 10720.00);
  assert.equal(round2(res.payment), 893.33);

  assert.equal((res.totalFeeRate * 100).toFixed(2), '7.20');
  assert.equal((res.nominalAnnualRate * 100).toFixed(2), '7.20');

  // monthly IRR 约 1.086185%
  assert.ok(Math.abs(res.monthlyIRR * 100 - 1.086185) < 1e-4);
  // 真实 APR 约 13.0342%
  assert.ok(Math.abs(res.apr * 100 - 13.0342) < 1e-3);
  // EAR 约 13.8418%
  assert.ok(Math.abs(res.ear * 100 - 13.8418) < 1e-3);

  // 还款明细尾差校正检验
  assert.equal(res.schedule.length, 12);
  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  const sumFee = round2(res.schedule.reduce((acc, row) => acc + row.fee, 0));
  const sumPayment = round2(res.schedule.reduce((acc, row) => acc + row.payment, 0));

  assert.equal(sumPrincipal, 10000.00);
  assert.equal(sumFee, 720.00);
  assert.equal(sumPayment, 10720.00);
  assert.equal(res.schedule[11].remainingPrincipal, 0.00);
});

test('首期一次性收取手续费模型测试：本金 10000, 12 期, 总费率 7.2%', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'total_fee_rate',
    feeValue: 7.2,
    feeChargeMode: 'first_period'
  });

  assert.equal(res.valid, true);
  assert.equal(round2(res.totalFee), 720.00);
  assert.equal(round2(res.totalRepayment), 10720.00);

  // 首期支付本金 833.33 + 手续费 720 = 1553.33
  assert.equal(round2(res.firstPeriodPayment), 1553.33);
  assert.equal(round2(res.subsequentPayment), 833.33);

  // 首期一次性收取的资金成本比分期收取更高！
  // 真实 APR 约 13.8560% (高于分期收取的 13.0342%)
  assert.ok(Math.abs(res.apr * 100 - 13.8560) < 1e-3);
  // EAR 约 14.7707% (高于分期收取的 13.8418%)
  assert.ok(Math.abs(res.ear * 100 - 14.7707) < 1e-3);

  // 还款明细检验
  assert.equal(res.schedule.length, 12);
  assert.equal(res.schedule[0].fee, 720.00);
  assert.equal(res.schedule[1].fee, 0.00);
  assert.equal(res.schedule[11].fee, 0.00);

  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  const sumFee = round2(res.schedule.reduce((acc, row) => acc + row.fee, 0));
  const sumPayment = round2(res.schedule.reduce((acc, row) => acc + row.payment, 0));

  assert.equal(sumPrincipal, 10000.00);
  assert.equal(sumFee, 720.00);
  assert.equal(sumPayment, 10720.00);
  assert.equal(res.schedule[11].remainingPrincipal, 0.00);
});

test('0 手续费测试', () => {
  const res = calculateInstallment({
    principal: 12000,
    periods: 12,
    feeMode: 'fee_rate_per_period',
    feeValue: 0
  });

  assert.equal(res.valid, true);
  assert.equal(res.totalFee, 0);
  assert.equal(res.monthlyIRR, 0);
  assert.equal(res.apr, 0);
  assert.equal(res.ear, 0);
  assert.equal(res.totalRepayment, 12000);
  assert.equal(res.payment, 1000);
});

test('3 期测试', () => {
  const res = calculateInstallment({
    principal: 9000,
    periods: 3,
    feeMode: 'fee_rate_per_period',
    feeValue: 0.6
  });

  assert.equal(res.valid, true);
  assert.equal(res.feePerPeriod, 54);
  assert.equal(res.totalFee, 162);
  assert.equal(res.totalRepayment, 9162);
  assert.equal((res.nominalAnnualRate * 100).toFixed(2), '7.20');
  assert.equal(res.schedule.length, 3);
});

test('24 期测试', () => {
  const res = calculateInstallment({
    principal: 24000,
    periods: 24,
    feeMode: 'fee_rate_per_period',
    feeValue: 0.6
  });

  assert.equal(res.valid, true);
  assert.equal(res.feePerPeriod, 144);
  assert.equal(res.totalFee, 3456);
  assert.equal(res.totalRepayment, 27456);
  assert.equal((res.nominalAnnualRate * 100).toFixed(2), '7.20');
  assert.equal(res.schedule.length, 24);
});

test('总费率输入模式', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'total_fee_rate',
    feeValue: 7.2
  });

  assert.equal(res.valid, true);
  assert.equal(round2(res.totalFee), 720.00);
  assert.equal(round2(res.feePerPeriod), 60.00);
  assert.equal(round2(res.payment), 893.33);
  assert.equal((res.apr * 100).toFixed(4), '13.0342');
});

test('每期手续费输入模式', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'fee_per_period',
    feeValue: 60
  });

  assert.equal(res.valid, true);
  assert.equal(res.feePerPeriod, 60);
  assert.equal(res.totalFee, 720);
  assert.equal(round2(res.payment), 893.33);
  assert.equal((res.apr * 100).toFixed(4), '13.0342');
});

test('每期还款额输入模式', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'payment_per_period',
    feeValue: 893.3333333333
  });

  assert.equal(res.valid, true);
  assert.equal(round2(res.totalFee), 720.00);
  assert.equal((res.apr * 100).toFixed(4), '13.0342');
});

test('异常输入：非法 0 本金与负数', () => {
  const zeroP = calculateInstallment({ principal: 0, periods: 12, feeMode: 'fee_rate_per_period', feeValue: 0.6 });
  assert.equal(zeroP.valid, false);
  assert.match(zeroP.error, /本金|金额/);

  const negP = calculateInstallment({ principal: -100, periods: 12, feeMode: 'fee_rate_per_period', feeValue: 0.6 });
  assert.equal(negP.valid, false);

  const negFee = calculateInstallment({ principal: 1000, periods: 12, feeMode: 'fee_rate_per_period', feeValue: -0.5 });
  assert.equal(negFee.valid, false);
  assert.match(negFee.error, /不能为负数/);
});

test('异常输入：每期还款额低于本金/期数', () => {
  const res = calculateInstallment({
    principal: 10000,
    periods: 12,
    feeMode: 'payment_per_period',
    feeValue: 800
  });

  assert.equal(res.valid, false);
  assert.match(res.error, /不能小于分期本金/);
});

test('小数金额支持', () => {
  const res = calculateInstallment({
    principal: 9999.99,
    periods: 12,
    feeMode: 'fee_rate_per_period',
    feeValue: 0.65
  });

  assert.equal(res.valid, true);
  assert.ok(res.payment > 0);
  assert.ok(res.apr > 0);
  assert.equal(res.schedule.length, 12);

  const sumP = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  assert.equal(sumP, 9999.99);
  assert.equal(res.schedule[11].remainingPrincipal, 0.00);
});
