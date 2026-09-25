(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.InstallmentCalculator = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function round2(val) {
    return Math.round((Number(val) + Number.EPSILON) * 100) / 100;
  }

  function solveMonthlyIRR(principal, payment, n, tolerance = 1e-9, maxIterations = 100) {
    if (principal <= 0 || payment <= 0 || n <= 0) {
      return null;
    }
    const totalRepayment = payment * n;
    if (totalRepayment < principal - 1e-7) {
      return null;
    }
    if (Math.abs(totalRepayment - principal) < 1e-7) {
      return 0;
    }

    function pv(r) {
      if (r === 0) return totalRepayment - principal;
      return payment * (1 - Math.pow(1 + r, -n)) / r - principal;
    }

    let low = 0;
    let high = 1.0;
    while (pv(high) > 0 && high < 100) {
      high *= 2;
    }
    if (pv(high) > 0) {
      return null;
    }

    for (let i = 0; i < maxIterations; i++) {
      const mid = (low + high) / 2;
      const val = pv(mid);
      if (Math.abs(val) < tolerance || (high - low) < tolerance) {
        return mid;
      }
      if (val > 0) {
        low = mid;
      } else {
        high = mid;
      }
    }
    return (low + high) / 2;
  }

  function solveMonthlyIRR_upfront(principal, totalFee, n, tolerance = 1e-9, maxIterations = 100) {
    if (principal <= 0 || n <= 0) {
      return null;
    }
    if (totalFee < -1e-7) {
      return null;
    }
    if (Math.abs(totalFee) < 1e-7) {
      return 0;
    }

    const pPerPeriod = principal / n;

    function pv(r) {
      if (r === 0) return totalFee;
      return (pPerPeriod * (1 - Math.pow(1 + r, -n)) / r) + (totalFee / (1 + r)) - principal;
    }

    let low = 0;
    let high = 1.0;
    while (pv(high) > 0 && high < 100) {
      high *= 2;
    }
    if (pv(high) > 0) {
      return null;
    }

    for (let i = 0; i < maxIterations; i++) {
      const mid = (low + high) / 2;
      const val = pv(mid);
      if (Math.abs(val) < tolerance || (high - low) < tolerance) {
        return mid;
      }
      if (val > 0) {
        low = mid;
      } else {
        high = mid;
      }
    }
    return (low + high) / 2;
  }

  function generateSchedule(principal, totalFee, n, feeChargeMode = 'per_period') {
    const schedule = [];
    let cumPrincipal = 0;
    let cumFee = 0;

    for (let i = 1; i <= n; i++) {
      let p, f;
      if (i < n) {
        p = round2(principal / n);
      } else {
        p = round2(principal - cumPrincipal);
      }
      cumPrincipal += p;

      if (feeChargeMode === 'first_period') {
        f = i === 1 ? round2(totalFee) : 0;
      } else {
        if (i < n) {
          f = round2(totalFee / n);
        } else {
          f = round2(totalFee - cumFee);
        }
      }
      cumFee += f;

      const payment = round2(p + f);
      const remaining = round2(Math.max(0, principal - cumPrincipal));

      schedule.push({
        period: i,
        principal: p,
        fee: f,
        payment: payment,
        remainingPrincipal: remaining
      });
    }
    return schedule;
  }

  function calculateInstallment(params) {
    const { principal, periods, feeMode, feeValue, feeChargeMode = 'per_period' } = params || {};

    const P = Number(principal);
    const n = Math.floor(Number(periods));
    const v = Number(feeValue);

    if (!Number.isFinite(P) || P <= 0) {
      return { valid: false, error: '分期金额必须大于 0' };
    }
    if (!Number.isFinite(n) || n <= 0) {
      return { valid: false, error: '分期期数必须大于 0' };
    }
    if (!Number.isFinite(v) || v < 0) {
      return { valid: false, error: '费用不能为负数' };
    }

    let feePerPeriod = 0;
    let totalFee = 0;
    let payment = 0;
    let totalRepayment = 0;

    switch (feeMode) {
      case 'fee_rate_per_period': {
        const r = v / 100;
        feePerPeriod = P * r;
        totalFee = feePerPeriod * n;
        payment = P / n + feePerPeriod;
        totalRepayment = P + totalFee;
        break;
      }
      case 'total_fee_rate': {
        const R = v / 100;
        totalFee = P * R;
        feePerPeriod = totalFee / n;
        payment = P / n + feePerPeriod;
        totalRepayment = P + totalFee;
        break;
      }
      case 'fee_per_period': {
        feePerPeriod = v;
        totalFee = feePerPeriod * n;
        payment = P / n + feePerPeriod;
        totalRepayment = P + totalFee;
        break;
      }
      case 'payment_per_period': {
        payment = v;
        totalRepayment = payment * n;
        if (totalRepayment < P - 1e-7) {
          return { valid: false, error: '每期还款总额不能小于分期本金' };
        }
        totalFee = totalRepayment - P;
        if (Math.abs(totalFee) < 1e-7) {
          totalFee = 0;
        }
        feePerPeriod = totalFee / n;
        break;
      }
      default:
        return { valid: false, error: '未知的费用输入方式' };
    }

    const totalFeeRate = P > 0 ? totalFee / P : 0;
    const nominalAnnualRate = P > 0 && n > 0 ? (totalFee / P) * (12 / n) : 0;

    let monthlyIRR;
    if (feeChargeMode === 'first_period') {
      monthlyIRR = solveMonthlyIRR_upfront(P, totalFee, n);
    } else {
      monthlyIRR = solveMonthlyIRR(P, payment, n);
    }

    if (monthlyIRR === null || !Number.isFinite(monthlyIRR)) {
      return { valid: false, error: '真实利率计算无有效解' };
    }

    const apr = monthlyIRR * 12;
    const ear = Math.pow(1 + monthlyIRR, 12) - 1;

    const schedule = generateSchedule(P, totalFee, n, feeChargeMode);

    return {
      valid: true,
      principal: P,
      periods: n,
      feeMode,
      feeValue: v,
      feeChargeMode,
      feePerPeriod,
      totalFee,
      payment,
      firstPeriodPayment: schedule[0]?.payment || payment,
      subsequentPayment: schedule[1]?.payment || round2(P / n),
      totalRepayment,
      totalFeeRate,
      nominalAnnualRate,
      monthlyIRR,
      apr,
      ear,
      schedule
    };
  }

  return {
    round2,
    solveMonthlyIRR,
    solveMonthlyIRR_upfront,
    generateSchedule,
    calculateInstallment
  };
});
