<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  templates: { type: Array, default: () => [] },
  activeName: { type: String, default: '' },
  yaml: { type: String, default: '' },
  validation: { type: Object, default: null },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['update:yaml', 'pick', 'validate', 'save', 'validated'])

const editing = ref(false)          // YAML 默认收起，把空间让给字段列表
const saveName = ref('')
const showErrors = ref(true)

// 字段分类过滤：全部 / 成分 / 组织 / 工艺 / 性能，单选
const BUCKET_FILTERS = [
  { v: 'all', label: '全部' },
  { v: 'composition', label: '成分' },
  { v: 'microstructure', label: '组织' },
  { v: 'process', label: '工艺' },
  { v: 'properties', label: '性能' },
]
const fieldFilter = ref('all')

const stats = computed(() => props.validation?.stats || null)
const errors = computed(() => props.validation?.errors || [])
const warnings = computed(() => props.validation?.warnings || [])
const activeTpl = computed(() => props.templates.find(t => t.name === props.activeName))
const groups = computed(() => props.validation?.schema?.groups || [])

// ---------------- 字段增删（支持手输，不限于模板预设） ----------------
const newFields = ref('')
const editBusy = ref(false)
const editMsg = ref('')

// 添加目标组直接跟随分类过滤器：选「成分」就加进成分组，不再单独选组。
// 「全部」时无法确定目标桶，添加会被拦截并提示先选分类。
const targetGroup = computed(() => {
  if (fieldFilter.value === 'all') return ''
  const g = groups.value.find(g => (g.bucket || 'properties') === fieldFilter.value)
  return g ? g.key : ''
})

watch(fieldFilter, () => { editMsg.value = '' })

// 展开成 [{internal, key, label, unit, group, groupLabel, bucket}] 供 chips 显示
const fieldChips = computed(() => {
  const out = []
  for (const g of groups.value) {
    const bucket = g.bucket || 'properties'
    for (const f of (g.fields || [])) {
      out.push({
        internal: bucket === 'meta' ? f.key : `${bucket}.${f.key}`,
        key: f.key, label: f.label || f.key, unit: f.unit || '',
        group: g.key, groupLabel: g.label || g.key, bucket,
      })
    }
  }
  return out
})

// 按「全部 / 成分 / 组织 / 工艺 / 性能」过滤；meta（合金牌号等标识）只在「全部」显示
const visibleChips = computed(() => {
  if (fieldFilter.value === 'all') return fieldChips.value
  return fieldChips.value.filter(c => c.bucket === fieldFilter.value)
})

const applyResult = (r) => {
  emit('update:yaml', r.text)
  emit('validated', {
    ok: !(r.errors && r.errors.length),
    errors: r.errors || [], warnings: r.warnings || [],
    stats: r.stats, schema: r.schema,
  })
}

// ---------------- 字段拖拽排序 ----------------
// 拖 A 放到 B 上 = A 移动到 B 所在组、插在 B 前面（可跨分类拖）。
// 顺序改动走后端 move 操作写回 YAML——YAML 是唯一事实源。
const dragKey = ref('')          // 正在拖拽的字段内部名
const dropHint = ref('')         // 悬停目标，用于高亮

function onDragStart(f, e) {
  dragKey.value = f.internal
  e.dataTransfer.effectAllowed = 'move'
  try { e.dataTransfer.setData('text/plain', f.internal) } catch (err) { /* IE 兼容，失败无所谓 */ }
}
async function onDropTo(f) {
  const from = dragKey.value
  dragKey.value = ''
  dropHint.value = ''
  if (!from || from === f.internal || editBusy.value) return
  editBusy.value = true
  editMsg.value = ''
  try {
    const r = await api.schemaEdit({
      text: props.yaml, op: 'move', keys: [from],
      group: f.group, before: f.internal,
    })
    applyResult(r)
    editMsg.value = `已移动 ${from}`
  } catch (e) {
    editMsg.value = e.message
  } finally {
    editBusy.value = false
  }
}

async function addFields() {
  const raw = newFields.value.trim()
  if (!raw || editBusy.value) return
  if (!targetGroup.value) {
    editMsg.value = '先在上面选一个分类（成分 / 组织 / 工艺 / 性能），新字段才知道放进哪个桶'
    return
  }
  editBusy.value = true
  editMsg.value = ''
  try {
    const r = await api.schemaEdit({
      text: props.yaml, op: 'add', group: targetGroup.value, fields: [raw],
    })
    applyResult(r)
    const added = r.info?.added || []
    const skipped = r.info?.skipped || []
    editMsg.value = added.length
      ? `已添加 ${added.join('、')}${skipped.length ? `；已存在跳过 ${skipped.join('、')}` : ''}`
      : (skipped.length ? `已存在：${skipped.join('、')}` : '没有可添加的字段')
    newFields.value = ''
  } catch (e) {
    editMsg.value = e.message
  } finally {
    editBusy.value = false
  }
}

async function removeField(internal) {
  if (editBusy.value) return
  editBusy.value = true
  editMsg.value = ''
  try {
    const r = await api.schemaEdit({ text: props.yaml, op: 'remove', keys: [internal] })
    applyResult(r)
    editMsg.value = `已删除 ${internal}`
  } catch (e) {
    editMsg.value = e.message
  } finally {
    editBusy.value = false
  }
}

// select / textarea 不能用 :value 绑定——Vue 会写成 HTML 属性，
// textarea 的 value 属性不显示内容、select 的 value 属性也不生效。
// 用 v-model + 计算属性代理，由 Vue 去设置 DOM property。
const tplModel = computed({
  get: () => props.activeName,
  set: (v) => emit('pick', v),
})
const yamlModel = computed({
  get: () => props.yaml,
  set: (v) => emit('update:yaml', v),
})

const doSave = () => {
  const name = (saveName.value || '').trim() ||
    (activeTpl.value && activeTpl.value.source === 'user' ? activeTpl.value.name : '')
  emit('save', name)
}

// 下拉里显示的字段数：当前选中的模板用「实时校验结果」，这样增删字段后立刻跟着变；
// 其它模板仍用列表里的静态值（模板文件本身没动过）。
const fieldCountOf = (t) =>
  (t.name === props.activeName && stats.value?.fields != null)
    ? stats.value.fields
    : (t.field_count || 0)
</script>

<template>
  <section class="panel flex min-h-0 min-w-0 flex-col overflow-hidden">
    <header class="panel-head">
      <span class="panel-title">抽取模板</span>
      <span v-if="stats" class="chip mono">{{ stats.fields }} 字段</span>
    </header>

    <div class="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
      <div>
        <label class="mb-1 block text-[11.5px] muted">选择模板</label>
        <select v-model="tplModel" class="field w-full">
          <option v-for="t in templates" :key="t.path || (t.name + t.source)" :value="t.name">
            {{ t.name }}（{{ t.source === 'builtin' ? '内置' : '自定义' }}，{{ fieldCountOf(t) }} 字段）
          </option>
        </select>
      </div>

      <!-- 字段：支持手输名称，不再只能从模板里选 -->
      <div>
        <div class="mb-1 flex items-center justify-between text-[11.5px]">
          <span class="muted">字段</span>
          <span class="mono muted">{{ visibleChips.length }}<template v-if="fieldFilter !== 'all'"> / {{ fieldChips.length }}</template></span>
        </div>
        <!-- 分类过滤：全部 / 成分 / 组织 / 工艺 / 性能，单选 -->
        <div class="mb-1.5 grid grid-cols-5 gap-1 rounded-md p-0.5" :style="{ background: 'var(--surface-2)' }">
          <button v-for="b in BUCKET_FILTERS" :key="b.v"
                  class="h-[24px] rounded-[5px] text-[11px]"
                  :style="fieldFilter === b.v
                    ? { background: 'var(--surface)', color: 'var(--text)', boxShadow: '0 1px 2px rgba(0,0,0,.06)', fontWeight: 600 }
                    : { color: 'var(--muted)' }"
                  @click="fieldFilter = b.v">{{ b.label }}</button>
        </div>
        <div class="flex gap-1.5">
          <input
            v-model="newFields"
            class="field min-w-0 flex-1"
            :placeholder="fieldFilter === 'all'
              ? '先选一个分类，再在这里输入字段名'
              : `添加到「${BUCKET_FILTERS.find(b => b.v === fieldFilter)?.label}」`"
            @keydown.enter.prevent="addFields"
          />
          <button class="btn shrink-0 px-2" :disabled="editBusy || !newFields.trim()" @click="addFields">
            添加
          </button>
        </div>
        <div class="mt-1 text-[10.5px] leading-snug muted">
          可直接写 <span class="mono">Si</span>、<span class="mono">抗拉强度</span>（自动带出类型与单位）、
          <span class="mono">自定义指标:number:%</span>；逗号分隔可一次加多个。
          新字段进入当前选中的分类；合金牌号等标识字段请展开 YAML 直接加。
        </div>
        <details class="mt-1 text-[10.5px] leading-snug muted">
          <summary class="cursor-pointer select-none" :style="{ color: 'var(--text-soft)' }">
            怎么用
          </summary>
          <div class="mt-0.5 space-y-0.5">
            <div>1 · 分类按钮是筛子：点「成分/组织/工艺/性能」只看该类字段。</div>
            <div>2 · 加字段：先点分类 → 输入名称回车（如 <span class="mono">Ni</span> 或 <span class="mono">抗拉强度</span>）。</div>
            <div>3 · chip 上的 × 删除该字段；悬停看完整信息。</div>
            <div>4 · 「全部」态不能加字段，需先选分类。</div>
          </div>
        </details>
        <div v-if="editMsg" class="mt-1 text-[10.5px]" :style="{ color: 'var(--accent)' }">{{ editMsg }}</div>
        <!-- 高度取 5 行的整数倍（22px chip + 4px gap），避免最后一行被切一半。
             chip 可拖拽：拖 A 放到 B 上 = A 移到 B 前面（可跨分类）。 -->
        <div class="mt-1.5 flex max-h-[126px] min-w-0 flex-wrap gap-1 overflow-y-auto overflow-x-hidden">
          <span v-for="f in visibleChips" :key="f.internal"
                class="chip max-w-[132px] cursor-grab select-none active:cursor-grabbing"
                :style="dropHint === f.internal
                  ? { outline: '2px solid var(--accent)', outlineOffset: '-1px' }
                  : undefined"
                :class="{ 'opacity-40': dragKey === f.internal }"
                :title="`${f.label} · ${f.unit || '无单位'} · ${f.groupLabel} · 拖拽可排序`"
                draggable="true"
                @dragstart="onDragStart(f, $event)"
                @dragover.prevent="dropHint = f.internal"
                @dragleave="dropHint === f.internal && (dropHint = '')"
                @drop.prevent="onDropTo(f)"
                @dragend="dragKey = ''; dropHint = ''">
            <span class="truncate">{{ f.key }}<span v-if="f.unit" class="opacity-60">/{{ f.unit }}</span></span>
            <button class="ml-0.5 leading-none opacity-60 hover:opacity-100"
                    :title="`删除 ${f.key}`" @click="removeField(f.internal)">×</button>
          </span>
          <span v-if="!visibleChips.length" class="text-[11px] muted">
            {{ fieldFilter === 'all' ? '还没有字段，先在上面输入一个' : `该分类下没有字段` }}
          </span>
        </div>
      </div>

      <div class="flex items-center justify-between">
        <label class="text-[11.5px] muted">YAML / JSON</label>
        <button class="btn btn-ghost h-[22px] px-2 text-[11px]" @click="editing = !editing">
          {{ editing ? '收起' : '展开' }}
        </button>
      </div>

      <textarea
        v-show="editing"
        v-model="yamlModel"
        class="field w-full min-h-[240px] flex-1"
        spellcheck="false"
      ></textarea>

      <div class="flex gap-2">
        <button class="btn flex-1" :disabled="busy" @click="emit('validate')">校验</button>
        <button class="btn flex-1" :disabled="busy" @click="doSave">另存为模板</button>
      </div>
      <input
        v-model="saveName"
        class="field w-full"
        placeholder="模板文件名（留空则覆盖当前自定义模板）"
      />

      <div v-if="validation" class="text-[11.5px] leading-relaxed">
        <div v-if="errors.length" class="rounded-md border px-2 py-1.5"
             :style="{ borderColor: 'var(--err)', background: 'var(--err-weak)', color: 'var(--err)' }">
          <div class="mb-1 flex items-center justify-between font-medium">
            <span>{{ errors.length }} 处错误</span>
            <button class="btn btn-ghost h-[20px] px-1 text-[11px]" @click="showErrors = !showErrors">
              {{ showErrors ? '隐藏' : '展开' }}
            </button>
          </div>
          <ul v-show="showErrors" class="list-disc pl-4">
            <li v-for="(e, i) in errors" :key="i">
              <span class="mono">{{ e.path || '—' }}</span>：{{ e.msg }}
            </li>
          </ul>
        </div>
        <div v-else class="rounded-md border px-2 py-1.5"
             :style="{ borderColor: 'var(--ok)', background: 'var(--ok-weak)', color: 'var(--ok)' }">
          结构校验通过<span v-if="stats"> · {{ stats.fields }} 字段 / {{ stats.groups }} 组</span>
        </div>
        <ul v-if="warnings.length" class="mt-1.5 list-disc pl-4" :style="{ color: 'var(--warn)' }">
          <li v-for="(w, i) in warnings" :key="i">{{ w.msg }}</li>
        </ul>
      </div>
    </div>
  </section>
</template>
