(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LprService = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Benchmark LPR rates (China Foreign Exchange Trade System / PBOC)
  var DEFAULT_LPR = {
    oneYear: 3.00,
    fiveYear: 3.50,
    publishedAt: '2026-08-20',
    source: '中国货币网 / 全国银行间同业拆借中心'
  };

  function getLatestLpr() {
    return Object.assign({}, DEFAULT_LPR);
  }

  // Reserved for future network or backend refresh without introducing crawler dependencies
  function refreshLpr() {
    return Promise.resolve(getLatestLpr());
  }

  return {
    DEFAULT_LPR: DEFAULT_LPR,
    getLatestLpr: getLatestLpr,
    refreshLpr: refreshLpr
  };
});

