const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const {
  round2,
  calculateEqualPayment,
  calculateEqualPrincipal,
  calculateMortgage,
  aggregateMortgageByYear
} = require(path.resolve(__dirname, '../web/js/mortgage.js'));

const { getLatestLpr } = require(path.resolve(__dirname, '../web/js/lpr.js'));

test('LPR 数据模块正确性', () => {
  const lpr = getLatestLpr();
  assert.equal(lpr.fiveYear, 3.50);
  assert.equal(lpr.oneYear, 3.00);
  assert.equal(lpr.publishedAt, '2026-08-20');
});

test('1. 等额本息基准测试 (30年, 100万, 3.2%)', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: 1000000,
    commercialActualRate: 3.20
  });

  assert.equal(res.valid, true);
  assert.equal(res.months, 360);
  assert.equal(res.totalPrincipal, 1000000);
  assert.equal(res.monthlyPayment, 4324.67);
  assert.equal(res.schedule.length, 360);

  // 验证总还款与总利息
  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  const sumInterest = round2(res.schedule.reduce((acc, row) => acc + row.interest, 0));
  const sumPayment = round2(res.schedule.reduce((acc, row) => acc + row.payment, 0));

  assert.equal(sumPrincipal, 1000000.00);
  assert.equal(sumInterest, res.totalInterest);
  assert.equal(sumPayment, res.totalPayment);
  assert.equal(res.schedule[359].remainingPrincipal, 0.00);
});

test('2. 等额本金基准测试 (30年, 100万, 3.2%)', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_principal',
    years: 30,
    commercialPrincipal: 1000000,
    commercialActualRate: 3.20
  });

  assert.equal(res.valid, true);
  assert.equal(res.firstMonthPayment, 5444.45);
  assert.ok(res.monthlyDecrease > 0);
  assert.equal(res.schedule.length, 360);

  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  const sumInterest = round2(res.schedule.reduce((acc, row) => acc + row.interest, 0));
  const sumPayment = round2(res.schedule.reduce((acc, row) => acc + row.payment, 0));

  assert.equal(sumPrincipal, 1000000.00);
  assert.equal(sumInterest, res.totalInterest);
  assert.equal(sumPayment, res.totalPayment);
  assert.equal(res.schedule[359].remainingPrincipal, 0.00);
});

test('3. 0% 利率测试 (免息贷款)', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 5,
    commercialPrincipal: 120000,
    commercialActualRate: 0
  });

  assert.equal(res.valid, true);
  assert.equal(res.totalInterest, 0);
  assert.equal(res.monthlyPayment, 2000);
  assert.equal(res.totalPayment, 120000);
  assert.equal(res.schedule[59].remainingPrincipal, 0);
});

test('4. 商业贷款测试', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    months: 240,
    commercialPrincipal: 500000,
    commercialActualRate: 3.5
  });
  assert.equal(res.valid, true);
  assert.equal(res.totalPrincipal, 500000);
  assert.equal(res.schedule.length, 240);
  assert.equal(res.schedule[239].remainingPrincipal, 0);
});

test('5. 公积金贷款测试', () => {
  const res = calculateMortgage({
    loanType: 'provident_fund',
    repaymentMethod: 'equal_payment',
    years: 20,
    fundPrincipal: 800000,
    fundRate: 2.85
  });
  assert.equal(res.valid, true);
  assert.equal(res.totalPrincipal, 800000);
  assert.equal(res.schedule.length, 240);
  assert.equal(res.schedule[239].remainingPrincipal, 0);
});

test('6. 组合贷款测试', () => {
  const res = calculateMortgage({
    loanType: 'combined',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: 1000000,
    commercialActualRate: 3.20,
    fundPrincipal: 500000,
    fundRate: 2.85
  });

  assert.equal(res.valid, true);
  assert.equal(res.totalPrincipal, 1500000);
  assert.equal(res.schedule.length, 360);

  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  const sumInterest = round2(res.schedule.reduce((acc, row) => acc + row.interest, 0));
  const sumPayment = round2(res.schedule.reduce((acc, row) => acc + row.payment, 0));

  assert.equal(sumPrincipal, 1500000.00);
  assert.equal(sumInterest, res.totalInterest);
  assert.equal(sumPayment, res.totalPayment);
  assert.equal(res.schedule[359].remainingPrincipal, 0.00);
  assert.ok(res.schedule[0].commercialPrincipal > 0);
  assert.ok(res.schedule[0].fundPrincipal > 0);
});

test('7. 5年期贷款测试', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 5,
    commercialPrincipal: 300000,
    commercialActualRate: 3.10
  });
  assert.equal(res.valid, true);
  assert.equal(res.schedule.length, 60);
  assert.equal(res.schedule[59].remainingPrincipal, 0);
});

test('8. 30年期贷款测试与年度汇总', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: 2000000,
    commercialActualRate: 3.50
  });
  assert.equal(res.valid, true);
  assert.equal(res.schedule.length, 360);
  assert.equal(res.yearlySchedule.length, 30);
  assert.equal(res.yearlySchedule[29].remainingPrincipal, 0);
  const sumYearPrincipal = round2(res.yearlySchedule.reduce((acc, y) => acc + y.principal, 0));
  assert.equal(sumYearPrincipal, 2000000.00);
});

test('9. 小数本金测试', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 10,
    commercialPrincipal: 999999.99,
    commercialActualRate: 3.25
  });
  assert.equal(res.valid, true);
  assert.equal(res.schedule.length, 120);
  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  assert.equal(sumPrincipal, 999999.99);
  assert.equal(res.schedule[119].remainingPrincipal, 0.00);
});

test('10. 最后一期尾差校正测试', () => {
  // 测试导致产生除不尽分币的情况
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    months: 37,
    commercialPrincipal: 100000.01,
    commercialActualRate: 3.333
  });
  assert.equal(res.valid, true);
  const sumPrincipal = round2(res.schedule.reduce((acc, row) => acc + row.principal, 0));
  assert.equal(sumPrincipal, 100000.01);
  assert.equal(res.schedule[res.schedule.length - 1].remainingPrincipal, 0.00);
});

test('11. 非法负数测试', () => {
  const res1 = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: -50000,
    commercialActualRate: 3.2
  });
  assert.equal(res1.valid, false);

  const res2 = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: 50000,
    commercialActualRate: -3.2
  });
  assert.equal(res2.valid, false);
});

test('12. 0 本金测试', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 30,
    commercialPrincipal: 0,
    commercialActualRate: 3.2
  });
  assert.equal(res.valid, false);
  assert.match(res.error, /大于 0/);
});

test('13. 非法期限测试', () => {
  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    months: 0,
    commercialPrincipal: 100000,
    commercialActualRate: 3.2
  });
  assert.equal(res.valid, false);
  assert.match(res.error, /期限/);
});

test('14. BP 正数测试 (LPR 3.50% + 20 BP = 3.70%)', () => {
  const lpr = 3.50;
  const bp = 20;
  const actualRate = round2(lpr + bp / 100);
  assert.equal(actualRate, 3.70);

  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 20,
    commercialPrincipal: 1000000,
    commercialActualRate: actualRate
  });
  assert.equal(res.valid, true);
  assert.equal(res.actualRate, 3.70);
});

test('15. BP 负数测试 (LPR 3.50% + (-30 BP) = 3.20%)', () => {
  const lpr = 3.50;
  const bp = -30;
  const actualRate = round2(lpr + bp / 100);
  assert.equal(actualRate, 3.20);

  const res = calculateMortgage({
    loanType: 'commercial',
    repaymentMethod: 'equal_payment',
    years: 20,
    commercialPrincipal: 1000000,
    commercialActualRate: actualRate
  });
  assert.equal(res.valid, true);
  assert.equal(res.actualRate, 3.20);
});
