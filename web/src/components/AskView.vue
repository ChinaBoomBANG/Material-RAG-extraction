<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'

defineProps({
  llmName: { type: String, default: '' },
})
const emit = defineEmits(['error'])

const question = ref('')
const topK = ref(8)
const busy = ref(false)
const elapsed = ref(0)
const answer = ref(null)
const timer = ref(null)

const citations = computed(() => answer.value?.citations || [])
const canAsk = computed(() => question.value.trim().length > 0 && !busy.value)

async function ask() {
  if (!canAsk.value) return
  busy.value = true
  answer.value = null
  emit('error', null)
  elapsed.value = 0
  timer.value = setInterval(() => { elapsed.value += 0.5 }, 500)
  try {
    answer.value = await api.ask(question.value.trim(), topK.value)
  } catch (e) {
    emit('error', e.message)
  } finally {
    clearInterval(timer.value)
    busy.value = false
  }
}
</script>

<template>
  <div class="mx-auto flex h-full min-h-0 w-full max-w-[900px] flex-col gap-3">
    <section class="panel shrink-0">
      <header class="panel-head">
        <span class="panel-title">文献问答</span>
        <span class="text-[11px] muted">检索本机索引 · 回答只依据命中的原文</span>
      </header>
      <div class="p-3">
        <textarea
          v-model="question"
          class="field h-[76px] w-full resize-none"
          style="font-family: inherit; font-size: 13px"
          placeholder="例如：6xxx 铝合金挤压温度对力学性能的影响有哪些？"
          @keydown.ctrl.enter="ask"
        ></textarea>
        <div class="mt-2 flex items-center gap-2">
          <label class="whitespace-nowrap text-[11.5px] muted">引用条数</label>
          <select v-model.number="topK" class="field w-[72px]">
            <option :value="4">4</option>
            <option :value="8">8</option>
            <option :value="12">12</option>
          </select>
          <span class="text-[10.5px] muted">Ctrl + Enter 提交</span>
          <button class="btn btn-primary ml-auto h-8 px-4" :disabled="!canAsk" @click="ask">
            <svg v-if="busy" width="12" height="12" viewBox="0 0 24 24" class="animate-spin">
              <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="3" stroke-dasharray="40 20"/>
            </svg>
            {{ busy ? `思考中 ${elapsed.toFixed(0)}s` : '提问' }}
          </button>
        </div>
      </div>
    </section>

    <section class="panel flex min-h-0 flex-1 flex-col">
      <header class="panel-head">
        <span class="panel-title">回答</span>
        <span v-if="citations.length" class="chip">{{ citations.length }} 条引用</span>
      </header>
      <div class="min-h-0 flex-1 overflow-y-auto p-4">
        <div v-if="!answer && !busy" class="empty">
          还没有提问<br />问题会先在本机索引里检索，再让本地模型组织答案
        </div>
        <div v-else-if="busy" class="empty">正在检索并生成… 单次通常 10–60 秒</div>
        <template v-else>
          <div class="whitespace-pre-wrap text-[13.5px] leading-[1.85]">{{ answer.answer }}</div>

          <div v-if="citations.length" class="mt-5">
            <div class="mb-1.5 border-t pt-3 text-[12px] font-medium"
                 :style="{ borderColor: 'var(--border)', color: 'var(--text-soft)' }">
              引用来源
            </div>
            <div v-for="c in citations" :key="c.no"
                 class="border-t py-1.5 text-[11.5px]" :style="{ borderColor: 'var(--border)' }">
              <div class="flex items-baseline gap-2">
                <span class="mono muted">[{{ c.no }}]</span>
                <span class="mono text-[11px]">{{ c.doc }}</span>
                <span class="chip" style="height: 18px">p{{ c.page }}</span>
                <span class="chip" style="height: 18px">{{ c.kind === 'table' ? '表格' : '正文' }}</span>
                <span class="mono ml-auto text-[10.5px] muted">score {{ c.score }}</span>
              </div>
              <div class="mt-0.5 pl-6 leading-relaxed muted">{{ c.excerpt }}…</div>
            </div>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>
