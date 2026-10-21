<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  docs: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  mode: { type: String, default: 'table' },
  workers: { type: Number, default: 2 },
  maxChunks: { type: Number, default: 0 },
  excelPath: { type: String, default: '' },
  running: { type: Boolean, default: false },
  stopping: { type: Boolean, default: false },
  progress: { type: Object, default: null },
  status: { type: Object, default: null },
  elapsedSec: { type: Number, default: 0 },    // 由 App 顶层计时器驱动，跨视图存活
  doneChunks: { type: Number, default: 0 },    // 上一批已抽块数（继续抽取用）
})
const emit = defineEmits(['update:selected', 'update:mode', 'update:workers',
  'update:maxChunks', 'update:excelPath', 'run', 'continue', 'stop'])

const advanced = ref(false)
const plan = ref(null)
const planErr = ref('')

const allChecked = computed(() =>
  props.docs.length > 0 && props.selected.length === props.docs.length)

// ---------------- 文献列表：拖底部手柄可拉开 ----------------
const LIST_H0 = 150
const listH = ref(LIST_H0)
const dragging = ref(false)
let dragY = 0
let dragH = 0

function startDrag(e) {
  dragging.value = true
  dragY = e.clientY
  dragH = listH.value
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', endDrag)
  e.preventDefault()
}
function onDrag(e) {
  if (!dragging.value) return
  listH.value = Math.max(84, Math.min(560, dragH + (e.clientY - dragY)))
}
function endDrag() {
  dragging.value = false
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', endDrag)
}
onBeforeUnmount(endDrag)

// ---- 开跑前的规模预估：只数块，不加载模型，秒回 ----
async function loadPlan() {
  try {
    plan.value = await api.extractPlan({ doc_ids: props.selected, mode: props.mode })
    planErr.value = ''
  } catch (e) {
    plan.value = null
    planErr.value = e.message
  }
}
watch(() => [props.selected.join(','), props.mode], loadPlan, { immediate: true })

// 实测并发加速比（本机 LM Studio，多次测量取保守值），用于换算 sec_per_chunk
const SPEEDUP = { 1: 1.0, 2: 1.5, 3: 1.6, 4: 1.6 }
const spc = computed(() => {
  if (!plan.value) return null
  const base = plan.value.sec_per_chunk || 60
  const from = SPEEDUP[plan.value.at_workers] || 1.74
  const to = SPEEDUP[props.workers] || 1.74
  return base * from / to
})
// 该范围下总共有多少块，以及受上限截断后真正会送进模型的块数
const planTotal = computed(() => (plan.value ? plan.value.picked || 0 : 0))
const planPicked = computed(() =>
  props.maxChunks > 0 ? Math.min(planTotal.value, props.maxChunks) : planTotal.value)
const etaPlan = computed(() => {
  if (!spc.value || !planPicked.value) return ''
  const s = spc.value * planPicked.value
  if (s < 60) return `${Math.round(s)} 秒`
  if (s < 3600) return `${Math.round(s / 60)} 分钟`
  return `${(s / 3600).toFixed(1)} 小时`
})
// 规模预警：按量级给不同提示，避免一上来就跑几小时
const warnLevel = computed(() => {
  const n = planPicked.value
  if (n > 150) return 'high'
  if (n > 60) return 'mid'
  return ''
})

const maxPresets = [1, 3, 10, 30]
function setMax(n) {
  emit('update:maxChunks', n)
}
function onMaxInput(e) {
  const v = parseInt(e.target.value, 10)
  emit('update:maxChunks', Number.isFinite(v) && v > 0 ? v : 0)
}

const toggleAll = () => {
  emit('update:selected', allChecked.value ? [] : props.docs.map(d => d.doc_id))
}
const toggleOne = (id) => {
  const s = new Set(props.selected)
  s.has(id) ? s.delete(id) : s.add(id)
  emit('update:selected', [...s])
}

const pct = computed(() => {
  const p = props.progress
  if (!p || !p.total) return props.running ? 3 : 0
  return Math.max(3, Math.min(100, Math.round((p.done / p.total) * 100)))
})

const mmss = (s) => {
  s = Math.max(0, Math.round(s || 0))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

const modes = [
  { v: 'table', label: '表格块', hint: '只抽表格（最快，成分/性能数据几乎都在表里）' },
  { v: 'auto', label: '自动', hint: '表格 + 正文里出现「数值+单位」的段落（比表格块多几倍）' },
  { v: 'all', label: '全部块', hint: '全部文本块（最慢，正文里绝大多数段落没有数据）' },
]
</script>

<template>
  <section class="panel flex min-h-0 flex-col">
    <header class="panel-head">
      <span class="panel-title">文献与运行</span>
      <span class="chip mono">{{ selected.length }}/{{ docs.length }} 篇</span>
    </header>

    <div class="flex min-h-0 flex-1 flex-col">
      <!-- 可滚动区：选项都在这里，窗口矮的时候自己滚，不挤占底部主按钮 -->
      <div class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
      <!-- 文献列表：头部固定不参与滚动（原先 sticky 会盖住第一项），列表高度可拖拽 -->
      <div class="flex shrink-0 flex-col overflow-hidden rounded-md border"
           :style="{ borderColor: 'var(--border)' }">
        <button
          class="flex w-full shrink-0 items-center gap-2 border-b px-2.5 py-1.5 text-left text-[11.5px] muted"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface-2)' }"
          @click="toggleAll"
        >
          <span class="inline-flex h-3.5 w-3.5 items-center justify-center rounded-[3px] border"
                :style="{ borderColor: allChecked ? 'var(--accent)' : 'var(--border-strong)',
                          background: allChecked ? 'var(--accent)' : 'transparent' }">
            <svg v-if="allChecked" width="9" height="9" viewBox="0 0 10 10"><path d="M1 5.2 3.6 8 9 2.2" fill="none" stroke="var(--accent-fg)" stroke-width="1.8"/></svg>
          </span>
          全选
          <span class="ml-auto mono text-[10.5px]">{{ selected.length }} / {{ docs.length }}</span>
        </button>
        <div class="min-w-0 overflow-y-auto" :style="{ height: listH + 'px' }">
          <div v-if="!docs.length" class="empty" style="padding: 22px 12px">
            还没有索引文献<br />先把 PDF 放进 data/raw_pdfs 再建索引
          </div>
          <button
            v-for="d in docs" :key="d.doc_id"
            class="flex w-full items-start gap-2 border-b px-2.5 py-1.5 text-left last:border-b-0"
            :style="{ borderColor: 'var(--border)',
                      background: selected.includes(d.doc_id) ? 'var(--accent-weak)' : 'transparent' }"
            @click="toggleOne(d.doc_id)"
          >
            <span class="mt-[3px] inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[3px] border"
                  :style="{ borderColor: selected.includes(d.doc_id) ? 'var(--accent)' : 'var(--border-strong)',
                            background: selected.includes(d.doc_id) ? 'var(--accent)' : 'transparent' }">
              <svg v-if="selected.includes(d.doc_id)" width="9" height="9" viewBox="0 0 10 10"><path d="M1 5.2 3.6 8 9 2.2" fill="none" stroke="var(--accent-fg)" stroke-width="1.8"/></svg>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[12px]" :title="d.doc_name">{{ d.doc_name }}</span>
              <span class="block text-[11px] muted mono">{{ d.page_count }} 页 · {{ d.chunks }} 块 · 表 {{ d.tables }}</span>
            </span>
          </button>
        </div>
        <!-- 拖这里改高度；双击复位 -->
        <div class="flex h-[10px] shrink-0 cursor-ns-resize items-center justify-center border-t"
             :style="{ borderColor: 'var(--border)',
                       background: dragging ? 'var(--accent-weak)' : 'var(--surface-2)' }"
             title="拖动调整列表高度，双击复位"
             @mousedown="startDrag" @dblclick="listH = LIST_H0">
          <span class="h-[2px] w-9 rounded-full" :style="{ background: 'var(--border-strong)' }"></span>
        </div>
      </div>

      <!-- 抽取范围：按钮上直接标出该模式会抽多少块 -->
      <div>
        <div class="mb-1 flex items-center justify-between text-[11.5px]">
          <span class="muted">抽取范围</span>
          <span v-if="planErr" class="text-[10.5px]" :style="{ color: 'var(--warn)' }">{{ planErr }}</span>
        </div>
        <div class="grid grid-cols-3 gap-1 rounded-md p-1" :style="{ background: 'var(--surface-2)' }">
          <button v-for="m in modes" :key="m.v"
                  class="flex h-[34px] flex-col items-center justify-center rounded-[5px] leading-tight"
                  :title="m.hint"
                  :style="mode === m.v
                    ? { background: 'var(--surface)', color: 'var(--text)', boxShadow: '0 1px 2px rgba(0,0,0,.06)', fontWeight: 600 }
                    : { color: 'var(--muted)' }"
                  @click="emit('update:mode', m.v)">
            <span class="text-[11.5px]">{{ m.label }}</span>
            <span class="mono text-[10px] muted">{{ plan ? (plan.counts[m.v] ?? 0) + ' 块' : '—' }}</span>
          </button>
        </div>
      </div>

      <!-- 抽取块数上限：先小批量试通，再放开 -->
      <div>
        <div class="mb-1 flex items-center justify-between text-[11.5px]">
          <span class="muted">最多抽几块</span>
          <span class="mono">{{ maxChunks > 0 ? maxChunks + ' 块' : '不限' }}</span>
        </div>
        <div class="grid grid-cols-5 gap-1">
          <button v-for="n in maxPresets" :key="n"
                  class="h-[26px] rounded-[5px] text-[11.5px]"
                  :style="maxChunks === n
                    ? { background: 'var(--accent)', color: 'var(--accent-fg)', fontWeight: 600 }
                    : { background: 'var(--surface-2)', color: 'var(--muted)' }"
                  @click="setMax(n)">{{ n }}</button>
          <button class="h-[26px] rounded-[5px] text-[11.5px]"
                  :style="maxChunks === 0
                    ? { background: 'var(--accent)', color: 'var(--accent-fg)', fontWeight: 600 }
                    : { background: 'var(--surface-2)', color: 'var(--muted)' }"
                  @click="setMax(0)">不限</button>
        </div>
        <input class="field mt-1 w-full" type="number" min="0" step="1"
               :value="maxChunks || ''" placeholder="自定义块数，0 或留空 = 不限"
               :disabled="running" @input="onMaxInput" />
      </div>

      <!-- 开跑前的规模与耗时预估 -->
      <div class="rounded-md px-2.5 py-1.5 text-[11px] leading-relaxed"
           :style="{ background: 'var(--surface-2)' }">
        <template v-if="plan">
          本次计划 <b class="mono">{{ planPicked }}</b><span v-if="planPicked !== planTotal"
            class="muted"> / {{ planTotal }}</span> 块
          <template v-if="etaPlan"> · 预计约 <b class="mono">{{ etaPlan }}</b></template>
          <span class="muted">
            （{{ spc !== null ? Math.round(spc) : '?' }} 秒/块{{ plan.measured ? '，本机实测' : '，首次估算' }}）
          </span>
          <div v-if="planPicked === 0" class="mt-0.5" :style="{ color: 'var(--warn)' }">
            当前条件下没有可抽取的块，换个范围或勾选更多文献。
          </div>
          <div v-else-if="warnLevel === 'high'" class="mt-0.5" :style="{ color: 'var(--err)' }">
            块数过多。先用「3」或「10」跑一遍确认抽取质量，再放开；
            表格里的成分/性能数据已经覆盖绝大多数目标。
          </div>
          <div v-else-if="warnLevel === 'mid'" class="mt-0.5" :style="{ color: 'var(--warn)' }">
            块数偏多，建议先小批量试一遍看质量，再决定要不要全量。
          </div>
        </template>
        <template v-else><span class="muted">正在统计可抽取块数…</span></template>
      </div>

      <!-- 并发 -->
      <div>
        <div class="mb-1 flex items-center justify-between text-[11.5px]">
          <span class="muted">并发请求数</span>
          <span class="mono">{{ workers }}</span>
        </div>
        <input type="range" min="1" max="4" step="1" :value="workers"
               class="w-full accent-[var(--accent)]"
               @input="emit('update:workers', Number($event.target.value))" />
        <div class="mt-0.5 text-[10.5px] muted">
          并发越快，但模型输出会变短：同一批块实测 1 并发 9~10 条、2 并发 8 条、3 并发 5~6 条。
        </div>
        <div v-if="workers >= 3" class="mt-0.5 text-[10.5px]" :style="{ color: 'var(--err)' }">
          ≥3 并发实测会明显少抽记录（约少一半）。要完整数据请调回 1，慢但最全。
        </div>
      </div>

      <button class="btn btn-ghost justify-start px-0 text-[11.5px]" @click="advanced = !advanced">
        {{ advanced ? '▾' : '▸' }} 目标 Excel（可选）
      </button>
      <div v-if="advanced">
        <input class="field w-full" :value="excelPath" placeholder="D:\\data\\target.xlsx"
               @input="emit('update:excelPath', $event.target.value)" />
        <div class="mt-1 text-[10.5px]" :style="{ color: 'var(--warn)' }">
          填了路径 → 以该表表头为准覆盖模板；不填 → 按模板抽，只显示结果
        </div>
      </div>

      <!-- 默认收起：面板高度有限，说明文字不该把「开始抽取」挤出可视区 -->
      <details class="shrink-0 border-t pt-1 text-[11px] leading-relaxed muted"
               :style="{ borderColor: 'var(--border)' }">
        <summary class="cursor-pointer select-none py-0.5" :style="{ color: 'var(--text-soft)' }">
          使用顺序 / 注意事项
        </summary>
        <div class="mt-1 space-y-0.5">
          <div>1 · 左侧选模板或粘贴 YAML，点「校验」</div>
          <div>2 · 勾选文献，选范围 / 块数 / 并发，点「开始抽取」</div>
          <div>3 · 右侧结果点任意行，展开原文引文与核验状态</div>
          <div class="pt-1 text-[10.5px]">不填目标 Excel 时只在界面显示，不会改动任何文件</div>
          <div class="text-[10.5px]" :style="{ color: 'var(--warn)' }">
            页面被关掉/被别的窗口冲掉时：空闲状态 15 秒后服务才停；
            正在抽取则**不会停**，任务跑完为止，重开页面自动接管进度与结果
            （宽限期在 config.yaml 的 web.shutdown_grace_s）
          </div>
        </div>
      </details>
      </div>

      <!-- 固定底部：主操作常驻，窗口再矮也点得到 -->
      <div class="shrink-0 space-y-2 border-t p-3 pt-2.5"
           :style="{ borderColor: 'var(--border)' }">
        <button class="btn btn-primary h-9 w-full" :disabled="running || !selected.length" @click="emit('run')">
          <svg v-if="running" width="12" height="12" viewBox="0 0 24 24" class="animate-spin">
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="3" stroke-dasharray="40 20"/>
          </svg>
          {{ running ? '抽取中…' : '开始抽取' }}
        </button>

        <!-- 继续抽取：上次抽到第 N 块，接着往下；全新开始则清空累计 -->
        <button v-if="!running && doneChunks > 0"
                class="btn h-8 w-full text-[11.5px]"
                :disabled="!selected.length" @click="emit('continue')">
          ▶ 继续抽取（已抽 {{ doneChunks }} 块，接着往下）
        </button>

        <button v-if="running" class="btn btn-ghost h-8 w-full text-[11.5px]"
                :disabled="stopping" @click="emit('stop')">
          {{ stopping ? '正在停止…（等当前几块收尾）' : '■ 停止（保留已抽完的部分）' }}
        </button>

        <div v-if="running || progress" class="rounded-md px-2.5 py-2" :style="{ background: 'var(--surface-2)' }">
          <div class="mb-1.5 flex items-baseline justify-between text-[11.5px]">
            <span class="mono">{{ progress ? `${progress.done}/${progress.total} 块` : '准备中' }}</span>
            <span class="muted mono">
              已用 {{ mmss(elapsedSec) }}
            </span>
          </div>
          <div class="track"><i :style="{ width: pct + '%' }"></i></div>
          <div class="mt-1.5 text-[10.5px] muted">
            <template v-if="progress">
              累计 {{ progress.records }} 条记录
              <template v-if="progress.done && progress.elapsed">
                · 实测 {{ Math.round(progress.elapsed / progress.done) }} 秒/块
              </template>
            </template>
            <template v-else>正在载入模型与索引…</template>
          </div>
          <div v-if="progress && progress.stopped" class="mt-1 text-[10.5px]"
               :style="{ color: 'var(--warn)' }">
            已请求停止，剩余块放弃；已抽完的部分会照常返回。
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
