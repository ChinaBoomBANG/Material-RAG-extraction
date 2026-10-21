// 极简 API 封装：统一 JSON 解析与错误抛出，组件里不再重复判 err。
const json = async (url, opt = {}) => {
  let res
  try {
    res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opt })
  } catch (e) {
    throw new Error('无法连接后端（服务是否已启动？）')
  }
  const data = await res.json().catch(() => ({ ok: false, error: '响应不是合法 JSON' }))
  if (!res.ok || data.ok === false) {
    // 后端校验失败时是 {errors:[{path,msg}]}，优先把第一条的 msg 抛出来
    const first = Array.isArray(data.errors) && data.errors[0]
    const detail = first ? (first.msg || String(first)) : ''
    throw new Error(data.detail || data.error || detail || `HTTP ${res.status}`)
  }
  return data
}

const post = (url, body) => json(url, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  status: () => json('/api/status'),
  llm: () => json('/api/llm'),
  ui: () => json('/api/ui'),
  docs: () => json('/api/docs'),
  templates: () => json('/api/schema/templates'),
  schemaGet: (name) => json(`/api/schema/${encodeURIComponent(name)}`),
  schemaValidate: (text) => post('/api/schema/validate', { text }),
  schemaSave: (text, name) => post('/api/schema', { text, save_as: name }),
  schemaEdit: (payload) => post('/api/schema/edit', payload),
  startExtract: (payload) => post('/api/extract/start', payload),
  extractPlan: (payload) => post('/api/extract/plan', payload),
  cancelExtract: (jobId) => post(`/api/extract/cancel/${jobId}`, {}),
  runningJob: () => json('/api/extract/running'),
  ask: (question, top_k) => post('/api/ask', { question, top_k }),
  sessionHello: (id) => post('/api/session/hello', { id }),
  sessionPing: (id) => post('/api/session/ping', { id }),
  sessionBye: (id) => post('/api/session/bye', { id }),
  stopServer: () => post('/api/session/shutdown', {}),
  exportXlsx: (payload) => post('/api/export', { ...payload, format: 'xlsx' }),
  // CSV 走 blob 下载，顺手把清洗报告从响应头里读出来
  exportCsv: async (payload) => {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, format: 'csv' }),
    })
    if (!res.ok) {
      const d = await res.json().catch(() => ({}))
      throw new Error(d.error || d.detail || `HTTP ${res.status}`)
    }
    const blob = await res.blob()
    const cd = res.headers.get('Content-Disposition') || ''
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd)
    const name = m ? decodeURIComponent(m[1]) : 'export.csv'
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 5000)
    let report = null
    try { report = JSON.parse(res.headers.get('X-Clean-Report') || 'null') } catch (e) { /* ignore */ }
    return {
      filename: name,
      rows: Number(res.headers.get('X-Rows') || 0),
      deduped: Number(res.headers.get('X-Deduped') || 0),
      report,
    }
  },
  ingest: (payload) => post('/api/ingest', payload),
}

// SSE：按事件流逐条消费，返回一个可 close 的句柄
export function streamProgress(jobId, onEvent, onError) {
  const es = new EventSource(`/api/extract/stream/${jobId}`)
  es.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data)
      onEvent(ev)
      if (ev.type === 'done' || ev.type === 'error') es.close()
    } catch (e) { /* 心跳或半包，忽略 */ }
  }
  es.onerror = () => {
    es.close()
    onError && onError(new Error('进度连接中断（任务可能仍在后台运行）'))
  }
  return es
}
