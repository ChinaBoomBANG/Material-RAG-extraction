<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  status: { type: Object, default: null },
  docs: { type: Array, default: () => [] },
  llmName: { type: String, default: '' },
})
const emit = defineEmits(['refresh', 'error'])

const glob = ref('')
const force = ref(false)
const busy = ref(false)
const elapsed = ref(0)
const lastResult = ref(null)
const timer = ref(null)
const stopping = ref(false)
const stopped = ref(false)

const idx = computed(() => props.status?.index || {})

async function stopServer() {
  stopping.value = true
  try {
    await api.stopServer()
    stopped.value = true
  } catch (e) {
    emit('error', e.message)
  } finally {
    stopping.value = false
  }
}

async function runIngest() {
  busy.value = true
  lastResult.value = null
  emit('error', null)
  elapsed.value = 0
  timer.value = setInterval(() => { elapsed.value += 0.5 }, 500)
  try {
    lastResult.value = await api.ingest({
      glob: glob.value.trim() || null,
      force: force.value,
    })
    emit('refresh')
  } catch (e) {
    emit('error', e.message)
  } finally {
    clearInterval(timer.value)
    busy.value = false
  }
}
</script>

<template>
  <div class="mx-auto flex h-full min-h-0 w-full max-w-[1000px] flex-col gap-3 overflow-y-auto">
    <section class="panel shrink-0">
      <header class="panel-head">
        <span class="panel-title">索引状态</span>
        <span class="chip mono">{{ llmName || 'LLM 未知' }}</span>
      </header>
      <div class="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4">
        <div class="rounded-md border px-3 py-2" :style="{ borderColor: 'var(--border)' }">
          <div class="text-[11px] muted">文献</div>
          <div class="mono text-[18px]">{{ idx.docs ?? '—' }}</div>
        </div>
        <div class="rounded-md border px-3 py-2" :style="{ borderColor: 'var(--border)' }">
          <div class="text-[11px] muted">文本块</div>
          <div class="mono text-[18px]">{{ idx.chunks ?? '—' }}</div>
        </div>
        <div class="rounded-md border px-3 py-2" :style="{ borderColor: 'var(--border)' }">
          <div class="text-[11px] muted">表格块</div>
          <div class="mono text-[18px]" :style="{ color: 'var(--accent)' }">{{ idx.tables ?? '—' }}</div>
        </div>
        <div class="rounded-md border px-3 py-2" :style="{ borderColor: 'var(--border)' }">
          <div class="text-[11px] muted">向量维度</div>
          <div class="mono text-[18px]">{{ idx.dim ?? '—' }}</div>
        </div>
      </div>
      <div class="px-3 pb-3 text-[11px] muted">
        索引目录：<span class="mono">{{ status?.index_dir || '—' }}</span><br />
        向量后端：<span class="mono">{{ status?.vector_backend || 'local' }}</span>
      </div>
    </section>

    <section class="panel shrink-0">
      <header class="panel-head">
        <span class="panel-title">建立索引</span>
        <span class="text-[11px] muted">把 data/raw_pdfs 里的 PDF 解析入库（不调用大模型）</span>
      </header>
      <div class="flex flex-wrap items-center gap-2 p-3">
        <input v-model="glob" class="field w-[320px]"
               placeholder="PDF 通配路径，留空 = data/raw_pdfs/*.pdf" />
        <label class="flex items-center gap-1.5 text-[11.5px]">
          <input v-model="force" type="checkbox" /> 已索引的也重新解析
        </label>
        <button class="btn btn-primary ml-auto h-8 px-4" :disabled="busy" @click="runIngest">
          <svg v-if="busy" width="12" height="12" viewBox="0 0 24 24" class="animate-spin">
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="3" stroke-dasharray="40 20"/>
          </svg>
          {{ busy ? `解析中 ${elapsed.toFixed(0)}s` : '建立索引' }}
        </button>
      </div>
      <div v-if="lastResult" class="mx-3 mb-3 rounded-md px-3 py-2 text-[11.5px]"
           :style="{ background: 'var(--ok-weak)', color: 'var(--ok)' }">
        索引完成：处理 {{ lastResult.processed ?? lastResult.docs ?? '—' }} 篇 ·
        共 {{ lastResult.chunks ?? '—' }} 块 ·
        表格 {{ lastResult.tables ?? '—' }} 个
      </div>
    </section>

    <section class="panel shrink-0">
      <header class="panel-head">
        <span class="panel-title">服务生命周期</span>
        <span class="chip" :style="{ color: 'var(--ok)', borderColor: 'var(--ok)' }">关页面即停</span>
      </header>
      <div class="flex flex-wrap items-center justify-between gap-3 p-3">
        <div class="text-[11.5px] leading-relaxed muted">
          关闭本页 <b>8 秒后</b>服务自动退出（刷新不会，会被重新连上的页面取消）。<br />
          正在进行的抽取会随之中断——这就是「关窗口 = 停掉这轮工作」。
        </div>
        <button class="btn h-8 px-3" :disabled="stopping || stopped" @click="stopServer">
          {{ stopped ? '已停止' : (stopping ? '停止中…' : '立即停止服务') }}
        </button>
      </div>
      <div v-if="stopped" class="mx-3 mb-3 rounded-md px-3 py-2 text-[11.5px]"
           :style="{ background: 'var(--ok-weak)', color: 'var(--ok)' }">
        服务已停止，可以直接关掉本页。
      </div>
    </section>

    <section class="panel flex min-h-0 flex-1 flex-col">
      <header class="panel-head">
        <span class="panel-title">已索引文献</span>
        <span class="chip mono">{{ docs.length }} 篇</span>
      </header>
      <div class="min-h-0 flex-1 overflow-auto">
        <table class="w-full border-collapse text-[12px]">
          <thead>
            <tr>
              <th v-for="h in ['文献', '页数', '块数', '表格']" :key="h"
                  class="sticky top-0 border-b px-3 py-1.5 text-left font-medium"
                  :style="{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--muted)' }">
                {{ h }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in docs" :key="d.doc_id">
              <td class="border-b px-3 py-1.5" :style="{ borderColor: 'var(--border)' }">{{ d.doc_name }}</td>
              <td class="border-b px-3 py-1.5 text-right mono" :style="{ borderColor: 'var(--border)' }">{{ d.page_count }}</td>
              <td class="border-b px-3 py-1.5 text-right mono" :style="{ borderColor: 'var(--border)' }">{{ d.chunks }}</td>
              <td class="border-b px-3 py-1.5 text-right mono" :style="{ borderColor: 'var(--border)' }">{{ d.tables }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="!docs.length" class="empty">还没有索引任何文献</div>
      </div>
    </section>
  </div>
</template>
