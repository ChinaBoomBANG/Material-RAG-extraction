<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  result: { type: Object, default: null },
  running: { type: Boolean, default: false },
  schemaText: { type: String, default: '' },
})
const view = ref('table')
const openRec = ref('')
const showDropped = ref(false)
const showExport = ref(false)
const expanded = ref(false)
const expFmt = ref('csv')
const expPath = ref('')
const expBusy = ref(false)
const expMsg = ref('')
const cleaner = ref({
  dedupe: true, drop_unverified: true,
  trim: true, unicode: true, space_in_number: true, strip_unit: false,
  balance_col: true,   // 补余量列：表里没有 Al 时补一列 Al=Bal.
})

watch(() => props.result, () => { openRec.value = '' })

const headers = computed(() => props.result?.headers || [])
const rows = computed(() => (props.result?.rows || []).map((r, i) => ({
  i, cells: r, id: (props.result.row_ids || [])[i] || '',
})))
const records = computed(() => props.result?.records || [])
const dropped = computed(() => props.result?.dropped || [])

const recOf = computed(() => {
  const m = {}
  for (const r of records.value) m[r.record_id] = r
  return m
})

const fieldCount = computed(() => {
  let n = 0
  for (const rec of records.value)
    for (const g of ['composition', 'process', 'microstructure', 'properties'])
      n += Object.keys(rec[g] || {}).length
  return n
})

const unverifiedCount = computed(() => {
  let n = 0
  for (const rec of records.value)
    for (const g of ['composition', 'process', 'microstructure', 'properties'])
      for (const v of Object.values(rec[g] || {})) if (!v.evidence?.verified) n += 1
  return n
})

const groupsOf = (rec) => (
  [['composition', '成分'], ['process', '工艺'],
   ['microstructure', '组织'], ['properties', '性能']]
    .map(([k, label]) => ({ key: k, label, items: Object.entries(rec[k] || {}) }))
    .filter(g => g.items.length)
)

const fmt = (v) => (v === null || v === undefined || v === '') ? '—' : String(v)

const cleanSummary = (rep) => {
  if (!rep) return ''
  const parts = [`扫描 ${rep.scanned_cells} 格`]
  parts.push(rep.changed_cells ? `修正 ${rep.changed_cells} 格` : '无需修正')
  const labels = { trim: '空格', unicode: '全半角', space_in_number: '数字内空格', strip_unit: '重复单位' }
  const by = Object.entries(rep.by_rule || {})
    .map(([k, v]) => `${labels[k] || k} ${v}`).join('、')
  if (by) parts.push(by)
  return parts.join(' · ')
}

async function doExport() {
  if (!props.result || expBusy.value) return
  expBusy.value = true
  expMsg.value = ''
  try {
    const payload = {
      records: props.result.records,
      schema_text: props.schemaText || null,
      clean: cleaner.value,
      excel_path: expFmt.value === 'xlsx' ? (expPath.value.trim() || null) : null,
    }
    if (expFmt.value === 'csv') {
      const r = await api.exportCsv(payload)
      expMsg.value = `已下载 ${r.filename}（${r.rows} 行${r.deduped ? `，去重 ${r.deduped}` : ''}）\n${cleanSummary(r.report)}`
    } else {
      const r = await api.exportXlsx(payload)
      const w = r.write || {}
      expMsg.value = `已写入 ${w.target || expPath.value}（${r.rows} 行）`
        + (w.created ? '\n新建文件' : `\n非预期变更 ${w.unexpected_count ?? '—'} 处`)
        + `\n${cleanSummary(r.clean_report)}`
    }
  } catch (e) {
    expMsg.value = '导出失败：' + e.message
  } finally {
    expBusy.value = false
  }
}
// 数值列右对齐，文本列左对齐——纯数字/带单位数字都算数值
const isNum = (v) => typeof v === 'number' ||
  (typeof v === 'string' && /^[-+]?\d[\d.,]*\s*[%a-zA-Z/³°]*$/.test(v.trim()))
const toggle = (id) => { openRec.value = openRec.value === id ? '' : id }
</script>

<template>
  <section class="panel flex min-h-0 flex-col"
           :class="expanded ? 'fixed left-3 right-3 bottom-3 top-[64px] z-30 shadow-2xl' : ''">
    <header class="panel-head">
      <span class="panel-title">抽取结果</span>
      <div v-if="result" class="flex items-center gap-1">
        <button class="btn btn-ghost h-[24px] px-2 text-[11.5px]"
                :style="view === 'table' ? { color: 'var(--accent)', fontWeight: 600 } : {}"
                @click="view = 'table'">表格</button>
        <button class="btn btn-ghost h-[24px] px-2 text-[11.5px]"
                :style="view === 'card' ? { color: 'var(--accent)', fontWeight: 600 } : {}"
                @click="view = 'card'">卡片</button>

        <button class="btn btn-ghost h-[24px] px-2 text-[11.5px]"
                :title="expanded ? '收起结果区' : '展开到全屏（宽表更好看）'"
                @click="expanded = !expanded">{{ expanded ? '收起' : '展开' }}</button>

        <span class="mx-0.5 h-3.5 w-px" :style="{ background: 'var(--border-strong)' }"></span>

        <div class="relative">
          <button class="btn btn-ghost h-[24px] px-2 text-[11.5px]"
                  :style="showExport ? { color: 'var(--accent)', fontWeight: 600 } : {}"
                  @click="showExport = !showExport">导出</button>

          <div v-if="showExport"
               class="absolute right-0 z-[6] mt-1 w-[300px] rounded-md border p-2.5"
               :style="{ background: 'var(--surface)', borderColor: 'var(--border-strong)',
                         boxShadow: '0 8px 24px rgba(0,0,0,.14)' }">
            <div class="mb-1.5 flex gap-1 rounded-md p-0.5" :style="{ background: 'var(--surface-2)' }">
              <button v-for="f in [['csv', 'CSV'], ['xlsx', 'Excel']]" :key="f[0]"
                      class="h-[24px] flex-1 rounded-[5px] text-[11.5px]"
                      :style="expFmt === f[0]
                        ? { background: 'var(--surface)', fontWeight: 600, color: 'var(--text)' }
                        : { color: 'var(--muted)' }"
                      @click="expFmt = f[0]">{{ f[1] }}</button>
            </div>

            <input v-if="expFmt === 'xlsx'" v-model="expPath" class="field w-full"
                   placeholder="目标 .xlsx 路径（不存在则新建）" />

            <div class="mt-1.5 grid grid-cols-2 gap-x-2 gap-y-1 text-[11px]">
              <label class="flex items-center gap-1"><input v-model="cleaner.dedupe" type="checkbox" />去重</label>
              <label class="flex items-center gap-1"><input v-model="cleaner.drop_unverified" type="checkbox" />剔除未核验</label>
              <label class="flex items-center gap-1"><input v-model="cleaner.trim" type="checkbox" />去多余空格</label>
              <label class="flex items-center gap-1"><input v-model="cleaner.unicode" type="checkbox" />全半角归一</label>
              <label class="flex items-center gap-1"><input v-model="cleaner.space_in_number" type="checkbox" />修数字内空格</label>
              <label class="flex items-center gap-1" title="表里没有 Al 列时，补一列 Al 填 Bal.（余量）">
                <input v-model="cleaner.balance_col" type="checkbox" />补余量列(Al=Bal.)
              </label>
              <span class="col-span-2 text-[10px] muted">
                单位不改动，原样保留；补余量列只在表里没有 Al 时生效
              </span>
            </div>

            <button class="btn btn-primary mt-2 h-8 w-full" :disabled="expBusy" @click="doExport">
              {{ expBusy ? '导出中…' : (expFmt === 'csv' ? '下载 CSV' : '写入 Excel') }}
            </button>
            <div v-if="expMsg" class="mt-1.5 whitespace-pre-wrap text-[10.5px]"
                 :style="{ color: expMsg.startsWith('导出失败') ? 'var(--err)' : 'var(--ok)' }">{{ expMsg }}</div>
            <div class="mt-1 text-[10px] muted">
              清洗只改字符层（空格/全半角），不换算单位、不改数值
            </div>
          </div>
        </div>
      </div>
    </header>

    <div v-if="!result" class="empty flex-1">
      <template v-if="running">抽取进行中，完成后结果会出现在这里</template>
      <template v-else>
        还没有结果<br />左侧选模板与文献，点「开始抽取」
      </template>
    </div>

    <div v-else class="flex min-h-0 flex-1 flex-col">
      <!-- 概览 -->
      <div class="flex flex-wrap items-center gap-1.5 px-3 py-2">
        <span class="chip"><b class="mono" style="color: var(--text)">{{ result.records_count }}</b> 条记录</span>
        <span class="chip"><b class="mono" style="color: var(--text)">{{ fieldCount }}</b> 字段</span>
        <span v-if="unverifiedCount" class="chip"
              :style="{ color: 'var(--warn)', borderColor: 'var(--warn)' }">
          {{ unverifiedCount }} 未核验
        </span>
        <span v-if="dropped.length" class="chip"
              :style="{ color: 'var(--err)', borderColor: 'var(--err)' }">
          {{ dropped.length }} 已过滤
        </span>
        <span v-if="result.stopped" class="chip"
              :style="{ color: 'var(--warn)', borderColor: 'var(--warn)' }"
              title="中途停止：只抽了已完成的部分，剩余块被放弃">
          已提前停止 · {{ result.picked_chunks }} 块中只跑了一部分
        </span>
        <span class="chip mono">{{ result.schema?.name || '内置' }}</span>
        <span class="chip mono">并发 {{ result.workers }}</span>
      </div>
      <div v-if="view === 'table' && headers.length > 8"
           class="px-3 pb-1.5 text-[10.5px] muted">
        共 {{ headers.length }} 列 · 可横向滚动；序号与牌号列固定在左侧，点「展开」看整表
      </div>

      <!-- 表格视图 -->
      <div v-if="view === 'table'" class="min-h-0 flex-1 overflow-auto border-t"
           :style="{ borderColor: 'var(--border)' }">
        <!-- w-max + min-w-full：列宽按内容走，不被容器压扁；超宽时横向滚动 -->
        <table class="w-max min-w-full border-collapse text-[12px]">
          <thead>
            <tr>
              <th class="sticky left-0 top-0 z-[4] w-[42px] min-w-[42px] border-b px-2 py-1.5 text-left font-medium"
                  :style="{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--muted)' }">
                #
              </th>
              <!-- 第一列（牌号）跟着横向滚动一起冻结，滚到右边也能认出是哪一行 -->
              <th v-for="(h, ci) in headers" :key="h"
                  class="sticky top-0 border-b border-l px-2 py-1.5 text-left font-medium"
                  :class="ci === 0 ? 'left-[42px] z-[4] max-w-[136px] min-w-[92px]' : 'z-[2] max-w-[112px]'"
                  :style="{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--muted)' }">
                <span class="block truncate" :title="h">{{ h }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in rows" :key="row.i">
              <tr class="cursor-pointer hover:opacity-95"
                  :style="{ background: openRec === row.id ? 'var(--accent-weak)' : 'transparent' }"
                  @click="toggle(row.id)">
                <td class="sticky left-0 z-[3] border-b px-2 py-1.5 align-top mono"
                    :style="{ borderColor: 'var(--border)', color: 'var(--muted)',
                              background: openRec === row.id ? 'var(--accent-weak)' : 'var(--surface)' }">{{ row.i + 1 }}</td>
                <td v-for="(h, ci) in headers" :key="h"
                    class="border-b border-l px-2 py-1.5 align-top mono"
                    :class="[isNum(row.cells[h]) ? 'text-right whitespace-nowrap' : 'text-left',
                             ci === 0 ? 'sticky left-[42px] z-[3] max-w-[136px] min-w-[92px]' : '']"
                    :style="{ borderColor: 'var(--border)',
                              background: ci === 0
                                ? (openRec === row.id ? 'var(--accent-weak)' : 'var(--surface)')
                                : undefined }">
                  <!-- 文本列限宽截断：真实数据里相名/牌号可能很长，不能把表撑爆 -->
                  <span v-if="!isNum(row.cells[h])" class="block max-w-[220px] truncate"
                        :title="fmt(row.cells[h])">{{ fmt(row.cells[h]) }}</span>
                  <template v-else>{{ fmt(row.cells[h]) }}</template>
                </td>
              </tr>
              <tr v-if="openRec === row.id">
                <td :colspan="headers.length + 1" class="border-b px-3 py-2 align-top"
                    :style="{ borderColor: 'var(--border)', background: 'var(--surface-2)' }">
                  <div class="mb-1 text-[11.5px] muted">
                    证据明细 · {{ recOf[row.id]?.row_label || recOf[row.id]?.alloy || '（无牌号）' }}
                    · p{{ recOf[row.id]?.source_page }}
                  </div>
                  <div v-for="g in groupsOf(recOf[row.id] || {})" :key="g.key" class="mb-1.5">
                    <div class="text-[11px] font-medium" :style="{ color: 'var(--accent)' }">{{ g.label }}</div>
                    <div v-for="[k, v] in g.items" :key="k"
                         class="flex flex-wrap items-baseline gap-x-2 border-t py-[3px]"
                         :style="{ borderColor: 'var(--border)' }">
                      <span class="min-w-[120px] text-[11.5px]">{{ k }}</span>
                      <span class="mono text-[11.5px]">{{ fmt(v.value) }}</span>
                      <span class="mono text-[11px] muted">{{ v.unit }}</span>
                      <span class="text-[10.5px]"
                            :style="{ color: v.evidence?.verified ? 'var(--ok)' : 'var(--err)' }">
                        {{ v.evidence?.verified ? '✓ 已核验' : '✗ 未通过' }}
                      </span>
                      <span class="min-w-0 break-words text-[10.5px] muted" style="max-width: 640px">
                        引文：{{ v.evidence?.quote || '—' }}
                      </span>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="!rows.length" class="empty">本批块没有抽到可用记录</div>
      </div>

      <!-- 卡片视图 -->
      <div v-else class="min-h-0 flex-1 space-y-2 overflow-y-auto border-t p-3"
           :style="{ borderColor: 'var(--border)' }">
        <article v-for="rec in records" :key="rec.record_id" class="rounded-md border p-2.5"
                 :style="{ borderColor: 'var(--border)' }">
          <header class="mb-1.5 flex items-baseline justify-between">
            <span class="text-[13px] font-semibold">{{ rec.alloy || rec.row_label || '（无牌号）' }}</span>
            <span class="chip mono">p{{ rec.source_page }}</span>
          </header>
          <div v-for="g in groupsOf(rec)" :key="g.key" class="mb-1">
            <div class="text-[11px] font-medium" :style="{ color: 'var(--accent)' }">{{ g.label }}</div>
            <div class="grid grid-cols-2 gap-x-3">
              <div v-for="[k, v] in g.items" :key="k"
                   class="flex items-baseline justify-between border-b py-[2px] text-[11.5px]"
                   :style="{ borderColor: 'var(--border)' }">
                <span class="min-w-0 truncate pr-2" :title="k">{{ k }}</span>
                <!-- 值可能是长文本（相名、状态说明），允许换行断词，不横向溢出 -->
                <span class="mono min-w-0 max-w-[62%] break-words text-right">
                  {{ fmt(v.value) }}<span class="ml-1 text-[10.5px] muted">{{ v.unit }}</span>
                </span>
              </div>
            </div>
          </div>
          <div v-if="rec.warnings?.length" class="mt-1 text-[10.5px]" :style="{ color: 'var(--warn)' }">
            {{ rec.warnings.join('；') }}
          </div>
        </article>
        <div v-if="!records.length" class="empty">本批块没有抽到可用记录</div>
      </div>

      <!-- 被过滤的记录 -->
      <div v-if="dropped.length" class="border-t" :style="{ borderColor: 'var(--border)' }">
        <button class="flex w-full items-center justify-between px-3 py-1.5 text-[11.5px]"
                :style="{ color: 'var(--err)' }" @click="showDropped = !showDropped">
          <span>{{ dropped.length }} 条记录被过滤（非样品行 / 无法绑定样品）</span>
          <span>{{ showDropped ? '▾' : '▸' }}</span>
        </button>
        <ul v-if="showDropped" class="max-h-32 overflow-y-auto px-3 pb-2 text-[11px] muted">
          <li v-for="(d, i) in dropped" :key="i" class="border-t py-1" :style="{ borderColor: 'var(--border)' }">
            p{{ d.page }} 「{{ d.row_label || '无标签' }}」— {{ d.reason }}
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>
