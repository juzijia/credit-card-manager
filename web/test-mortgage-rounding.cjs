const test = require('node:test');
const assert = require('node:assert/strict');
const calculator = require('./js/mortgage.js');

test('mortgage amounts round decimal half cents consistently', () => {
  for (const [value, expected] of [
    [100000.125, 100000.13], [1.005, 1.01], [-1.005, -1.01], [1.2349999999999999, 1.23],
  ]) {
    assert.equal(calculator.round2(value), expected);
  }
});

test('equal principal schedule keeps the rounded first payment and reconciles the final principal', () => {
  const schedule = calculator.calculateEqualPrincipal(1200001.50, 0, 12).schedule;
  assert.equal(schedule[0].payment, 100000.13);
  assert.equal(schedule[11].payment, 100000.07);
  assert.equal(schedule.reduce((total, row) => total + Math.round(row.principal * 100), 0), 120000150);
  assert.equal(schedule[11].remainingPrincipal, 0);
});
