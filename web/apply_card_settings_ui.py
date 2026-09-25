from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/web/index.html")
text = path.read_text(encoding="utf-8")

if "card-annual-fee-input" in text:
    print("card settings UI already applied")
    sys.exit(0)

# 0) Hide number input spinners globally via CSS (removes unwanted up/down arrows).
style_anchor = "  <style>"
spin_css = """  <style>
    /* Hide number input spin buttons / steppers */
    input[type=number]::-webkit-inner-spin-button,
    input[type=number]::-webkit-outer-spin-button {
      -webkit-appearance: none !important;
      margin: 0 !important;
    }
    input[type=number] {
      -moz-appearance: textfield !important;
      appearance: textfield !important;
    }"""
if style_anchor in text:
    text = text.replace(style_anchor, spin_css, 1)

# 1) Extend the edit form with annual-fee policy type for the select dropdown.
old_form = """        const editCardForm = ref({
          card_product: '',
          annual_fee_cny: null,
          annual_fee_policy: ''
        });"""
new_form = """        const editCardForm = ref({
          card_product: '',
          annual_fee_cny: null,
          annual_fee_policy: '',
          annual_fee_policy_type: ''
        });"""
if old_form not in text:
    raise SystemExit("editCardForm anchor not found")
text = text.replace(old_form, new_form, 1)

# 2) Define named policy types and their linked default descriptions.
policy_anchor = "        const commonAnnualFeePolicies = ["
policy_types = r"""        const annualFeePolicyTypeOptions = [
          { id: 'lifetime', label: '终身 / 有效期内免', text: '卡片有效期内无条件免年费' },
          { id: 'count', label: '消费笔数免', text: '首年免年费，单卡每年刷卡消费满 6 次免次年' },
          { id: 'amount', label: '消费金额免', text: '当年累计消费满 100,000 元免次年' },
          { id: 'count_amount', label: '笔数 + 金额双达标', text: '当年消费满 12 笔且累计金额满 20,000 元免次年' },
          { id: 'points', label: '积分抵扣免', text: '次年扣除 200,000 综合积分抵扣' },
          { id: 'rigid', label: '刚性年费', text: '核卡/下账单即扣，不可减免' },
          { id: 'custom', label: '自定义 / 特殊规则', text: '' }
        ];
"""
if policy_anchor not in text:
    raise SystemExit("annual fee policy list anchor not found")
text = text.replace(policy_anchor, policy_types + policy_anchor, 1)

# 3) Replace card product and annual fee controls with unified combobox and preset pills.
product_label = ">卡种 / 产品</label>"
product_label_pos = text.find(product_label)
if product_label_pos < 0:
    raise SystemExit("card product UI anchor not found")
product_start = text.rfind("          <div>", 0, product_label_pos)

policy_label = ">年费减免政策说明</label>"
policy_label_pos = text.find(policy_label)
if policy_label_pos < 0:
    raise SystemExit("annual fee policy UI anchor not found")
policy_start = text.rfind("          <div>", 0, policy_label_pos)

new_product_and_fee_ui = r"""          <!-- 卡种 / 产品 (combobox with dropdown preset list matching Image 2) -->
          <div>
            <label class="block text-slate-600 dark:text-slate-300 mb-1 font-normal">卡种 / 产品 <span class="text-rose-500">*</span></label>
            <div class="relative">
              <!-- Backdrop to close dropdown on outside click -->
              <div v-if="showCardProductDropdown" class="fixed inset-0 z-30" @click="showCardProductDropdown = false"></div>

              <div class="relative z-30 flex items-center">
                <input
                  v-model="editCardForm.card_product"
                  @focus="onCardProductFocus"
                  @input="onCardProductInput"
                  placeholder="可直接输入自定义卡种"
                  class="w-full pl-3.5 pr-9 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-xs text-slate-900 dark:text-white focus:outline-none focus:border-blue-500 transition placeholder:text-slate-400"
                />
                <button
                  v-if="editingCardProducts.length"
                  type="button"
                  @click="toggleCardProductDropdown"
                  class="absolute right-2.5 p-1 text-slate-700 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white transition"
                  tabindex="-1"
                >
                  <svg class="w-3.5 h-3.5 transition-transform" :class="showCardProductDropdown ? 'rotate-180' : ''" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M7 10l5 5 5-5z" />
                  </svg>
                </button>
              </div>

              <!-- Preset products popover with top pointer notch matching Image 2 -->
              <div
                v-if="showCardProductDropdown && availableCardProducts.length"
                class="absolute left-0 z-40 mt-2 min-w-[200px] max-h-52 overflow-y-auto bg-white dark:bg-[#111625] border border-slate-200 dark:border-slate-700 rounded-2xl shadow-xl py-1.5 px-1 text-xs"
              >
                <!-- Top pointer notch -->
                <div class="absolute -top-1.5 left-6 w-3 h-3 bg-white dark:bg-[#111625] border-t border-l border-slate-200 dark:border-slate-700 rotate-45"></div>
                <div
                  v-for="p in availableCardProducts"
                  :key="p.name"
                  @click="selectCardProductPreset(p)"
                  class="relative z-10 px-3 py-2 rounded-lg cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/80 text-slate-800 dark:text-slate-200 transition font-normal"
                >
                  {{ p.name }}
                </div>
              </div>
            </div>
          </div>

          <!-- 年费金额 (元/年) with presets below matching Image 2, stepper arrows removed -->
          <div>
            <label class="block text-slate-600 dark:text-slate-300 mb-1 font-normal">年费金额 (元/年)</label>
            <input
              id="card-annual-fee-input"
              v-model.number="editCardForm.annual_fee_cny"
              type="number"
              placeholder="输入年费金额"
              class="w-full px-3.5 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl font-mono text-xs text-slate-900 dark:text-white focus:outline-none focus:border-blue-500 transition [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
              style="-moz-appearance: textfield; appearance: textfield;"
            />
            <div class="flex items-center flex-wrap gap-1.5 mt-2">
              <span class="text-xs text-slate-400 dark:text-slate-500 mr-1">快捷档位:</span>
              <button
                v-for="tier in [180, 360, 480, 600, 800]"
                :key="tier"
                type="button"
                @click="editCardForm.annual_fee_cny = tier"
                :class="editCardForm.annual_fee_cny === tier ? 'bg-slate-900 text-white dark:bg-slate-700 font-medium' : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700'"
                class="px-2.5 py-1 rounded-md text-xs transition"
              >{{ tier }}</button>
              <button
                type="button"
                @click="focusAnnualFeeInput"
                :class="(![180, 360, 480, 600, 800].includes(editCardForm.annual_fee_cny) && editCardForm.annual_fee_cny !== null && editCardForm.annual_fee_cny !== '') ? 'bg-slate-900 text-white dark:bg-slate-700 font-medium' : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700'"
                class="px-2.5 py-1 rounded-md text-xs transition"
              >自定义</button>
            </div>
          </div>
"""
text = text[:product_start] + new_product_and_fee_ui + text[policy_start:]

# 4) Replace the annual-fee policy block with a compact type select + editable description input (removed '选择减免规则...' placeholder option).
policy_label = ">年费减免政策说明</label>"
policy_label_pos = text.find(policy_label)
if policy_label_pos < 0:
    raise SystemExit("annual fee policy UI anchor not found")
policy_start = text.rfind("          <div>", 0, policy_label_pos)
policy_end = text.find("          <!-- 年费收取时间设置 -->", policy_label_pos)
if policy_end < 0:
    raise SystemExit("annual fee policy UI end anchor not found")

new_policy_ui = r"""          <div>
            <label class="block text-slate-600 dark:text-slate-300 mb-1 font-normal">年费减免</label>
            <div class="space-y-2">
              <select
                v-model="editCardForm.annual_fee_policy_type"
                @change="onSelectAnnualFeePolicyType($event.target.value)"
                class="w-full px-3 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-xs text-slate-700 dark:text-slate-200 focus:outline-none focus:border-blue-500 transition"
              >
                <option v-for="item in annualFeePolicyTypeOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select>
              <input
                v-model="editCardForm.annual_fee_policy"
                placeholder="输入资产达标免、分期免或其他特殊规则"
                class="w-full px-3.5 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-xs text-slate-900 dark:text-white focus:outline-none focus:border-blue-500 transition"
              />
            </div>
          </div>
"""
text = text[:policy_start] + new_policy_ui + text[policy_end:]

# 5) Replace rigid month/day selects with calendar-style month/day pickers that auto-close on selection.
charge_start = text.find("          <!-- 年费收取时间设置 -->")
charge_end = text.find("          <!-- 授信额度补充设置 -->", charge_start)
if charge_start < 0 or charge_end < 0:
    raise SystemExit("charge schedule UI anchor not found")

new_charge_ui = r"""          <!-- 年费收取时间设置 -->
          <div>
            <label class="block text-slate-600 dark:text-slate-300 font-normal mb-1.5">年费收取时间</label>
            <!-- Backdrop when any schedule picker is open -->
            <div v-if="activeFeeSchedulePicker" class="fixed inset-0 z-30" @click="activeFeeSchedulePicker = null"></div>

            <div class="grid grid-cols-2 gap-2 relative z-30">
              <!-- Month picker -->
              <div class="relative">
                <button
                  type="button"
                  @click="activeFeeSchedulePicker = activeFeeSchedulePicker === 'month' ? null : 'month'"
                  class="w-full px-3 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-xs text-slate-700 dark:text-slate-200 flex items-center justify-between gap-2 select-none"
                >
                  <span>{{ annualFeeMonthLabel() }}</span>
                  <svg class="w-3.5 h-3.5 text-slate-400 transition-transform" :class="activeFeeSchedulePicker === 'month' ? 'rotate-180' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                </button>
                <div v-if="activeFeeSchedulePicker === 'month'" class="absolute left-0 z-40 mt-2 w-64 p-3 rounded-xl bg-white dark:bg-[#111625] border border-slate-200 dark:border-slate-700 shadow-xl">
                  <div class="grid grid-cols-4 gap-1.5">
                    <button
                      v-for="m in 12"
                      :key="m"
                      type="button"
                      @click="chooseAnnualFeeMonth(m)"
                      :class="annualFeeMonth() === m ? 'bg-slate-900 text-white dark:bg-slate-700' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800'"
                      class="h-8 rounded-lg text-[11px] font-medium transition"
                    >{{ m }}月</button>
                  </div>
                  <div class="mt-2.5 pt-2 border-t border-slate-100 dark:border-slate-800 flex justify-end">
                    <button type="button" @click="clearAnnualFeeSchedule" class="text-[11px] text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">清除</button>
                  </div>
                </div>
              </div>

              <!-- Day picker: month only means billing day; explicit day means fixed date -->
              <div class="relative" :class="annualFeeMonth() ? '' : 'pointer-events-none opacity-55'">
                <button
                  type="button"
                  :disabled="!annualFeeMonth()"
                  @click="activeFeeSchedulePicker = activeFeeSchedulePicker === 'day' ? null : 'day'"
                  class="w-full px-3 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-xs text-slate-700 dark:text-slate-200 flex items-center justify-between gap-2 select-none"
                >
                  <span>{{ annualFeeDayLabel() }}</span>
                  <svg class="w-3.5 h-3.5 text-slate-400 transition-transform" :class="activeFeeSchedulePicker === 'day' ? 'rotate-180' : ''" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                </button>
                <div v-if="activeFeeSchedulePicker === 'day'" class="absolute right-0 z-40 mt-2 w-[300px] p-3 rounded-xl bg-white dark:bg-[#111625] border border-slate-200 dark:border-slate-700 shadow-xl">
                  <button
                    type="button"
                    @click="chooseAnnualFeeDay(null)"
                    :class="annualFeeDay() === null ? 'bg-slate-900 text-white dark:bg-slate-700' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800'"
                    class="w-full h-8 rounded-lg text-[11px] font-medium transition mb-2"
                  >账单日</button>
                  <div class="grid grid-cols-7 gap-1">
                    <button
                      v-for="d in 31"
                      :key="d"
                      type="button"
                      :disabled="d > annualFeeMaxDay()"
                      @click="chooseAnnualFeeDay(d)"
                      :class="annualFeeDay() === d ? 'bg-slate-900 text-white dark:bg-slate-700' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 disabled:opacity-25 disabled:cursor-not-allowed'"
                      class="h-8 rounded-md text-[11px] font-medium transition"
                    >{{ d }}</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
"""
text = text[:charge_start] + new_charge_ui + text[charge_end:]

# 5.1) Remove the credit limit supplementary explanatory text as requested.
limit_desc = '<p class="mt-1 text-[11px] text-slate-400">用以补充账单未解析出的额度，保存后将自动同步至授信总额度。</p>'
if limit_desc in text:
    text = text.replace(limit_desc, "", 1)

# 5.2) Annual fee ledger table visual & text refinements:
# - Remove the '手动填报' badge completely
manual_badge = '<span v-else class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-400" title="手动填报">手动填报</span>'
text = text.replace(manual_badge, "")

# - Move '/ 年' to column header: '年费金额' -> '年费金额 (元/年)'
text = text.replace(
    '<th class="py-3 px-4">年费金额</th>',
    '<th class="py-3 px-4">年费金额 (元/年)</th>',
    1
)

# - '收取扣款时间' -> '扣款时间' in table header and subtitle
text = text.replace(
    '<th class="py-3 px-4">收取扣款时间</th>',
    '<th class="py-3 px-4">扣款时间</th>',
    1
)
text = text.replace(
    '统一追踪各卡年费金额、收取扣款时间与减免达标规则',
    '统一追踪各卡年费金额、扣款时间与减免达标规则',
    1
)

# - In annual fee cell: remove redundant ' / 年'
text = text.replace(
    '¥ {{ (card.annual_fee_minor / 100).toFixed(2) }} / 年',
    '¥ {{ (card.annual_fee_minor / 100).toFixed(2) }}',
    1
)

# - In charge date cell: format date e.g. 06-12 -> 6月12日
text = text.replace(
    '<span class="font-medium text-slate-800 dark:text-slate-200">{{ card.annual_fee_charge_date }}</span>',
    '<span class="font-medium text-slate-800 dark:text-slate-200">{{ formatAnnualFeeChargeDate(card.annual_fee_charge_date) }}</span>',
    1
)

# - Action button: '设置年费/额度' -> '设置'
text = text.replace('设置年费/额度', '设置', 1)

# 6) Initialise dropdown/modal states and inferred policy type whenever a card is opened.
if "            hidden: Boolean(card.hidden)\n          };\n        }" in text:
    old_open_tail = """            hidden: Boolean(card.hidden)
          };
        }"""
    new_open_tail = """            hidden: Boolean(card.hidden)
          };
          showCardProductDropdown.value = false;
          isFilteringCardProducts.value = false;
          activeFeeSchedulePicker.value = null;
        }"""
else:
    old_open_tail = """            credit_limit_yuan: card.credit_limit_minor ? (card.credit_limit_minor / 100) : null
          };
        }"""
    new_open_tail = """            credit_limit_yuan: card.credit_limit_minor ? (card.credit_limit_minor / 100) : null
          };
          showCardProductDropdown.value = false;
          isFilteringCardProducts.value = false;
          activeFeeSchedulePicker.value = null;
        }"""
if old_open_tail not in text:
    raise SystemExit("openEditCardModal tail anchor not found")
text = text.replace(old_open_tail, new_open_tail, 1)

old_open = """            annual_fee_policy: card.annual_fee_policy || '',
            annual_fee_charge_date: card.annual_fee_charge_date || '',"""
new_open = """            annual_fee_policy: card.annual_fee_policy || '',
            annual_fee_policy_type: inferAnnualFeePolicyType(card.annual_fee_policy || ''),
            annual_fee_charge_date: card.annual_fee_charge_date || '',"""
if old_open not in text:
    raise SystemExit("openEditCardModal annual fee fields anchor not found")
text = text.replace(old_open, new_open, 1)

# 7) Add policy-linkage, card product combobox, and annual-fee schedule helpers.
old_handler = """        function onSelectAnnualFeePolicyPreset(val) {
          if (!val) return;
          editCardForm.value.annual_fee_policy = val;
        }

        function normalizedCustomCardOrder() {"""
new_handler = r"""        function inferAnnualFeePolicyType(policy) {
          const value = String(policy || '').trim();
          if (!value) return 'lifetime';
          const exact = annualFeePolicyTypeOptions.find(item => item.text && item.text === value);
          if (exact) return exact.id;
          if (/积分/.test(value)) return 'points';
          if (/刚性|不可减免|不可免/.test(value)) return 'rigid';
          if (/终身|有效期内|无条件免/.test(value)) return 'lifetime';
          const hasCount = /(\d+)\s*(笔|次)/.test(value) || /消费笔数|刷卡.*(笔|次)/.test(value);
          const hasAmount = /(\d[\d,.]*)\s*元/.test(value) || /消费金额|累计金额|累计消费/.test(value);
          if (hasCount && hasAmount) return 'count_amount';
          if (hasCount) return 'count';
          if (hasAmount) return 'amount';
          return 'custom';
        }

        const activeFeeSchedulePicker = ref(null);
        const showCardProductDropdown = ref(false);
        const isFilteringCardProducts = ref(false);

        const availableCardProducts = computed(() => {
          if (!editingCardProducts.value || !editingCardProducts.value.length) return [];
          if (!isFilteringCardProducts.value) return editingCardProducts.value;
          const query = (editCardForm.value.card_product || '').trim().toLowerCase();
          if (!query) return editingCardProducts.value;
          const matched = editingCardProducts.value.filter(p => p.name.toLowerCase().includes(query));
          return matched.length > 0 ? matched : editingCardProducts.value;
        });

        function onCardProductFocus() {
          if (editingCardProducts.value && editingCardProducts.value.length) {
            isFilteringCardProducts.value = false;
            showCardProductDropdown.value = true;
          }
        }

        function onCardProductInput() {
          isFilteringCardProducts.value = true;
          showCardProductDropdown.value = true;
        }

        function toggleCardProductDropdown() {
          if (!showCardProductDropdown.value) {
            isFilteringCardProducts.value = false;
            showCardProductDropdown.value = true;
          } else {
            showCardProductDropdown.value = false;
          }
        }

        function selectCardProductPreset(prod) {
          if (!prod) return;
          editCardForm.value.card_product = prod.name;
          const fee = prod.annual_fee_cny !== undefined ? prod.annual_fee_cny : (prod.annual_fee_minor !== undefined ? prod.annual_fee_minor / 100 : undefined);
          if (fee !== undefined) {
            editCardForm.value.annual_fee_cny = fee;
          }
          if (prod.policy) {
            editCardForm.value.annual_fee_policy = prod.policy;
            editCardForm.value.annual_fee_policy_type = inferAnnualFeePolicyType(prod.policy);
          }
          showCardProductDropdown.value = false;
          isFilteringCardProducts.value = false;
        }

        function focusAnnualFeeInput() {
          const el = document.getElementById('card-annual-fee-input');
          if (el) {
            el.focus();
            el.select();
          }
        }

        function onSelectAnnualFeePolicyType(typeId) {
          if (!typeId) return;
          const opt = annualFeePolicyTypeOptions.find(o => o.id === typeId);
          if (!opt) return;
          if (opt.text) {
            editCardForm.value.annual_fee_policy = opt.text;
          }
        }

        function parseAnnualFeeScheduleParts() {
          const val = String(editCardForm.value.annual_fee_charge_date || '').trim();
          if (!val) return { month: null, day: null };
          const mmdd = val.match(/^(\d{1,2})[-/](\d{1,2})$/);
          if (mmdd) {
            const m = Number(mmdd[1]);
            const d = Number(mmdd[2]);
            if (m >= 1 && m <= 12 && d >= 1 && d <= 31) {
              return { month: m, day: d };
            }
          }
          const mChineseDay = val.match(/(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]/);
          if (mChineseDay) {
            const m = Number(mChineseDay[1]);
            const d = Number(mChineseDay[2]);
            if (m >= 1 && m <= 12 && d >= 1 && d <= 31) {
              return { month: m, day: d };
            }
          }
          const mOnly = val.match(/(\d{1,2})\s*月/);
          if (mOnly) {
            const m = Number(mOnly[1]);
            if (m >= 1 && m <= 12) {
              return { month: m, day: null };
            }
          }
          const numOnly = val.match(/^(\d{1,2})$/);
          if (numOnly) {
            const m = Number(numOnly[1]);
            if (m >= 1 && m <= 12) {
              return { month: m, day: null };
            }
          }
          return { month: null, day: null };
        }

        function annualFeeMonth() {
          return parseAnnualFeeScheduleParts().month;
        }

        function annualFeeDay() {
          return parseAnnualFeeScheduleParts().day;
        }

        function annualFeeMaxDay(month) {
          const m = month || annualFeeMonth();
          if (!m) return 31;
          if (m === 2) return 29;
          if ([4, 6, 9, 11].includes(m)) return 30;
          return 31;
        }

        function setAnnualFeeMonth(month) {
          if (!Number.isInteger(month) || month < 1 || month > 12) return;
          const currentDay = annualFeeDay();
          if (currentDay && currentDay <= annualFeeMaxDay(month)) {
            editCardForm.value.annual_fee_charge_date = String(month).padStart(2, '0') + '-' + String(currentDay).padStart(2, '0');
          } else {
            editCardForm.value.annual_fee_charge_date = '每年 ' + month + ' 月';
          }
        }

        function setAnnualFeeDay(dayValue) {
          const month = annualFeeMonth();
          if (!month) return;
          if (dayValue === null || dayValue === '' || dayValue === undefined) {
            editCardForm.value.annual_fee_charge_date = '每年 ' + month + ' 月';
            return;
          }
          const day = Number(dayValue);
          if (!Number.isInteger(day) || day < 1 || day > annualFeeMaxDay(month)) return;
          editCardForm.value.annual_fee_charge_date = String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0');
        }

        function chooseAnnualFeeMonth(m) {
          setAnnualFeeMonth(m);
          activeFeeSchedulePicker.value = null;
        }

        function chooseAnnualFeeDay(d) {
          setAnnualFeeDay(d);
          activeFeeSchedulePicker.value = null;
        }

        function clearAnnualFeeSchedule() {
          editCardForm.value.annual_fee_charge_date = '';
          activeFeeSchedulePicker.value = null;
        }

        function annualFeeMonthLabel() {
          const month = annualFeeMonth();
          return month ? (month + '月') : '选择月份';
        }

        function annualFeeDayLabel() {
          const day = annualFeeDay();
          return day ? (day + '日') : '账单日';
        }

        function formatAnnualFeeChargeDate(val) {
          if (!val) return '';
          const str = String(val).trim();
          const mdMatch = str.match(/^(?:\d{4}-)?(\d{1,2})[-/](\d{1,2})$/);
          if (mdMatch) {
            const m = parseInt(mdMatch[1], 10);
            const d = parseInt(mdMatch[2], 10);
            if (m >= 1 && m <= 12 && d >= 1 && d <= 31) {
              return m + '月' + d + '日';
            }
          }
          const mChineseDay = str.match(/^(?:每年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?$/);
          if (mChineseDay) {
            const m = parseInt(mChineseDay[1], 10);
            const d = parseInt(mChineseDay[2], 10);
            if (m >= 1 && m <= 12 && d >= 1 && d <= 31) {
              return m + '月' + d + '日';
            }
          }
          const mChinese = str.match(/^(?:每年\s*)?(\d{1,2})\s*月$/);
          if (mChinese) {
            const m = parseInt(mChinese[1], 10);
            if (m >= 1 && m <= 12) {
              return '每年 ' + m + ' 月';
            }
          }
          const numOnly = str.match(/^(\d{1,2})$/);
          if (numOnly) {
            const m = parseInt(numOnly[1], 10);
            if (m >= 1 && m <= 12) {
              return '每年 ' + m + ' 月';
            }
          }
          return str;
        }

        function normalizedCustomCardOrder() {"""
if old_handler not in text:
    raise SystemExit("annual fee policy handler anchor not found")
text = text.replace(old_handler, new_handler, 1)

# 8) Expose the new helpers to the template.
old_return = """          commonAnnualFeePolicies,
          onSelectAnnualFeePreset,
          onSelectAnnualFeePolicyPreset,"""
new_return = """          commonAnnualFeePolicies,
          annualFeePolicyTypeOptions,
          onSelectAnnualFeePreset,
          onSelectAnnualFeePolicyType,
          annualFeeMonth,
          annualFeeDay,
          annualFeeMaxDay,
          setAnnualFeeMonth,
          setAnnualFeeDay,
          chooseAnnualFeeMonth,
          chooseAnnualFeeDay,
          clearAnnualFeeSchedule,
          annualFeeMonthLabel,
          annualFeeDayLabel,
          formatAnnualFeeChargeDate,
          activeFeeSchedulePicker,
          showCardProductDropdown,
          isFilteringCardProducts,
          availableCardProducts,
          onCardProductFocus,
          onCardProductInput,
          toggleCardProductDropdown,
          selectCardProductPreset,
          focusAnnualFeeInput,"""
if old_return not in text:
    raise SystemExit("setup return anchor not found")
text = text.replace(old_return, new_return, 1)

path.write_text(text, encoding="utf-8")
print("card settings UI patch successfully generated")
