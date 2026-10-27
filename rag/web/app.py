"""FastAPI 后端：本地界面。默认只监听 127.0.0.1。"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import traceback
import uuid

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config as cfg
from ..pipeline import RagPipeline
_EXCEL_TARGET = None


def excel_target():
    """返回 ExcelTarget 类；首次调用时才导入 pandas / openpyxl。"""
    global _EXCEL_TARGET
    if _EXCEL_TARGET is None:
        from ..writeback.excel import ExcelTarget
        _EXCEL_TARGET = ExcelTarget
    return _EXCEL_TARGET


from ..schemas import (apply_field_edit, builtin_schema, from_text, list_schemas,
                       parse_field_spec, resolve, save, schemas_dir)
from ..writeback.clean import clean_rows, dedupe_rows

# pandas + openpyxl 只在"导出 / 写回 Excel"时才需要，顶层导入会让每次启动
# 多付约 485 ms（pandas 304 + openpyxl 181）。这里改成首次使用时再加载。

app = FastAPI(title="MaterialRAG", version="0.2.0")
_WEB = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_WEB, "static")       # 旧版单页（dist 不存在时兜底）
_DIST = os.path.join(_WEB, "dist")           # Vite 构建产物

_pipe: RagPipeline | None = None


def pipe() -> RagPipeline:
    global _pipe
    if _pipe is None:
        _pipe = RagPipeline()
    return _pipe


def _err(e: Exception) -> JSONResponse:
    return JSONResponse(status_code=500,
                        content={"ok": False, "error": str(e),
                                 "trace": traceback.format_exc()[-1500:]})


# ---------------------------------------------------------------- 页面

# ---------------------------------------------------------------- 页面（见文件末尾：优先构建产物）

# ---------------------------------------------------------------- 状态

def ready_marker_path() -> str:
    return os.path.join(cfg.path("project.output_dir"), "web.ready")


def mark_ready(content: str = "") -> None:
    """写就绪标记，内容是活着那个服务进程的 pid（外部实例写 "running"）。

    启动脚本（start_web.vbs）轮询这个文件，而不是发 HTTP 请求：VBS 里 WinHttp
    的代理/超时行为不可控，检测一旦失效就会空等到超时上限（用户侧表现为
    "双击后两三秒到几十秒才出页面"）。文件判定是纯本地操作，毫秒级且没有网络
    语义可出错。pid 用来让脚本识别"标记是上次强杀进程留下的残骸"。
    """
    try:
        cfg.ensure_dir(cfg.path("project.output_dir"))
        with open(ready_marker_path(), "w", encoding="utf-8") as f:
            f.write(content or str(os.getpid()))
    except Exception:
        pass


def clear_ready() -> None:
    try:
        os.remove(ready_marker_path())
    except Exception:
        pass


@app.on_event("startup")
def _on_startup() -> None:
    mark_ready(str(os.getpid()))


@app.on_event("shutdown")
def _on_shutdown() -> None:
    clear_ready()


@app.get("/api/status")
def status():
    try:
        return {"ok": True, **pipe().status()}
    except Exception as e:
        return _err(e)


@app.get("/api/config")
def get_config():
    c = cfg.load()
    return {"ok": True,
            "pid": os.getpid(),
            "raw_dir": cfg.path("project.raw_dir"),
            "index_dir": cfg.path("project.index_dir"),
            "output_dir": cfg.path("project.output_dir"),
            "llm_base_url": c["llm"]["base_url"],
            "embedding_backend": c["embedding"]["backend"]}


# ---------------------------------------------------------------- 索引

class IngestReq(BaseModel):
    glob: str | None = None
    force: bool = False


@app.post("/api/ingest")
def ingest(req: IngestReq):
    try:
        p = pipe()
        p.ensure_models()
        return {"ok": True, **p.ingest(pdf_glob=req.glob, force=req.force)}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 问答

class AskReq(BaseModel):
    question: str
    top_k: int | None = None


@app.post("/api/ask")
def ask(req: AskReq):
    try:
        ans = pipe().ask(req.question, top_k=req.top_k)
        return {"ok": True, **ans.to_dict()}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 抽取

class ExtractReq(BaseModel):
    excel_path: str | None = None      # 不填 = 只抽取并显示，不涉及任何表格
    mode: str = "auto"
    max_chunks: int = 0
    skip_ids: list[str] = []           # 继续抽取：已抽过的块 id（与块列表顺序无关）
    headers: list[str] | None = None
    schema_name: str | None = None     # 模板名或路径；留空 = 内置模板
    schema_text: str | None = None     # 界面上直接传 YAML/JSON 文本
    workers: int | None = None         # 并发数；不填 = config.extract.workers


def _extract_payload(res: dict, excel_path: str | None) -> dict:
    """把管线结果整理成前端要的形状（同步接口与 SSE 任务共用）。"""
    recs = res["records"]
    rows, evidence = excel_target().to_rows(recs, res["specs"], trusted_only=True)
    specs = res["specs"]
    ordered = [s.column for s in specs.values() if any(s.column in r for r in rows)]
    if excel_path:
        headers = excel_target()(excel_path, cfg.load()).headers()
    else:
        headers = ordered
    sdef = res.get("schema_def")
    return {
        "ok": True,
        "stopped": bool(res.get("stopped_early")),
        "records_count": len(recs),
        "picked_chunks": res["picked_chunks"],
        "workers": res.get("workers", 1),
        "target_excel": excel_path or "",
        "schema_mode": (res.get("schema") or {}).get("mode", ""),
        "schema": {"matched": res["schema"].get("matched", []),
                   "custom": res["schema"].get("custom", []),
                   "name": (sdef.name if sdef else ""),
                   "field_count": (sdef.field_count() if sdef else 0),
                   "hash": (sdef.schema_hash if sdef else "")},
        "schema_groups": ([g.to_dict() for g in sdef.groups] if sdef else []),
        "dropped": res.get("dropped", []),
        "headers": headers,
        "rows": rows,
        "row_ids": [r.record_id for r in recs],
        "evidence": evidence,
        "records": [r.to_dict() for r in recs],
    }


class StartReq(ExtractReq):
    doc_ids: list[str] | None = None


# ---------------------------------------------------------------- 抽取前的规模预估
# 每块墙钟耗时经验值（含并发摊薄），以及测它时的并发数。首次没有实测值时用
# config 里的默认估计。
_SPEED = {"sec_per_chunk": None, "workers": 2}


def _note_speed(spc: float, workers: int = 2) -> None:
    """指数平滑，避免单次异常块把预估值带偏。"""
    old = _SPEED["sec_per_chunk"]
    _SPEED["sec_per_chunk"] = round(spc if old is None else old * 0.5 + spc * 0.5, 2)
    _SPEED["workers"] = workers


class PlanReq(BaseModel):
    doc_ids: list[str] | None = None
    mode: str = "table"


@app.post("/api/extract/plan")
def extract_plan(req: PlanReq):
    """开跑前告诉界面：各模式分别会抽多少块、预计多久。不加载模型，秒回。"""
    try:
        p = pipe()
        counts = {m: len(p.pick_chunks(mode=m, doc_ids=req.doc_ids))
                  for m in ("table", "auto", "all")}
        spc = _SPEED["sec_per_chunk"]
        if spc is None:
            spc = float(cfg.get("extract.sec_per_chunk", 60) or 60)
        return {"ok": True, "counts": counts,
                "picked": counts.get(req.mode, counts.get("table", 0)),
                "sec_per_chunk": round(spc, 2),
                "at_workers": _SPEED["workers"],
                "measured": _SPEED["sec_per_chunk"] is not None}
    except Exception as e:
        return _err(e)


@app.post("/api/extract")
def extract(req: ExtractReq):
    try:
        p = pipe()
        p.ensure_models()
        schema_arg = req.schema_text or req.schema_name or None
        res = p.extract(excel_path=req.excel_path, mode=req.mode,
                        max_chunks=req.max_chunks,
                        exclude_ids=req.skip_ids,
                        headers=req.headers,
                        schema=schema_arg, workers=req.workers)
        return _extract_payload(res, req.excel_path)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 异步抽取 + 进度

_JOBS: dict[str, dict] = {}


@app.post("/api/extract/start")
def extract_start(req: StartReq):
    """启动后台抽取任务，返回 job_id；进度用 SSE 订阅。"""
    # 只保留最近一个已完成任务，避免 _JOBS 无限堆积（刷新接管的兜底数据）
    for jid, j in list(_JOBS.items()):
        if j.get("done"):
            _JOBS.pop(jid, None)
    jid = uuid.uuid4().hex[:12]
    _JOBS[jid] = {"events": [], "cv": threading.Condition(), "done": False,
                  "result": None, "error": None, "started_at": time.time(),
                  "last": {}, "done_ids": [],
                  # 参数快照：记录本批是怎么发起的
                  "req": {"skip_ids": req.skip_ids, "max_chunks": req.max_chunks,
                          "mode": req.mode, "doc_ids": req.doc_ids or []}}

    def work() -> None:
        last: dict = {}
        try:
            p = pipe()
            p.ensure_models()
            schema_arg = req.schema_text or req.schema_name or None

            def on_ev(ev: dict) -> None:
                last.update(ev)
                _JOBS[jid]["last"] = dict(ev)
                # 累积本批已完成的块 id：刷新后靠它精确还原"抽到哪了"。
                # 不能用 req.skip_ids 代替——那是本批启动时的起点，不含本批新抽的块。
                ids = ev.get("chunk_ids")
                if ids:
                    _JOBS[jid]["done_ids"].extend(ids)
                _publish(jid, {"type": "progress", **ev})

            res = p.extract(excel_path=req.excel_path, doc_ids=req.doc_ids,
                            mode=req.mode, max_chunks=req.max_chunks,
                            exclude_ids=req.skip_ids,
                            headers=req.headers, schema=schema_arg,
                            workers=req.workers or 2, on_event=on_ev,
                            should_stop=lambda: bool(_JOBS[jid].get("cancel")))
            payload = _extract_payload(res, req.excel_path)
            _JOBS[jid]["result"] = payload
            # done 事件带上本批全部 chunk_id：即使中途某个订阅者漏了进度事件，
            # 光靠这一条也能把"已抽到哪"补回来（继续抽取的按钮靠它）。
            _publish(jid, {"type": "done", "result": payload,
                           "chunk_ids": list(_JOBS[jid]["done_ids"])})
        except Exception as e:                     # noqa: BLE001
            _JOBS[jid]["error"] = str(e)
            _publish(jid, {"type": "error", "error": str(e),
                           "trace": traceback.format_exc()[-1200:]})
        finally:
            # 唤醒所有订阅者：任务已经收工，别让它们干等到心跳超时
            job = _JOBS.get(jid)
            if job is not None:
                job["done"] = True
                job["finished_at"] = time.time()
                with job["cv"]:
                    job["cv"].notify_all()
            # 用实测墙钟时间更新「每块耗时」经验值，供界面开跑前预估
            if last.get("done") and last.get("elapsed"):
                _note_speed(last["elapsed"] / last["done"])

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "job_id": jid}


def _publish(jid: str, ev: dict) -> None:
    job = _JOBS.get(jid)
    if job is not None:
        _publish_event(job, ev)


def _publish_event(job: dict, ev: dict) -> None:
    """把一条事件记进日志并唤醒所有订阅者。

    早先每个任务只有一条 queue.Queue：事件是"取走就没"的。页面刷新后新页面订阅
    同一任务时，进度事件会被旧连接抢走一部分 —— 新页面拿不到 chunk_ids，
    「继续抽取」的按钮就没了。改成"事件日志 + 条件变量"后：每个订阅者各按自己的
    游标读，互不干扰；后连的先补发历史事件，再多晚连上都不丢；多个标签页同时看
    也不会互相抢。收尾事件统一在这里唤醒，避免订阅者干等到心跳超时。
    """
    job["events"].append(ev)
    if ev.get("type") in ("done", "error"):
        job["done"] = True
        job.setdefault("finished_at", time.time())
    cv = job.get("cv")
    if cv is not None:
        with cv:
            cv.notify_all()


def _iter_events(job: dict, start: int = 0):
    """按游标产出事件；None 表示一次心跳（让代理/浏览器别掐断长连接）。"""
    cv = job["cv"]
    i = start
    while True:
        with cv:
            if i >= len(job["events"]) and not job["done"]:
                cv.wait(timeout=15)
            if i < len(job["events"]):
                ev = job["events"][i]
                i += 1
            elif job["done"]:
                return
            else:
                ev = None
        yield ev                                  # None = 心跳


# 已完成任务保留多久还肯"交还"给刷新后的页面。
# 目的是兜住手滑刷新（几十秒~几分钟内），而不是让每一次后续刷新都去重新
# 搬运并渲染上一批的完整结果——结果里带逐字段原文证据，几十块就有上百 KB~MB
# 量级，每次刷新都传一遍、解析一遍、重绘一遍，页面就"变慢了"。
_ADOPT_TTL_S = float(cfg.get("web.adopt_ttl_s", 600))


def _running_job(now: float | None = None) -> dict:
    """找出一个「可接管」的抽取任务，供页面刷新后恢复现场。

    两种都算可接管：
      1. 正在跑的（done=False）——重连 SSE 接着看进度；
      2. 已跑完且**刚结束不久**（≤ web.adopt_ttl_s，默认 10 分钟）——把结果还回去，
         免得刷新一次就把几十分钟的成果丢了。超过这个窗口就只还原"已抽到哪"
         （done_ids），不再搬运结果，刷新因此保持轻快。

    now 可注入，便于自测（纯函数，不依赖真实时钟）。
    """
    now = time.time() if now is None else now
    for jid, j in list(_JOBS.items()):
        if not j.get("done"):
            return {"job_id": jid, "done": False,
                    "started_at": j.get("started_at", 0.0),
                    "progress": dict(j.get("last") or {}),
                    "done_ids": list(j.get("done_ids") or []),
                    "result": None, "req": dict(j.get("req") or {})}
    for jid, j in list(_JOBS.items()):
        if not (j.get("done") and j.get("result")):
            continue
        finished = j.get("finished_at") or j.get("started_at", 0.0)
        fresh = (now - finished) <= _ADOPT_TTL_S
        return {"job_id": jid, "done": True,
                "started_at": j.get("started_at", 0.0),
                "progress": dict(j.get("last") or {}),
                "done_ids": list(j.get("done_ids") or []),
                "result": j.get("result") if fresh else None,
                "result_expired": not fresh,
                "req": dict(j.get("req") or {})}
    return {}


@app.get("/api/extract/running")
def extract_running():
    """页面加载时调一次：有没有任务能接管（刷新/手滑关页面后恢复进度与结果）。"""
    info = _running_job()
    if not info:
        return {"ok": True, "running": False}
    return {"ok": True, "running": True, **info}


@app.post("/api/extract/cancel/{job_id}")
def extract_cancel(job_id: str):
    """请求停止：已跑完的批次结果保留，剩下的块直接放弃。"""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    job["cancel"] = True
    return {"ok": True, "job_id": job_id, "cancel": True}


@app.get("/api/extract/stream/{job_id}")
def extract_stream(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    def gen():
        # 每个订阅者各按自己的游标读事件日志：先补发历史（刷新/新标签页连上来
        # 不丢进度），再等新事件；多个订阅者互不干扰。
        for ev in _iter_events(job):
            if ev is None:
                yield ": ping\n\n"          # 心跳，防止代理断开
                continue
            yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            if ev.get("type") in ("done", "error"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class FillReq(BaseModel):
    excel_path: str
    records: list[dict]          # MaterialRecord.to_dict() 的结果（含证据）
    confirm: bool = False


@app.post("/api/fill")
def fill(req: FillReq):
    """写入 Excel。

    安全约束：本端点**不接受**平铺的行数据，只接受带证据的原始记录。
    服务端会重新读取表头、重建记录、并强制 trusted_only=True ——
    未通过原文核验的字段在这一步必然被过滤，前端无法绕过。
    """
    try:
        from ..models import MaterialRecord
        target = excel_target()(req.excel_path, cfg.load())
        specs = target.schema()["specs"]
        records = [MaterialRecord.record_from_dict(r) for r in req.records]
        rows, _ev = excel_target().to_rows(records, specs, trusted_only=True)
        w = target.write(rows, dry_run=not req.confirm)
        if not w.get("dry_run"):
            from ..writeback import diff as _diffmod     # 延迟导入：拉起 openpyxl
            w["report_text"] = _diffmod.format_report(w)
        return {"ok": True, **w}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 模型探测

@app.get("/api/llm")
def llm_health():
    try:
        h = pipe().llm.health()
        if not h.get("ok"):
            # 静默启动（pythonw）没有控制台：连不上必须落到日志里，
            # 否则界面只显示"未连接"，事后完全无从查起。
            print(f"[llm] 健康检查失败：{h.get('error') or '未知原因'} @ {h.get('base_url')}")
        return {"ok": True, **h}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 抽取模板

class SchemaTextReq(BaseModel):
    text: str = ""                     # YAML / JSON 文本
    save_as: str | None = None         # 给了就落盘成用户模板
    name: str | None = None


@app.get("/api/docs")
def docs_list():
    """已索引文献清单（供界面勾选抽取范围）。"""
    try:
        p = pipe()
        cnt: dict[str, int] = {}
        tabs: dict[str, int] = {}
        for c in p.store.chunks:
            cnt[c.doc_id] = cnt.get(c.doc_id, 0) + 1
            if c.kind == "table":
                tabs[c.doc_id] = tabs.get(c.doc_id, 0) + 1
        items = []
        for did, meta in (p.store.metas or {}).items():
            items.append({"doc_id": did,
                          "doc_name": meta.get("doc_name", did),
                          "page_count": int(meta.get("page_count", 0) or 0),
                          "chunks": cnt.get(did, 0),
                          "tables": tabs.get(did, 0)})
        items.sort(key=lambda x: x["doc_name"])
        return {"ok": True, "docs": items}
    except Exception as e:
        return _err(e)


@app.get("/api/schema/templates")
def schema_templates():
    try:
        return {"ok": True, "templates": list_schemas()}
    except Exception as e:
        return _err(e)


@app.get("/api/schema/{name}")
def schema_get(name: str):
    """取模板全文（含 YAML 原文，供编辑器载入）。"""
    try:
        if name in ("", "builtin", "default"):
            sdef = builtin_schema()
            path = ""
        else:
            sdef, errs, _w = resolve(name)
            if sdef is None:
                raise HTTPException(status_code=404,
                                    detail=errs[0]["msg"] if errs else "模板不存在")
            path = sdef.path
        text = yaml.safe_dump(sdef.to_yaml_dict(), allow_unicode=True,
                              sort_keys=False, default_flow_style=False)
        return {"ok": True, "schema": sdef.to_dict(), "yaml": text, "path": path}
    except HTTPException:
        raise
    except Exception as e:
        return _err(e)


@app.post("/api/schema/validate")
def schema_validate(req: SchemaTextReq):
    """只校验不保存：界面边编辑边校验，错误逐条定位。"""
    sdef, errors, warnings = from_text(req.text or "")
    specs = sdef.to_specs() if sdef else {}
    return {
        "ok": sdef is not None and not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "fields": len(specs),
            "groups": len(sdef.groups) if sdef else 0,
            "brief_chars": (len("\n".join(f"- {k}" for k in specs)) if specs else 0),
            "schema_hash": sdef.schema_hash if sdef else "",
        },
        "schema": sdef.to_dict() if sdef else None,
    }


@app.post("/api/schema")
def schema_save(req: SchemaTextReq):
    """校验通过后落盘为用户模板。"""
    sdef, errors, warnings = from_text(req.text or "")
    if sdef is None or errors:
        return JSONResponse(status_code=400,
                            content={"ok": False, "errors": errors, "warnings": warnings})
    if req.save_as:
        safe = "".join(c for c in req.save_as if c.isalnum() or c in "-_") or "schema"
        path = os.path.join(schemas_dir(), f"{safe}.yaml")
    else:
        path = ""
    path = save(sdef, path)
    return {"ok": True, "path": path, "schema": sdef.to_dict(), "warnings": warnings}


class FieldEditReq(BaseModel):
    text: str                       # 当前模板全文（YAML/JSON）
    op: str = "add"                 # add | remove | move
    group: str = ""                 # add：目标分组 key；move：目标分组 key
    fields: list[str] = []          # add：用户原始输入，如 "Si, 延伸率" / "硬度HV:number:HV"
    keys: list[str] = []            # remove/move：内部 key，如 "properties.UTS"
    before: str = ""                # move：插到该内部 key 之前；空 = 追加到组末尾


@app.post("/api/schema/edit")
def schema_edit(req: FieldEditReq):
    """按字段名增删（支持手输，名称能匹配内置词典时自动带出类型与单位）。

    前端没有 YAML 解析器，且"中文名 -> 标准字段"要用后端词典，所以放在这里做。
    """
    try:
        text, errs, info = apply_field_edit(req.text, op=req.op, group=req.group,
                                            fields=req.fields, keys=req.keys,
                                            before=req.before)
        if text is None:
            return JSONResponse(status_code=400,
                               content={"ok": False, "errors": errs, "info": info})
        sdef, e2, warns = from_text(text)
        # 回显解析结果（按逗号拆开后的每一条），前端直接刷新 chips 与校验框
        preview = info.get("parsed", [])
        return {"ok": True, "text": text, "info": info,
                "preview": preview,
                "schema": sdef.to_dict() if sdef else None,
                "errors": e2, "warnings": warns,
                "stats": {"fields": sdef.field_count() if sdef else 0,
                          "groups": len(sdef.groups) if sdef else 0,
                          "schema_hash": sdef.schema_hash if sdef else ""}}
    except Exception as e:
        return _err(e)


@app.delete("/api/schema/{name}")
def schema_delete(name: str):
    """删除用户模板（内置模板不可删）。"""
    try:
        sdef, errs, _w = resolve(name)
        if sdef is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        if sdef.source == "builtin" or os.sep + "builtin" + os.sep in sdef.path:
            raise HTTPException(status_code=400, detail="内置模板不可删除")
        os.remove(sdef.path)
        return {"ok": True, "deleted": sdef.path}
    except HTTPException:
        raise
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 导出与清洗

class ExportReq(BaseModel):
    records: list[dict]              # MaterialRecord.to_dict()（服务端据此重建，防止绕过核验）
    schema_text: str | None = None   # 当前模板 YAML；留空用内置模板
    format: str = "csv"              # csv | xlsx
    excel_path: str | None = None    # xlsx 必填；文件不存在则新建，存在则追加（带备份+diff）
    clean: dict | None = None        # 清洗开关，见 _clean_opts


def _clean_opts(raw: dict | None) -> dict:
    d = raw or {}
    return {
        "dedupe": bool(d.get("dedupe", True)),
        "drop_unverified": bool(d.get("drop_unverified", True)),
        "trim": bool(d.get("trim", True)),
        "unicode": bool(d.get("unicode", True)),
        "space_in_number": bool(d.get("space_in_number", True)),
        # 默认关：表头一般不含单位，贸然去重会把单位信息丢掉
        "strip_unit": bool(d.get("strip_unit", False)),
        # 补余量列：铝合金文献不列 Al（余量），导出时补一列 Al 填 "Bal."。
        # 只在表里本来没有 Al 列时才补，所以默认开着也安全。
        "balance_col": bool(d.get("balance_col", True)),
    }


def _is_al_column(header: str) -> bool:
    """判断表头是不是 Al 列（兼容 "Al" / "Al (wt.%)" / "Al/wt.%" 等写法）。"""
    s = re.sub(r"[\(\[].*?[\)\]]", "", str(header))   # 去掉括号里的单位
    s = s.split("/")[0].strip()
    return s.lower() == "al"


def _ordered_headers(rows: list[dict], specs: dict) -> list[str]:
    return [s.column for s in specs.values() if any(s.column in r for r in rows)]


def _write_new_xlsx(path: str, headers: list[str], rows: list[dict]) -> dict:
    """目标文件不存在时新建（不涉及备份/diff：新文件没有旧内容可破坏）。"""
    import openpyxl
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    wb.save(path)
    return {"ok": True, "created": True, "target": path, "rows": len(rows),
            "columns": len(headers), "unexpected_count": 0}


@app.post("/api/export")
def export_data(req: ExportReq):
    """导出 CSV / Excel（含基础清洗）。"""
    try:
        from ..models import MaterialRecord

        if req.schema_text:
            sdef, errs, _w = from_text(req.schema_text)
            if sdef is None:
                return JSONResponse(status_code=400,
                                    content={"ok": False, "errors": errs})
        else:
            sdef = builtin_schema()
        specs = sdef.to_specs()
        opts = _clean_opts(req.clean)

        recs = [MaterialRecord.record_from_dict(r) for r in req.records]
        rows, _ev = excel_target().to_rows(recs, specs,
                                        trusted_only=opts["drop_unverified"])
        deduped = 0
        if opts["dedupe"]:
            rows, deduped = dedupe_rows(rows)

        # 只在表头本身写了单位时才去掉值里的重复单位，避免丢信息
        unit_map = {}
        for sp in specs.values():
            u = (sp.unit or "").strip()
            if u and u.lower().replace(" ", "") in sp.column.lower().replace(" ", ""):
                unit_map[sp.column] = u
        rules = [r for r in ("unicode", "trim", "space_in_number", "strip_unit")
                 if opts.get(r)]
        rows, report = clean_rows(rows, rules, unit_map)

        headers = _ordered_headers(rows, specs)

        # ---- 补余量列 ----
        # 铝合金文献的成分表不列 Al（Al 是余量），模板删掉 Al 字段后导出就没有这一列。
        # 这里补一列 Al=Bal.，忠实反映"余量"语义（不是算出来的数值，不存在臆造）。
        # 表里本来就有 Al 列时不重复添加。
        balance_added = False
        if opts.get("balance_col") and rows:
            # 判断依据是「模板有没有声明 Al 字段」，不是「这批数据里有没有值」：
            # 模板即契约——模板要 Al 就按 Al 走（有值显示、没值按既有行为忽略该列），
            # 不能拿 Bal. 去冒充抽到了。只有模板根本不要 Al（无余量模板）时才补。
            has_al_spec = any(_is_al_column(s.column) for s in specs.values())
            if not has_al_spec and not any(_is_al_column(h) for h in headers):
                comp_cols = [s.column for s in specs.values()
                             if s.group == "composition" and s.column in headers]
                pos = headers.index(comp_cols[-1]) + 1 if comp_cols else len(headers)
                headers.insert(pos, "Al")
                for r in rows:
                    r["Al"] = "Bal."
                balance_added = True

        base = "".join(c for c in (sdef.name or "export") if c.isalnum() or c in "-_") or "export"
        stamp = time.strftime("%Y%m%d_%H%M%S")

        if req.format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(headers)
            for r in rows:
                w.writerow(["" if r.get(h) is None else r.get(h) for h in headers])
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM：Excel 打开不乱码
            filename = f"{base}_{stamp}.csv"
            from urllib.parse import quote
            return StreamingResponse(
                iter([data]), media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                    "X-Clean-Report": json.dumps(report, ensure_ascii=False)[:2000],
                    "X-Rows": str(len(rows)),
                    "X-Deduped": str(deduped),
                    "X-Balance-Added": "1" if balance_added else "0",
                })

        if req.format != "xlsx":
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": f"不支持的格式 {req.format}"})
        if not req.excel_path:
            return JSONResponse(status_code=400,
                                content={"ok": False,
                                         "error": "导出 Excel 需要先填目标路径"})
        target = os.path.abspath(req.excel_path)
        if os.path.exists(target):
            wres = excel_target()(target, cfg.load()).write(rows, dry_run=False)
        else:
            wres = _write_new_xlsx(target, headers, rows)
        return {"ok": True, "format": "xlsx", "write": wres,
                "rows": len(rows), "columns": len(headers),
                "headers": headers, "deduped": deduped,
                "balance_added": balance_added,
                "clean_report": report, "schema": sdef.to_dict()}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------- 页面生命周期
# 目标：关掉浏览器页面 = 停掉服务，不需要额外的停止脚本。
# 四个机制配合，避免误杀：
#   1) 页面关闭时 sendBeacon 发 /api/session/bye，进入 shutdown_grace_s（默认 15 秒）倒计时；
#   2) 刷新页面会立刻发 /api/session/hello，倒计时被取消（刷新不会杀服务）；
#   3) 页面每 15 秒心跳；若浏览器被强杀没发 bye，超过 idle_timeout_s 无心跳也自停；
#   4) 倒计时到期时若**有抽取任务在跑则不自杀**——页面可能被别的窗口冲掉，
#      服务留着让任务跑完，回来重开页面自动接管；任务结束后由第 3 条的 idle 兜底收尾。

_CLIENTS: dict[str, float] = {}      # session_id -> 最后心跳时间
_close_at: float = 0.0               # >0 表示已排定关闭时刻
_last_seen: float = 0.0
_watch_started = False


class SessionReq(BaseModel):
    id: str


def _cfg_num(key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _job_running() -> bool:
    return any(not j.get("done") for j in _JOBS.values())


def _do_shutdown(reason: str) -> None:
    print(f"[shutdown] {reason}")
    # 进行中的抽取任务直接随进程结束（LLM 调用一并中断），这正是"关页面=停这一轮工作"
    for j in _JOBS.values():
        j["done"] = True
    srv = getattr(app.state, "uvicorn_server", None)
    if srv is not None:
        srv.should_exit = True


def _watch_loop() -> None:
    global _close_at, _last_seen
    grace = _cfg_num("web.shutdown_grace_s", 15)
    idle = _cfg_num("web.idle_timeout_s", 180)
    while True:
        time.sleep(1.0)
        now = time.time()
        # 心跳过期的会话（标签页被强杀、电脑休眠后唤醒等）清掉
        for cid, ts in list(_CLIENTS.items()):
            if now - ts > 600:
                _CLIENTS.pop(cid, None)
        if _close_at:
            if now < _close_at:
                continue
            if _CLIENTS:                     # 倒计时期间又连上了（多半是刷新）
                _close_at = 0.0
                continue
            if _job_running():
                # 有抽取在跑就不自杀：用户可能只是被别的窗口把页面冲掉了，
                # 服务留着，任务跑完，回来重开页面会自动接管进度与结果。
                # 任务结束后由下面的 idle 兜底（无心跳 3 分钟）收尾。
                _close_at = 0.0
                continue
            _close_at = 0.0
            _do_shutdown(f"页面已关闭（宽限 {grace:.0f}s 内无重连）")
            return
        # 兜底：完全没有页面连着，且没有任务在跑
        if (not _CLIENTS and _last_seen and idle > 0
                and now - _last_seen > idle and not _job_running()):
            _do_shutdown(f"无页面心跳超过 {idle:.0f}s")
            return


def _ensure_watch() -> None:
    global _watch_started
    if _watch_started:
        return
    _watch_started = True
    threading.Thread(target=_watch_loop, daemon=True).start()


@app.post("/api/session/hello")
def session_hello(req: SessionReq):
    """页面加载：登记会话并取消可能已排定的关闭（刷新不杀服务）。"""
    global _close_at, _last_seen
    _CLIENTS[req.id] = time.time()
    _last_seen = time.time()
    cancelled = _close_at > 0
    _close_at = 0.0
    _ensure_watch()
    return {"ok": True, "cancelled_close": cancelled,
            "shutdown_on_close": bool(cfg.get("web.shutdown_on_page_close", True)),
            "grace_s": _cfg_num("web.shutdown_grace_s", 15)}


@app.post("/api/session/ping")
def session_ping(req: SessionReq):
    global _close_at, _last_seen
    _CLIENTS[req.id] = time.time()
    _last_seen = time.time()
    _close_at = 0.0
    # 心跳顺带把模型连接状态带回去（health 有 60 秒缓存，几乎不花钱）：
    # 顶部芯片每 15 秒自动校正一次，一次网络抖动造成的"未连接"会自己恢复，
    # 不用刷新页面。
    try:
        h = pipe().llm.health(ttl=60)
        llm = {"ok": bool(h.get("ok")), "using": h.get("using"),
               "error": h.get("error")}
    except Exception as e:                         # noqa: BLE001
        llm = {"ok": False, "error": str(e)}
    return {"ok": True, "llm": llm}


@app.post("/api/session/bye")
def session_bye(req: SessionReq):
    """页面卸载：最后一个页面走了才排定关闭。"""
    global _close_at, _last_seen
    _CLIENTS.pop(req.id, None)
    _last_seen = time.time()
    if not cfg.get("web.shutdown_on_page_close", True):
        return {"ok": True, "scheduled": False, "note": "配置未开启关页面停服务"}
    _ensure_watch()
    if _CLIENTS:
        return {"ok": True, "scheduled": False, "remaining_clients": len(_CLIENTS)}
    _close_at = time.time() + _cfg_num("web.shutdown_grace_s", 15)
    return {"ok": True, "scheduled": True,
            "grace_s": _cfg_num("web.shutdown_grace_s", 15)}


@app.post("/api/session/shutdown")
def session_shutdown():
    """界面上的「立即停止服务」按钮（相当于关页面，但不等宽限）。"""
    _do_shutdown("用户点击停止服务")
    return {"ok": True}


# ---------------------------------------------------------------- 前端静态资源

_NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
def index() -> FileResponse:
    """优先返回 Vite 构建产物；没有构建过就退回旧版单页。

    no-store：浏览器默认会对没有缓存头的 HTML 做"启发式缓存"，改过前端代码
    重新构建后，用户看到的还是旧页面（表现就是"修好了但界面没变"）。
    资产文件名带 hash，跟着 HTML 走即可，不需要额外处理。
    """
    built = os.path.join(_DIST, "index.html")
    if os.path.isfile(built):
        return FileResponse(built, headers=_NO_STORE)
    return FileResponse(os.path.join(_STATIC, "index.html"), headers=_NO_STORE)


@app.get("/api/ui")
def ui_info():
    """给界面/自检用：当前前端是构建版还是兜底版、以及构建时间。"""
    p = os.path.join(_DIST, "index.html")
    built = os.path.isfile(p)
    return {"ok": True, "dist": _DIST, "built": built,
            "built_at": (os.path.getmtime(p) if built else 0),
            "assets": os.path.isdir(os.path.join(_DIST, "assets"))}


if os.path.isdir(_DIST):
    _assets = os.path.join(_DIST, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{fname}")
    def dist_file(fname: str):
        """构建产物的根级文件（favicon 等）。带路径穿越防护。"""
        p = os.path.normpath(os.path.join(_DIST, fname))
        if p.startswith(os.path.normpath(_DIST)) and os.path.isfile(p):
            return FileResponse(p)
        raise HTTPException(status_code=404, detail="not found")
