from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/web/index.html")
text = path.read_text(encoding="utf-8")

# Keep the list mode dense and aligned while preserving the existing single/double switch.
if "Shared desktop headings" in text:
    print("card management UI already applied")
    sys.exit(0)

old_grid = """          <!-- Minimalist Card Grid -->
          <div :class=\"cardsLayoutMode === 'single' ? 'grid grid-cols-1 gap-3' : 'grid grid-cols-1 xl:grid-cols-2 gap-4'\">"""
new_grid = """          <!-- Shared desktop headings for the dense single-row management view -->
          <div v-if=\"cardsLayoutMode === 'single'\" class=\"hidden xl:grid xl:grid-cols-[minmax(260px,1.62fr)_minmax(140px,1fr)_minmax(105px,.72fr)_minmax(105px,.72fr)_minmax(130px,.82fr)_minmax(150px,.9fr)] items-center gap-x-4 px-5 pb-1.5 -mb-0.5 text-[11px] font-semibold text-slate-500 dark:text-slate-400\">
            <span class=\"pl-[52px]\">信用卡</span>
            <span>授信额度</span>
            <span>还款日</span>
            <span>账单日</span>
            <span>年费</span>
            <span class=\"text-right pr-2\">操作</span>
          </div>

          <!-- Minimalist Card Grid -->
          <div :class=\"cardsLayoutMode === 'single' ? 'grid grid-cols-1 gap-2.5' : 'grid grid-cols-1 xl:grid-cols-2 gap-4'\">"""
if old_grid not in text:
    raise SystemExit("card grid anchor not found")
text = text.replace(old_grid, new_grid, 1)

old_size = """                cardsLayoutMode === 'single'
                  ? 'px-4 py-3.5 sm:px-5 sm:py-4 min-h-[96px]'
                  : 'p-4 min-h-[196px]'"""
new_size = """                cardsLayoutMode === 'single'
                  ? 'px-4 py-3 sm:px-5 sm:py-3 min-h-[82px]'
                  : 'p-4 sm:p-5 min-h-[202px]'"""
if old_size not in text:
    raise SystemExit("card item size anchor not found")
text = text.replace(old_size, new_size, 1)

single_start = text.index("              <!-- Single row layout (compact card-like management list) -->")
double_marker = "              <!-- Double row layout (card cover + key management data) -->"
single_end = text.index(double_marker, single_start)
new_single = r'''              <!-- Single row layout (dense card management list) -->
              <template v-if="cardsLayoutMode === 'single'">
                <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-[minmax(260px,1.62fr)_minmax(140px,1fr)_minmax(105px,.72fr)_minmax(105px,.72fr)_minmax(130px,.82fr)_minmax(150px,.9fr)] items-center gap-x-4 gap-y-3 min-h-[56px]">
                  <div class="min-w-0 flex items-center gap-3">
                    <div class="w-10 h-10 shrink-0 rounded-xl bg-white dark:bg-slate-800 border border-slate-200/80 dark:border-slate-700/80 p-1.5 flex items-center justify-center overflow-hidden shadow-xs">
                      <img v-if="getBankLogoUrl(card.bank_name)" :src="getBankLogoUrl(card.bank_name)" class="w-full h-full object-contain" :alt="card.bank_name">
                      <span v-else class="text-[9px] font-bold text-slate-500 dark:text-slate-400">{{ getBankShort(card.bank_name) }}</span>
                    </div>
                    <div class="min-w-0">
                      <div class="flex items-center gap-2 min-w-0">
                        <h3 class="text-sm font-bold text-slate-900 dark:text-white leading-tight truncate">{{ card.bank_name }}</h3>
                        <span class="shrink-0 px-1.5 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 text-[10px] font-medium max-w-[150px] truncate">{{ card.card_product || '标准卡' }}</span>
                      </div>
                      <div class="mt-1 flex items-center gap-1.5 text-[11px] text-slate-400 font-mono whitespace-nowrap">
                        <span>尾号 {{ card.last4 }}</span>
                        <span class="text-slate-300 dark:text-slate-600">·</span>
                        <span>{{ card.currency || 'CNY' }}</span>
                      </div>
                    </div>
                  </div>

                  <div class="min-w-0">
                    <span class="xl:hidden text-[10px] leading-none text-slate-400 block">授信额度</span>
                    <span v-if="card.credit_limit_minor" class="xl:mt-0 mt-1.5 block font-mono text-sm font-semibold text-slate-900 dark:text-white truncate">¥ {{ formatMoney(card.credit_limit_minor) }}</span>
                    <button v-else @click.stop="openEditCardModal(card)" class="xl:mt-0 mt-1.5 text-xs font-medium text-blue-600 dark:text-blue-400 hover:underline">+ 补充额度</button>
                  </div>

                  <div class="min-w-0">
                    <span class="xl:hidden text-[10px] leading-none text-slate-400 block">还款日</span>
                    <span class="xl:mt-0 mt-1.5 block text-sm font-semibold truncate" :class="card.due_day ? 'text-rose-600 dark:text-rose-400' : 'text-slate-400'">{{ card.due_day ? ('每月 ' + card.due_day + ' 日') : '待设置' }}</span>
                  </div>

                  <div class="min-w-0">
                    <span class="xl:hidden text-[10px] leading-none text-slate-400 block">账单日</span>
                    <span class="xl:mt-0 mt-1.5 block text-sm font-medium text-slate-800 dark:text-slate-200 truncate">{{ card.statement_day ? ('每月 ' + card.statement_day + ' 日') : '待设置' }}</span>
                  </div>

                  <div class="min-w-0">
                    <span class="xl:hidden text-[10px] leading-none text-slate-400 block">年费</span>
                    <span v-if="card.annual_fee_minor === 0" class="xl:mt-0 mt-1.5 block text-xs font-medium text-emerald-600 dark:text-emerald-400 truncate">免年费</span>
                    <span v-else-if="card.annual_fee_minor" class="xl:mt-0 mt-1.5 block font-mono text-xs font-medium text-slate-600 dark:text-slate-300 truncate">¥ {{ formatMoney(card.annual_fee_minor) }} / 年</span>
                    <span v-else class="xl:mt-0 mt-1.5 block text-xs text-slate-400 truncate">待设置</span>
                  </div>

                  <div class="flex items-center justify-end gap-2 shrink-0 md:col-span-2 xl:col-span-1">
                    <button @click.stop="openEditCardModal(card)" class="px-3 py-1.5 rounded-xl bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 text-[11px] font-medium transition whitespace-nowrap">设置</button>
                    <button @click="filterStatementsByCard(card.id)" class="px-3 py-1.5 rounded-xl bg-slate-900 hover:bg-slate-800 text-white dark:bg-slate-700 dark:hover:bg-slate-600 text-[11px] font-medium transition whitespace-nowrap">账单</button>
                  </div>
                </div>
              </template>

'''
text = text[:single_start] + new_single + text[single_end:]

# The double-column mode is the primary visual card-management view: identity on the
# payment-card face, account summary on the right, and only the two high-value actions.
double_start = text.index(double_marker)
double_end_marker = "              </template>\n"
double_end = text.index(double_end_marker, double_start) + len(double_end_marker)
new_double = r'''              <!-- Double row layout (payment card + account summary) -->
              <template v-else>
                <div class="grid grid-cols-1 sm:grid-cols-[minmax(196px,208px)_minmax(0,1fr)] gap-5 h-full items-center">
                  <!-- Card face owns identity: bank / product / last4 -->
                  <div
                    class="relative w-full max-w-[208px] mx-auto sm:mx-0 overflow-hidden rounded-xl border border-white/10 shadow-sm"
                    style="aspect-ratio: 1.586 / 1;"
                    :style="getCardCoverStyle(card)"
                  >
                    <div class="absolute inset-0 bg-gradient-to-br from-white/10 via-transparent to-black/25"></div>
                    <div class="absolute -right-9 -bottom-14 w-40 h-40 rounded-full border border-white/10"></div>
                    <div class="absolute -right-3 -bottom-8 w-28 h-28 rounded-full border border-white/10"></div>
                    <div class="relative z-10 h-full p-3.5 flex flex-col justify-between text-white">
                      <div class="flex items-start gap-2 min-w-0">
                        <div class="w-8 h-8 rounded-lg bg-white/95 p-1.5 flex items-center justify-center shrink-0 shadow-sm">
                          <img v-if="getBankLogoUrl(card.bank_name)" :src="getBankLogoUrl(card.bank_name)" class="w-full h-full object-contain" :alt="card.bank_name">
                          <span v-else class="text-[9px] font-bold text-slate-700">{{ getBankShort(card.bank_name) }}</span>
                        </div>
                        <div class="min-w-0 pt-0.5">
                          <div class="text-sm font-bold leading-tight truncate drop-shadow-sm">{{ card.bank_name }}</div>
                          <div class="mt-1 text-[10px] leading-tight text-white/75 truncate">{{ card.card_product || card.display_name || '信用卡' }}</div>
                        </div>
                      </div>
                      <div class="text-sm font-mono font-semibold tracking-[0.10em] whitespace-nowrap">尾号 {{ card.last4 }}</div>
                    </div>
                  </div>

                  <!-- Right side: scan order is limit -> due day -> statement day -> fee/actions. -->
                  <div class="min-w-0 self-stretch flex flex-col justify-center py-0.5">
                    <div>
                      <span class="text-[11px] leading-none text-slate-400 block">授信额度</span>
                      <span v-if="card.credit_limit_minor" class="mt-2 block font-mono text-[22px] leading-none font-bold tracking-tight text-slate-950 dark:text-white truncate">¥ {{ formatMoney(card.credit_limit_minor).replace(/\.00$/, '') }}</span>
                      <button v-else @click.stop="openEditCardModal(card)" class="mt-2 text-sm font-semibold text-blue-600 dark:text-blue-400 hover:underline">+ 补充额度</button>
                    </div>

                    <div class="mt-5 grid grid-cols-2 gap-x-7">
                      <div class="min-w-0">
                        <span class="text-[11px] leading-none text-slate-400 block">还款日</span>
                        <span class="mt-2 block text-sm font-semibold truncate" :class="card.due_day ? 'text-rose-600 dark:text-rose-400' : 'text-slate-400'">{{ card.due_day ? ('每月 ' + card.due_day + ' 日') : '待设置' }}</span>
                      </div>
                      <div class="min-w-0">
                        <span class="text-[11px] leading-none text-slate-400 block">账单日</span>
                        <span class="mt-2 block text-sm font-semibold text-slate-800 dark:text-slate-200 truncate">{{ card.statement_day ? ('每月 ' + card.statement_day + ' 日') : '待设置' }}</span>
                      </div>
                    </div>

                    <div class="mt-auto pt-3 border-t border-slate-100 dark:border-slate-800/70 flex items-center justify-between gap-3">
                      <div class="min-w-0 text-[11px] text-slate-400 whitespace-nowrap">
                        <span>年费</span>
                        <span v-if="card.annual_fee_minor === 0" class="ml-1.5 font-medium text-emerald-600 dark:text-emerald-400">免年费</span>
                        <span v-else-if="card.annual_fee_minor" class="ml-1.5 font-mono font-medium text-slate-600 dark:text-slate-300">¥ {{ formatMoney(card.annual_fee_minor).replace(/\.00$/, '') }}</span>
                        <span v-else class="ml-1.5 text-slate-400">待设置</span>
                      </div>
                      <div class="flex items-center gap-2 shrink-0">
                        <button
                          @click.stop="openEditCardModal(card)"
                          class="px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 text-[11px] font-medium transition"
                        >
                          设置
                        </button>
                        <button
                          @click="filterStatementsByCard(card.id)"
                          class="px-3 py-1.5 rounded-lg bg-slate-900 hover:bg-slate-800 text-white dark:bg-slate-700 dark:hover:bg-slate-600 text-[11px] font-medium transition"
                        >
                          账单
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </template>
'''
text = text[:double_start] + new_double + text[double_end:]

# Visibility is a low-frequency card setting. Keep it out of the card face/list actions,
# but preserve the existing PATCH behavior by editing the same `hidden` field in settings.
open_anchor = """            annual_fee_charge_date: card.annual_fee_charge_date || '',
            credit_limit_yuan: card.credit_limit_minor ? (card.credit_limit_minor / 100) : null
          };"""
open_replacement = """            annual_fee_charge_date: card.annual_fee_charge_date || '',
            credit_limit_yuan: card.credit_limit_minor ? (card.credit_limit_minor / 100) : null,
            hidden: Boolean(card.hidden)
          };"""
if open_anchor not in text:
    raise SystemExit("card settings open anchor not found")
text = text.replace(open_anchor, open_replacement, 1)

save_anchor = """              annual_fee_policy: editCardForm.value.annual_fee_policy || null,
              annual_fee_charge_date: editCardForm.value.annual_fee_charge_date ? editCardForm.value.annual_fee_charge_date.trim() : null,
              annual_fee_source: editCardForm.value.annual_fee_charge_date ? (editingCard.value.annual_fee_source || 'manual') : null
            };"""
save_replacement = """              annual_fee_policy: editCardForm.value.annual_fee_policy || null,
              annual_fee_charge_date: editCardForm.value.annual_fee_charge_date ? editCardForm.value.annual_fee_charge_date.trim() : null,
              annual_fee_source: editCardForm.value.annual_fee_charge_date ? (editingCard.value.annual_fee_source || 'manual') : null,
              hidden: Boolean(editCardForm.value.hidden)
            };"""
if save_anchor not in text:
    raise SystemExit("card settings save anchor not found")
text = text.replace(save_anchor, save_replacement, 1)

visibility_anchor = """            <p class=\"mt-1 text-[11px] text-slate-400\">用以补充账单未解析出的额度，保存后将自动同步至授信总额度。</p>
          </div>
        </div>

        <div class=\"flex justify-end gap-2 pt-2\">"""
visibility_replacement = r'''            <p class="mt-1 text-[11px] text-slate-400">用以补充账单未解析出的额度，保存后将自动同步至授信总额度。</p>
          </div>

          <!-- Low-frequency visibility control lives in settings, not on every card tile. -->
          <div class="pt-3 border-t border-slate-100 dark:border-slate-800/70 flex items-center justify-between gap-4">
            <div class="min-w-0">
              <div class="text-xs font-medium text-slate-700 dark:text-slate-300">卡片显示</div>
              <p class="mt-1 text-[11px] text-slate-400">隐藏后不参与普通概览与提醒，历史账单仍保留。</p>
            </div>
            <button
              type="button"
              role="switch"
              :aria-checked="!editCardForm.hidden"
              @click="editCardForm.hidden = !editCardForm.hidden"
              :class="editCardForm.hidden ? 'bg-slate-200 dark:bg-slate-700' : 'bg-slate-900 dark:bg-slate-600'"
              class="relative w-10 h-5 rounded-full transition shrink-0"
              :title="editCardForm.hidden ? '当前隐藏，点击恢复显示' : '当前显示，点击隐藏'"
            >
              <span
                :class="editCardForm.hidden ? 'translate-x-0.5' : 'translate-x-5'"
                class="absolute top-0.5 left-0 w-4 h-4 rounded-full bg-white shadow-sm transition-transform"
              ></span>
            </button>
          </div>
        </div>

        <div class="flex justify-end gap-2 pt-2">'''
if visibility_anchor not in text:
    raise SystemExit("card visibility UI anchor not found")
text = text.replace(visibility_anchor, visibility_replacement, 1)

path.write_text(text, encoding="utf-8")
print("card management UI patch applied")
