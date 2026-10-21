<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, streamProgress } from './api'
import SchemaPanel from './components/SchemaPanel.vue'
import RunPanel from './components/RunPanel.vue'
import ResultPanel from './components/ResultPanel.vue'
import AskView from './components/AskView.vue'
import IndexView from './components/IndexView.vue'

// 支持 ?view=ask / ?view=index 直达（也方便截图与分享）
const initial = new URLSearchParams(location.search).get('view')
const view = ref(['extract', 'ask', 'index'].includes(initial) ? initial : 'extract')
const VIEWS = [
  { v: 'extract', label: '抽取' },
  { v: 'ask', label: '问答' },
  { v: 'index', label: '索引' },
]
const theme = ref(document.documentElement.getAttribute('data-theme') || 'light')
const toggleTheme = () => {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', theme.value)
  try { localStorage.setItem('mr-theme', theme.value) } catch (e) { /* ignore */ }
}

const templates = ref([])
const activeTpl = ref('')
const yamlText = ref('')
const validation = ref(null)
const docs = ref([])
const selected = ref([])
const status = ref(null)
const llm = ref(null)

const mode = ref('table')
const workers = ref(2)
// 默认限 10 块：先小批量确认抽取质量，再点「不限」放开。本地模型逐块生成，
// 不限量时几十块就是几十分钟，默认限速比默认全跑更符合实际使用节奏。
const maxChunks = ref(10)
const excelPath = ref('')

const running = ref(false)
const progress = ref(null)
const result = ref(null)
const err = ref(null)
const busy = ref(false)
const jobId = ref('')
const stopping = ref(false)

// ---- 实时用时：计时器挂在 App 顶层 ----
// 原来放 RunPanel 里，但 RunPanel 用 v-if 切换视图时会被卸载，计时器随之销毁，
// 切到问答/索引或最小化后计时就停。提升到 App 后计时器与视图解耦，始终在跑。
const runStart = ref(0)
const nowTick = ref(0)
let elapsedTimer = null

function startClock(startTs) {
  // startTs 用于刷新接管：把已跑掉的时间接上，而不是从 0 重新数
  runStart.value = startTs || Date.now()
  nowTick.value = Date.now()
  if (elapsedTimer) clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
}
function stopClock() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null }
}
onBeforeUnmount(stopClock)

const elapsedSec = computed(() => {
  if (running.value && runStart.value) {
    return Math.max(0, Math.floor((nowTick.value - runStart.value) / 1000))
  }
  return Math.round(progress.value?.elapsed || 0)
})

// 继续抽取：记录上一批实际抽到的块数（skip = 已完成的块数）
const doneChunks = ref(0)
// 改了抽取范围（文献/模式/块数）后，「已抽 N 块」这个位置就失去意义了：
// 块列表变了，原来的 skip 会跳到错误的位置，所以必须归零重新计数。
watch([selected, mode, maxChunks], () => { doneChunks.value = 0 })

const statLine = computed(() => {
  const s = status.value?.index
  if (!s) return ''
  return `${s.docs} 篇 · ${s.chunks} 块 · ${s.tables} 表`
})


const llmOk = computed(() => llm.value?.ok === true)
const llmName = computed(() => llm.value?.using || (llmOk.value ? '已连接' : '未连接'))

async function loadAll() {
  try {
    const [st, ds, tp, ll] = await Promise.all([
      api.status(), api.docs(), api.templates(), api.llm().catch(() => null),
    ])
    status.value = st
    docs.value = ds.docs || []
    selected.value = docs.value.map(d => d.doc_id)
    templates.value = tp.templates || []
    llm.value = ll
    if (templates.value.length) {
      const first = templates.value[0].name
      await pickTemplate(first)
    }
    const cfgWorkers = 2
    workers.value = cfgWorkers
  } catch (e) {
    err.value = e.message
  }
}

async function pickTemplate(name) {
  busy.value = true
  try {
    const r = await api.schemaGet(name)
    activeTpl.value = name
    yamlText.value = r.yaml || ''
    await runValidate()
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

async function runValidate() {
  busy.value = true
  try {
    validation.value = await api.schemaValidate(yamlText.value)
  } catch (e) {
    validation.value = { ok: false, errors: [{ path: '', msg: e.message }], warnings: [] }
  } finally {
    busy.value = false
  }
}

async function saveTemplate(name) {
  busy.value = true
  try {
    const r = await api.schemaSave(yamlText.value, name || '')
    const tp = await api.templates()
    templates.value = tp.templates || []
    activeTpl.value = r.schema?.name || activeTpl.value
    err.value = null
    alert(`已保存到：${r.path}`)
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

function onProgress(ev) {
  if (ev.type === 'progress') progress.value = ev
}

// 继续抽取时把新一批结果接到已有结果后面。
// 不去重：块是按 skip 切分的、互不重叠，重复行只可能是原文真的重复出现，
// 而"不臆造、不擅自丢弃"是铁律——要去掉重复行请用导出时的「去重」开关（可审计）。
function mergeResults(prev, next) {
  if (!prev) return next
  // 两批之间改过模板时表头会变（增删字段）。若直接拼接 rows，
  // 旧行缺少新字段的键，表格会整行错位——所以按新表头对齐，缺的补空。
  const headers = next.headers || prev.headers || []
  const old = prev.headers || []
  const same = old.length === headers.length && old.every((h, i) => h === headers[i])
  const oldRows = same
    ? (prev.rows || [])
    : (prev.rows || []).map(r => Object.fromEntries(headers.map(h => [h, r[h] ?? ''])))
  return {
    ...next,
    headers,
    records: [...(prev.records || []), ...(next.records || [])],
    rows: [...oldRows, ...(next.rows || [])],
    row_ids: [...(prev.row_ids || []), ...(next.row_ids || [])],
    dropped: [...(prev.dropped || []), ...(next.dropped || [])],
    records_count: (prev.records_count || 0) + (next.records_count || 0),
    picked_chunks: (prev.picked_chunks || 0) + (next.picked_chunks || 0),
  }
}

async function start(continueRun = false) {
  err.value = null
  await runValidate()
  if (!validation.value?.ok) {
    err.value = '模板校验未通过，先把错误改掉再抽取'
    return
  }
  running.value = true
  stopping.value = false
  if (!continueRun) {
    result.value = null
    doneChunks.value = 0          // 全新开始
  }
  progress.value = null           // 进度是本批的，两种情况都归零
  const skip = continueRun ? doneChunks.value : 0
  startClock()
  try {
    const { job_id } = await api.startExtract({
      schema_text: yamlText.value,
      mode: mode.value,
      workers: workers.value,
      max_chunks: maxChunks.value,
      skip,
      doc_ids: selected.value,
      excel_path: excelPath.value.trim() || null,
    })
    jobId.value = job_id
    streamProgress(
      job_id,
      (ev) => {
        onProgress(ev)
        if (ev.type === 'done') {
          // 继续抽取：新结果追加到已有结果后面，不能覆盖（否则上一批记录全没了）
          result.value = continueRun ? mergeResults(result.value, ev.result) : ev.result
          // 累计已抽块数，供「继续抽取」定位
          doneChunks.value = skip + (progress.value?.done || 0)
          running.value = false
          jobId.value = ''
          stopping.value = false
          stopClock()
        } else if (ev.type === 'error') {
          err.value = ev.error + (ev.trace ? `\n${ev.trace}` : '')
          running.value = false
          jobId.value = ''
          stopping.value = false
          stopClock()
        }
      },
      (e) => { err.value = e.message; running.value = false; stopClock() },
    )
  } catch (e) {
    err.value = e.message
    running.value = false
    stopClock()
  }
}

// 继续抽取：从上次完成位置接着往下抽
function continueRun() {
  start(true)
}

// 停止：已抽完的块保留，剩下的直接放弃（不用干等到结束）
async function stopRun() {
  if (!jobId.value) return
  stopping.value = true
  try {
    await api.cancelExtract(jobId.value)
  } catch (e) {
    err.value = e.message
    stopping.value = false
  }
}

// ---- 页面生命周期：关页面 = 停服务（刷新由服务端 web.shutdown_grace_s 宽限期兜住）----
const sessionId = (window.crypto?.randomUUID)
  ? window.crypto.randomUUID()
  : `${Date.now()}-${Math.random().toString(16).slice(2)}`
let hbTimer = null
let byeSent = false

function sendBye() {
  if (byeSent) return
  byeSent = true
  const body = JSON.stringify({ id: sessionId })
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/session/bye',
        new Blob([body], { type: 'application/json' }))
    } else {
      fetch('/api/session/bye', {
        method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body,
      }).catch(() => {})
    }
  } catch (e) { /* 关页面时异常也无所谓 */ }
}

// ---- 刷新/重开后接管服务端还在跑（或刚跑完）的抽取任务 ----
// 没有这段的话，刷新一次就丢失 jobId：任务在后台继续跑，但界面既看不到进度也拿不到结果。
async function adoptRunningJob() {
  try {
    const r = await api.runningJob()
    if (!r?.running) return
    const skip = r.req?.skip || 0
    doneChunks.value = skip      // 先按起始位置对齐，进度事件会继续累加
    if (r.done && r.result) {
      result.value = r.result    // 已跑完：直接把结果还回来，不白等
      progress.value = r.progress?.done
        ? { ...r.progress } : null
      doneChunks.value = skip + (r.progress?.done || 0)
      return
    }
    // 还在跑：重连 SSE，并把计时接到任务真正的起点
    running.value = true
    jobId.value = r.job_id
    progress.value = r.progress?.done ? { ...r.progress } : null
    startClock(r.started_at ? r.started_at * 1000 : Date.now())
    streamProgress(
      r.job_id,
      (ev) => {
        onProgress(ev)
        if (ev.type === 'done') {
          result.value = ev.result
          doneChunks.value = skip + (progress.value?.done || 0)
          running.value = false
          jobId.value = ''
          stopping.value = false
          stopClock()
        } else if (ev.type === 'error') {
          err.value = ev.error + (ev.trace ? `\n${ev.trace}` : '')
          running.value = false
          jobId.value = ''
          stopping.value = false
          stopClock()
        }
      },
      (e) => { err.value = e.message; running.value = false; stopClock() },
    )
  } catch (e) { /* 老后端没有该接口，忽略 */ }
}

onMounted(async () => {
  loadAll()
  adoptRunningJob()
  try { await api.sessionHello(sessionId) } catch (e) { /* 老后端没有该接口 */ }
  hbTimer = setInterval(() => {
    api.sessionPing(sessionId).catch(() => {})
  }, 15000)
  window.addEventListener('pagehide', sendBye)
  window.addEventListener('beforeunload', sendBye)
})

onBeforeUnmount(() => { if (hbTimer) clearInterval(hbTimer) })
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- 顶栏 -->
    <header class="flex h-[52px] shrink-0 items-center justify-between border-b px-4"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }">
      <div class="flex items-center gap-3">
        <span class="text-[15px] font-semibold tracking-tight">MaterialRAG</span>
        <nav class="flex items-center gap-0.5 rounded-md p-0.5" :style="{ background: 'var(--surface-2)' }">
          <button v-for="v in VIEWS" :key="v.v"
                  class="h-[24px] rounded-[5px] px-2.5 text-[12px]"
                  :style="view === v.v
                    ? { background: 'var(--surface)', color: 'var(--text)', boxShadow: '0 1px 2px rgba(0,0,0,.06)', fontWeight: 600 }
                    : { color: 'var(--muted)' }"
                  @click="view = v.v">{{ v.label }}</button>
        </nav>
        <span v-if="statLine" class="chip mono">{{ statLine }}</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="chip" :style="llmOk ? { color: 'var(--ok)', borderColor: 'var(--ok)' } : { color: 'var(--err)', borderColor: 'var(--err)' }">
          <i class="inline-block h-[6px] w-[6px] rounded-full"
             :style="{ background: llmOk ? 'var(--ok)' : 'var(--err)' }"></i>
          <span class="mono max-w-[190px] truncate" :title="llmName">{{ llmName }}</span>
        </span>
        <button class="btn btn-ghost h-8 px-2" :title="theme === 'dark' ? '切换浅色' : '切换深色'" @click="toggleTheme">
          <svg v-if="theme === 'dark'" width="15" height="15" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4.2"/>
            <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4 19 19M19 5l-1.6 1.6M6.6 17.4 5 19"/></svg>
          <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.8"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z"/></svg>
        </button>
      </div>
    </header>

    <!-- 错误条 -->
    <div v-if="err" class="shrink-0 border-b px-4 py-2 text-[12px]"
         :style="{ borderColor: 'var(--err)', background: 'var(--err-weak)', color: 'var(--err)' }">
      <div class="flex items-start justify-between gap-3">
        <pre class="mono max-h-24 overflow-auto whitespace-pre-wrap">{{ err }}</pre>
        <button class="btn btn-ghost h-6 shrink-0 px-2 text-[11px]" @click="err = null">关闭</button>
      </div>
    </div>

    <!-- 抽取工作台
         断点策略：≥1280 三栏、1024~1280 两栏（结果区跨列下沉）、<1024 单栏纵向排列。
         每个面板给固定高度并各自内部滚动，避免窗口变窄时 grid 行高塌陷、面板互相压住。 -->
    <main v-if="view === 'extract'"
          class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-3 xl:overflow-hidden">
      <div class="grid min-h-0 grid-cols-1 gap-3
                  lg:grid-cols-[300px_minmax(0,1fr)]
                  xl:h-full xl:grid-cols-[300px_minmax(0,1fr)_minmax(0,460px)]">
        <SchemaPanel
          class="h-[72vh] min-w-0 xl:h-full"
          :templates="templates" :active-name="activeTpl" :yaml="yamlText"
          :validation="validation" :busy="busy"
          @update:yaml="yamlText = $event"
          @validated="validation = $event"
          @pick="pickTemplate" @validate="runValidate" @save="saveTemplate" />

        <RunPanel
          class="h-[72vh] min-w-0 xl:h-full"
          :docs="docs" :selected="selected" :mode="mode" :workers="workers"
          :max-chunks="maxChunks" :excel-path="excelPath"
          :running="running" :stopping="stopping" :progress="progress" :status="status"
          :elapsed-sec="elapsedSec" :done-chunks="doneChunks"
          @update:selected="selected = $event" @update:mode="mode = $event"
          @update:workers="workers = $event" @update:maxChunks="maxChunks = $event"
          @update:excelPath="excelPath = $event"
          @run="start()" @continue="continueRun" @stop="stopRun" />

        <ResultPanel class="h-[72vh] min-w-0 lg:col-span-2 xl:col-span-1 xl:h-full"
                     :result="result" :running="running"
                     :schema-text="yamlText" />
      </div>
    </main>

    <!-- 文献问答（纯 RAG，不涉及抽取与写表） -->
    <main v-else-if="view === 'ask'" class="min-h-0 flex-1 p-3">
      <AskView :llm-name="llmName" @error="err = $event" />
    </main>

    <!-- 索引与运行环境 -->
    <main v-else class="min-h-0 flex-1 p-3">
      <IndexView :status="status" :docs="docs" :llm-name="llmName"
                 @refresh="loadAll" @error="err = $event" />
    </main>
  </div>
</template>
