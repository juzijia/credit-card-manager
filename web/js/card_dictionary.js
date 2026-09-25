(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.CardDictionary = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function formatChangeType(type) {
    const map = {
      'ADD_CARD': '新增卡片',
      'UPDATE_CARD': '修改卡片',
      'DISCONTINUE_CARD': '停发卡片',
      'ADD_ARTWORK': '卡面变化',
      'UPDATE_ARTWORK': '卡面更新',
      'UPDATE_ANNUAL_FEE': '年费变化',
      'UPDATE_BENEFIT': '权益变化',
      'UPDATE_SOURCE': '来源更新',
      'NEEDS_REVIEW': '待人工核实'
    };
    return map[type] || type;
  }

  function getChangeTypeBadgeClass(type) {
    switch (type) {
      case 'ADD_CARD':
        return 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400 border border-emerald-200/50 dark:border-emerald-800/50';
      case 'DISCONTINUE_CARD':
        return 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400 border border-rose-200/50 dark:border-rose-800/50';
      case 'UPDATE_ANNUAL_FEE':
        return 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400 border border-amber-200/50 dark:border-amber-800/50';
      case 'UPDATE_BENEFIT':
        return 'bg-indigo-50 text-indigo-600 dark:bg-indigo-500/10 dark:text-indigo-400 border border-indigo-200/50 dark:border-indigo-800/50';
      case 'ADD_ARTWORK':
      case 'UPDATE_ARTWORK':
        return 'bg-purple-50 text-purple-600 dark:bg-purple-500/10 dark:text-purple-400 border border-purple-200/50 dark:border-purple-800/50';
      default:
        return 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300';
    }
  }

  return {
    formatChangeType: formatChangeType,
    getChangeTypeBadgeClass: getChangeTypeBadgeClass
  };
});

