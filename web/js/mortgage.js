(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.MortgageCalculator = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function round2(val) {
    var value = Number(val);
    var parts = String(Math.abs(value)).split('e');
    var shifted = Number(parts[0] + 'e' + (Number(parts[1] || 0) + 2));
    return Math.sign(value) * Math.round(shifted) / 100;
  }

  /**
   * 等额本息 (Equal Payment) 单笔贷款计算
   * P: 本金, annualRate: 年利率(如 3.2 代表 3.2%), n: 期数 (月)
   */
  function calculateEqualPayment(P, annualRate, n) {
    var r = (annualRate / 100) / 12;
    var payment;
    if (r === 0) {
      payment = P / n;
    } else {
      var factor = Math.pow(1 + r, n);
      payment = P * r * factor / (factor - 1);
    }

    var schedule = [];
    var remaining = P;
    var totalInterest = 0;
    var totalPayment = 0;

    for (var i = 1; i <= n; i++) {
      var interest, principal, curPayment;
      if (i < n) {
        interest = round2(remaining * r);
        principal = round2(payment - interest);
        curPayment = round2(principal + interest);
        remaining = round2(remaining - principal);
      } else {
        // 最后一期做金额尾差修正
        principal = round2(remaining);
        interest = round2(remaining * r);
        curPayment = round2(principal + interest);
        remaining = 0;
      }
      totalInterest = round2(totalInterest + interest);
      totalPayment = round2(totalPayment + curPayment);

      schedule.push({
        period: i,
        payment: curPayment,
        principal: principal,
        interest: interest,
        remainingPrincipal: remaining
      });
    }

    return {
      monthlyPayment: schedule[0] ? schedule[0].payment : 0,
      totalInterest: totalInterest,
      totalPayment: totalPayment,
      schedule: schedule
    };
  }

  /**
   * 等额本金 (Equal Principal) 单笔贷款计算
   * P: 本金, annualRate: 年利率(如 3.2 代表 3.2%), n: 期数 (月)
   */
  function calculateEqualPrincipal(P, annualRate, n) {
    var r = (annualRate / 100) / 12;
    var basePrincipal = P / n;

    var schedule = [];
    var remaining = P;
    var totalInterest = 0;
    var totalPayment = 0;

    for (var i = 1; i <= n; i++) {
      var principal, interest, curPayment;
      if (i < n) {
        principal = round2(basePrincipal);
        interest = round2(remaining * r);
        curPayment = round2(principal + interest);
        remaining = round2(remaining - principal);
      } else {
        principal = round2(remaining);
        interest = round2(remaining * r);
        curPayment = round2(principal + interest);
        remaining = 0;
      }
      totalInterest = round2(totalInterest + interest);
      totalPayment = round2(totalPayment + curPayment);

      schedule.push({
        period: i,
        payment: curPayment,
        principal: principal,
        interest: interest,
        remainingPrincipal: remaining
      });
    }

    var firstPayment = schedule[0] ? schedule[0].payment : 0;
    var monthlyDecrease = schedule.length >= 2 ? round2(schedule[0].payment - schedule[1].payment) : 0;

    return {
      firstMonthPayment: firstPayment,
      monthlyDecrease: monthlyDecrease,
      totalInterest: totalInterest,
      totalPayment: totalPayment,
      schedule: schedule
    };
  }

  /**
   * 单项贷款计算调度器
   */
  function calculateSingleLoan(principal, annualRate, months, method) {
    if (method === 'equal_principal') {
      return calculateEqualPrincipal(principal, annualRate, months);
    }
    return calculateEqualPayment(principal, annualRate, months);
  }

  /**
   * 合并两笔贷款明细（组合贷）
   */
  function mergeSchedules(schedA, schedB, n) {
    var merged = [];
    for (var i = 0; i < n; i++) {
      var a = schedA[i] || { payment: 0, principal: 0, interest: 0, remainingPrincipal: 0 };
      var b = schedB[i] || { payment: 0, principal: 0, interest: 0, remainingPrincipal: 0 };

      merged.push({
        period: i + 1,
        payment: round2(a.payment + b.payment),
        principal: round2(a.principal + b.principal),
        interest: round2(a.interest + b.interest),
        remainingPrincipal: round2(a.remainingPrincipal + b.remainingPrincipal),
        commercialPrincipal: a.principal,
        commercialInterest: a.interest,
        fundPrincipal: b.principal,
        fundInterest: b.interest
      });
    }
    return merged;
  }

  /**
   * 年度汇总计算
   */
  function aggregateMortgageByYear(schedule) {
    var yearly = [];
    var currentYear = null;

    for (var i = 0; i < schedule.length; i++) {
      var row = schedule[i];
      var yearIndex = Math.floor((row.period - 1) / 12) + 1;

      if (!currentYear || currentYear.year !== yearIndex) {
        if (currentYear) {
          yearly.push(currentYear);
        }
        currentYear = {
          year: yearIndex,
          payment: 0,
          principal: 0,
          interest: 0,
          remainingPrincipal: row.remainingPrincipal
        };
      }

      currentYear.payment = round2(currentYear.payment + row.payment);
      currentYear.principal = round2(currentYear.principal + row.principal);
      currentYear.interest = round2(currentYear.interest + row.interest);
      currentYear.remainingPrincipal = row.remainingPrincipal;
    }

    if (currentYear) {
      yearly.push(currentYear);
    }
    return yearly;
  }

  /**
   * 房贷计算器主入口
   * 支持 commercial, provident_fund, combined
   */
  function calculateMortgage(params) {
    var loanType = params.loanType || 'commercial';
    var method = params.repaymentMethod || 'equal_payment';
    var months = Math.floor(Number(params.months || (Number(params.years) * 12)));

    if (!Number.isFinite(months) || months <= 0) {
      return { valid: false, error: '贷款期限必须大于 0' };
    }

    if (loanType === 'commercial') {
      var commP = Number(params.commercialPrincipal);
      var commRate = Number(params.commercialActualRate);

      if (!Number.isFinite(commP) || commP <= 0) {
        return { valid: false, error: '商业贷款金额必须大于 0' };
      }
      if (!Number.isFinite(commRate) || commRate < 0) {
        return { valid: false, error: '商业贷款利率不能为负数' };
      }

      var res = calculateSingleLoan(commP, commRate, months, method);
      return {
        valid: true,
        loanType: loanType,
        repaymentMethod: method,
        months: months,
        totalPrincipal: commP,
        totalInterest: res.totalInterest,
        totalPayment: res.totalPayment,
        monthlyPayment: res.monthlyPayment,
        firstMonthPayment: res.firstMonthPayment,
        monthlyDecrease: res.monthlyDecrease,
        actualRate: commRate,
        schedule: res.schedule,
        yearlySchedule: aggregateMortgageByYear(res.schedule)
      };
    } else if (loanType === 'provident_fund') {
      var fundP = Number(params.fundPrincipal);
      var fundRate = Number(params.fundRate);

      if (!Number.isFinite(fundP) || fundP <= 0) {
        return { valid: false, error: '公积金贷款金额必须大于 0' };
      }
      if (!Number.isFinite(fundRate) || fundRate < 0) {
        return { valid: false, error: '公积金贷款利率不能为负数' };
      }

      var resFund = calculateSingleLoan(fundP, fundRate, months, method);
      return {
        valid: true,
        loanType: loanType,
        repaymentMethod: method,
        months: months,
        totalPrincipal: fundP,
        totalInterest: resFund.totalInterest,
        totalPayment: resFund.totalPayment,
        monthlyPayment: resFund.monthlyPayment,
        firstMonthPayment: resFund.firstMonthPayment,
        monthlyDecrease: resFund.monthlyDecrease,
        actualRate: fundRate,
        schedule: resFund.schedule,
        yearlySchedule: aggregateMortgageByYear(resFund.schedule)
      };
    } else if (loanType === 'combined') {
      var cP = Number(params.commercialPrincipal) || 0;
      var cRate = Number(params.commercialActualRate) || 0;
      var fP = Number(params.fundPrincipal) || 0;
      var fRate = Number(params.fundRate) || 0;

      if (cP < 0 || fP < 0) {
        return { valid: false, error: '贷款金额不能为负数' };
      }
      if (cP + fP <= 0) {
        return { valid: false, error: '组合贷款总金额必须大于 0' };
      }
      if (cRate < 0 || fRate < 0) {
        return { valid: false, error: '贷款利率不能为负数' };
      }

      var commRes = cP > 0 ? calculateSingleLoan(cP, cRate, months, method) : { totalInterest: 0, totalPayment: 0, schedule: [] };
      var fRes = fP > 0 ? calculateSingleLoan(fP, fRate, months, method) : { totalInterest: 0, totalPayment: 0, schedule: [] };

      var mergedSchedule = mergeSchedules(commRes.schedule, fRes.schedule, months);
      var totalInterest = round2(commRes.totalInterest + fRes.totalInterest);
      var totalPayment = round2((cP + fP) + totalInterest);

      var firstMonth = mergedSchedule[0] ? mergedSchedule[0].payment : 0;
      var monthlyDec = mergedSchedule.length >= 2 ? round2(mergedSchedule[0].payment - mergedSchedule[1].payment) : 0;

      return {
        valid: true,
        loanType: loanType,
        repaymentMethod: method,
        months: months,
        totalPrincipal: round2(cP + fP),
        totalInterest: totalInterest,
        totalPayment: totalPayment,
        monthlyPayment: method === 'equal_payment' ? firstMonth : undefined,
        firstMonthPayment: method === 'equal_principal' ? firstMonth : undefined,
        monthlyDecrease: method === 'equal_principal' ? monthlyDec : undefined,
        commercialActualRate: cRate,
        fundRate: fRate,
        schedule: mergedSchedule,
        yearlySchedule: aggregateMortgageByYear(mergedSchedule)
      };
    }

    return { valid: false, error: '未知的贷款类型' };
  }

  return {
    round2: round2,
    calculateEqualPayment: calculateEqualPayment,
    calculateEqualPrincipal: calculateEqualPrincipal,
    calculateSingleLoan: calculateSingleLoan,
    calculateMortgage: calculateMortgage,
    aggregateMortgageByYear: aggregateMortgageByYear,
    mergeSchedules: mergeSchedules
  };
});

