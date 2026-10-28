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
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
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
_DIST = os.path.join(_WEB, "dist")           # Vite 构建产物

_pipe: RagPipeline | None = None


def pipe() -> RagPipeline:
    global _pipe
    if _pipe is None:
        _pipe = RagPipeline()
    return _pipe


def _log(msg: str) -> None:
    """写一行带时间戳的运行日志。

    静默启动（pythonw）没有控制台，stdout/stderr 被重定向到 data/output/web.log，
    所以后台抽取的成败必须靠这里留痕——否则任务悄悄失败、结果不更新，
    用户看到的永远是上一批数据，却查不到原因。
    """
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _err(e: Exception) -> JSONResponse:
    # 异常必须落到日志里：之前只回了 500，静默启动时没人看得见，
    # 表现就是"点了没反应 / 数据没更新"。
    _log(f"[err] {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    return JSONResponse(status_code=500,
                        content={"ok": False, "error": str(e),
                                 "trace": traceback.format_exc()[-1500:]})


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
            "embedding_backend": c["embedding"]["backend"],
            # 并发/批处理：前端按云端配额给可选范围，后端再夹一层硬上限
            "extract_workers": int(cfg.get("extract.workers", 2) or 2),
            "extract_max_workers": int(cfg.get("extract.max_workers", 8) or 8),
            "extract_batch_chunks": int(cfg.get("extract.batch_chunks", 1) or 1)}


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


def _save_rounds_log(rounds: dict) -> dict:
    """把逐字段变更日志落成 CSV（会很长，不塞进前端 payload）。"""
    log = rounds.get("log") or []
    out = {k: v for k, v in rounds.items() if k != "log"}
    out["log_count"] = len(log)
    out["log_head"] = log[:20]
    if not log:
        return out
    try:
        import csv
        d = cfg.load()["project"]["output_dir"]
        if not os.path.isabs(d):
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), d)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"rounds_log_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
            w.writeheader()
            for r in log:
                w.writerow(r)
        out["log_csv"] = p
    except Exception as e:                                  # noqa: BLE001
        out["log_csv_error"] = f"{type(e).__name__}: {e}"
    return out


def _extract_payload(res: dict, excel_path: str | None) -> dict:
    """把管线结果整理成前端要的形状（供 SSE 任务与导出复用）。"""
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
        "process_card": res.get("process_card"),
        "rounds": (_save_rounds_log(res["rounds"]) if isinstance(res.get("rounds"), dict)
                   else res.get("rounds")),
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


# ---------------------------------------------------------------- 异步抽取 + 进度
_JOBS: dict[str, dict] = {}


@app.post("/api/extract/start")
def extract_start(req: StartReq):
    """启动后台抽取任务，返回 job_id；进度用 SSE 订阅。"""
    running_ids = [j for j, jb in _JOBS.items() if not jb.get("done")]
    if running_ids:
        # 连点「开始抽取」会叠出多个并发任务：一起打云端配额（429），
        # 且都往同一个 last_extract.json 写，最后写完的那个说了算 —— 结果不可预测。
        return JSONResponse(status_code=409, content={
            "ok": False, "job_id": running_ids[0],
            "error": "已经有一个抽取任务在跑，请先等它结束或点「停止」。",
            "running_jobs": running_ids})
    # 只保留最近一个已完成任务，避免 _JOBS 无限堆积（刷新接管的兜底数据）
    for jid, j in list(_JOBS.items()):
        if j.get("done"):
            _JOBS.pop(jid, None)
    # 不带 skip_ids = 全新开始：上一批（含盘上那份）一起作废，否则中途关掉页面
    # 再打开，界面会把本该丢弃的上一批结果又显示出来。
    if not req.skip_ids:
        _clear_last_result()
    jid = uuid.uuid4().hex[:12]
    _JOBS[jid] = {"events": [], "cv": threading.Condition(), "done": False,
                  "result": None, "error": None, "started_at": time.time(),
                  "last": {}, "done_ids": [],
                  # 参数快照：记录本批是怎么发起的
                  "req": {"skip_ids": req.skip_ids, "max_chunks": req.max_chunks,
                          "mode": req.mode, "doc_ids": req.doc_ids or []}}

    _log(f"[extract] 启动 {jid}：mode={req.mode} max_chunks={req.max_chunks} "
         f"workers={req.workers or 2} skip={len(req.skip_ids or [])} "
         f"doc_ids={len(req.doc_ids or []) or '全部'}")

    def work() -> None:
        last: dict = {}
        used_workers = int(req.workers or 2)
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
            # 实测并发以管线为准（会被 extract.max_workers 夹住），记下来供预估换算
            used_workers = int(res.get("workers") or used_workers)
            payload = _extract_payload(res, req.excel_path)
            _JOBS[jid]["result"] = payload
            _save_last_result(jid, _JOBS[jid], payload)
            # done 事件带上本批全部 chunk_id：即使中途某个订阅者漏了进度事件，
            # 光靠这一条也能把"已抽到哪"补回来（继续抽取的按钮靠它）。
            _publish(jid, {"type": "done", "result": payload,
                           "chunk_ids": list(_JOBS[jid]["done_ids"])})
            _log(f"[extract] {jid} 完成：{payload.get('picked_chunks')} 块 / "
                 f"{payload.get('records_count')} 条记录（已落盘）")
        except Exception as e:                     # noqa: BLE001
            _JOBS[jid]["error"] = str(e)
            _JOBS[jid]["failed_at"] = time.time()
            _save_last_failure(jid, _JOBS[jid], e, traceback.format_exc()[-1200:])
            _log(f"[extract] {jid} 失败：{type(e).__name__}: {e}\n"
                 f"{traceback.format_exc()[-1200:]}")
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
                _note_speed(last["elapsed"] / last["done"], used_workers)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "job_id": jid}


# 上一批结果落盘的位置（data/output/ 已 gitignore，且只在本地，不外传）。
# 为什么必须落盘：关掉页面后服务会自停，进程一没，内存里的结果就没了；
# 而「已抽过哪些块」存在浏览器 localStorage 里，所以重开后按钮还在、跳过也照常，
# 唯独右边的结果空了 —— 这个文件就是补上那一半。
_LAST_RESULT_FILE = "last_extract.json"


def _last_result_path() -> str:
    return os.path.join(cfg.path("project.output_dir"), _LAST_RESULT_FILE)


def _read_last_result() -> dict:
    try:
        with open(_last_result_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_payload(prev: dict | None, nxt: dict) -> dict:
    """把新一批结果接到上一批后面（前端 mergeResults 的服务端孪生）。

    只在落盘续接时用：续抽的每一批单独看只有这一批的记录，不拼起来，
    重开页面就只剩最后一批。表头在两批之间可能变（中途改过模板），
    旧行按新表头对齐、缺的补空 —— 与前端保持同样的处理。
    """
    if not prev:
        return nxt
    headers = nxt.get("headers") or prev.get("headers") or []
    old_rows = (prev.get("rows") if (prev.get("headers") or []) == headers
                else [{h: r.get(h, "") for h in headers}
                      for r in (prev.get("rows") or [])])
    out = dict(nxt)
    out["headers"] = headers
    out["rows"] = list(old_rows) + list(nxt.get("rows") or [])
    for k in ("row_ids", "dropped", "records"):
        out[k] = list(prev.get(k) or []) + list(nxt.get(k) or [])
    out["evidence"] = {**(prev.get("evidence") or {}), **(nxt.get("evidence") or {})}
    for k in ("records_count", "picked_chunks"):
        out[k] = (prev.get(k) or 0) + (nxt.get(k) or 0)
    return out


def _save_last_result(jid: str, job: dict, payload: dict) -> None:
    """任务收工后把结果写盘（原子写，避免半截文件）。"""
    try:
        prev = _read_last_result()
        # 续抽（带了 skip_ids）就接在上一批后面；全新开始则整份覆盖。
        continuing = bool((job.get("req") or {}).get("skip_ids"))
        prev_result = prev.get("result") if continuing else None
        prev_ids = prev.get("done_ids") if continuing else []
        data = {
            "job_id": jid,
            "started_at": job.get("started_at", 0.0),
            "finished_at": job.get("finished_at") or time.time(),
            "done_ids": list(dict.fromkeys(list(prev_ids or [])
                                           + list(job.get("done_ids") or []))),
            "req": dict(job.get("req") or {}),
            "result": _merge_payload(prev_result, payload),
        }
        cfg.ensure_dir(cfg.path("project.output_dir"))
        p = _last_result_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:                              # noqa: BLE001
        pass


def _clear_last_result() -> None:
    """全新开始时把盘上的上一批清掉，免得中途关页面再开又冒出上一批的结果。"""
    try:
        os.remove(_last_result_path())
    except OSError:
        pass


def _save_last_failure(jid: str, job: dict, e: Exception, trace: str) -> None:
    """任务失败也要留痕，保留原有结果。

    不然会出现这种局面：新一批悄悄失败 -> 结果文件没更新 -> 重开页面把上一批
    原样显示出来，用户以为"抽完了"（其实看到的是旧数据）。
    现在失败会写进同一个文件，重开时界面能明确提示"上一批失败了"。
    """
    try:
        prev = _read_last_result()
        prev.update({"failed_job_id": jid, "failed_at": job.get("failed_at")
                     or time.time(), "failed_error": str(e), "failed_trace": trace})
        cfg.ensure_dir(cfg.path("project.output_dir"))
        path = _last_result_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prev, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:                              # noqa: BLE001
        pass


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
# 关页面后隔多久重开还肯把上一批结果整份还原（默认 24 小时）。
_RESTORE_TTL_S = float(cfg.get("web.restore_ttl_s", 86400))
_RESTORED_DONE = False                             # 本次进程是否已捞过磁盘上的上一批


def _restore_last_result(now: float | None = None) -> dict:
    """进程重启后把磁盘上的上一批结果捞回内存，**每个进程只做一次**。

    为什么需要它：关掉页面 → 服务自停 → 内存里的结果随进程一起没了，
    而「已抽过哪些块」存在浏览器 localStorage 里，于是重开后按钮还在、
    跳过也照常，唯独右边的结果面板空了。

    为什么只做一次：结果里带逐字段的原文证据，几十块就有上百 KB~MB 量级。
    捞回来之后登记成一条普通的"已完成任务"，后续刷新走和第 2 步一样的
    TTL 判断，不会每次刷新都重新传一遍、重绘一遍。
    """
    global _RESTORED_DONE
    if _RESTORED_DONE:
        return {}
    _RESTORED_DONE = True
    disk = _read_last_result()
    # 没有结果、也没有失败记录 -> 没什么可恢复的
    if not (disk.get("result") or disk.get("failed_error")):
        return {}
    finished = float(disk.get("finished_at") or disk.get("failed_at") or 0.0)
    # 隔了太久（默认超过 24 小时）就不还原了：那时多半已经换了一批文献，
    # 把上周的结果摆出来反而误导。已抽到哪仍由浏览器 localStorage 兜住。
    now = time.time() if now is None else now
    if finished and (now - finished) > _RESTORE_TTL_S:
        return {}
    jid = disk.get("job_id") or "restored"
    if jid not in _JOBS:                           # 真有同名任务在跑就不覆盖
        _JOBS[jid] = {"events": [], "cv": threading.Condition(), "done": True,
                      "result": disk.get("result"), "error": None,
                      "started_at": disk.get("started_at", finished),
                      "finished_at": finished,
                      "last": {}, "done_ids": list(disk.get("done_ids") or []),
                      "req": dict(disk.get("req") or {})}
    return {"job_id": jid, "done": True,
            "started_at": disk.get("started_at", finished),
            "progress": {}, "done_ids": list(disk.get("done_ids") or []),
            "result": disk.get("result"), "result_expired": False,
            "result_saved_at": finished,
            "last_failed_at": disk.get("failed_at"),
            "last_failed_error": disk.get("failed_error"),
            "req": dict(disk.get("req") or {})}


def _running_job(now: float | None = None) -> dict:
    """找出一个「可接管」的抽取任务，供页面刷新后恢复现场。

    三种都算可接管：
      1. 正在跑的（done=False）——重连 SSE 接着看进度；
      2. 本进程刚起来、磁盘上还留着上一批——把结果整份还回去；
      3. 已跑完且**刚结束不久**（≤ web.adopt_ttl_s，默认 10 分钟）——把结果还回去，
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
    restored = _restore_last_result(now)
    if restored:
        return restored
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
                "result_saved_at": finished,
                "last_failed_at": j.get("failed_at"),
                "last_failed_error": j.get("error"),
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
    """目标文件不存在（或为空表）时从头写：先表头，再数据。"""
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
            "columns": len(headers), "written_cells": len(rows) * len(headers),
            "unexpected_count": 0}


def _count_duplicate_rows(path: str, sheet: str, headers: list[str],
                          rows: list[dict]) -> int | None:
    """新行里有多少行与表里已有的行完全相同（用于提示，不会据此删行）。

    只按"本批要写入的那几列"比对，且要求整行逐个值相同 —— 这样才是
    "重复点了一次导出"这种明确情况，不涉及任何模糊判定。
    读不出来就返回 None（宁可不报，也不报错数字）。
    """
    try:
        import openpyxl
        idx = [i for i, h in enumerate(headers) if (h or "").strip()]
        if not idx:
            return None
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet in wb.sheetnames else wb[wb.sheetnames[0]]
            seen: set[tuple] = set()
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n == 0:                                  # 跳过表头
                    continue
                if not any(v is not None and str(v).strip() != "" for v in row):
                    continue                                # 空行不算
                seen.add(tuple(row[i] if i < len(row) else None for i in idx))
        finally:
            wb.close()
        n_dup = 0
        for r in rows:
            if tuple(r.get(headers[i]) for i in idx) in seen:
                n_dup += 1
        return n_dup
    except Exception:                                       # noqa: BLE001
        return None


def _fill_coverage(headers: list[str], rows: list[dict]) -> dict:
    """这批数据在目标表各列上的填充率——"写进去了"不等于"抽到了"。

    只统计目标表里真正有值的列；分母是这批行数。返回逐列比例与整体比例，
    界面用它告诉用户「工艺列只有 5%」这类事实，而不是一句"已写入 N 行"。
    """
    cols = [h for h in headers if h and h.strip()]
    n = len(rows) or 1
    per: dict[str, float] = {}
    for h in cols:
        got = sum(1 for r in rows
                  if r.get(h) is not None and str(r.get(h)).strip() != "")
        per[h] = round(got / n, 4)
    overall = round(sum(per.values()) / len(per), 4) if per else 0.0
    return {"rows": len(rows), "overall": overall, "columns": per,
            "low": [h for h, v in per.items() if v < 0.5]}


def _map_rows_to_headers(rows: list[dict], specs: dict,
                         headers: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """把「模板列名」行改写为「目标表真实列名」行。

    为什么要这一步：记录里的键来自**模板**的列（如 Si），
    而 ExcelTarget.write 是按**目标表**的列名定位单元格的。两边写法只要有一点点
    不同（Si(wt.%) / Si 含量 / 硅 / 牌号），原来的实现就整行匹配不上——
    结果是 0 个单元格写成，却照样返回"成功"，界面显示"已写入 N 行"。

    匹配走 strict_spec_for_header（别名原样 + 元素带单位），不做含糊的子串兜底：
    宁可明确告诉用户"这一列对不上"，也不擅自把值塞进表里看起来像的那一列。

    返回 (改名后的行, ["模板列 -> 目标列", ...], 未能匹配而被跳过的模板列)。
    """
    from ..extract.schema import _norm, strict_spec_for_header

    norm_map = {}
    for h in headers:
        h = (h or "").strip()
        if h:
            norm_map.setdefault(_norm(h), h)

    key_by_col: dict[str, str] = {}
    for k, sp in specs.items():
        key_by_col.setdefault(sp.column, k)

    # 目标列名 -> 模板列（先靠标准字段，再靠列名/别名完全相同）
    col_to_header: dict[str, str] = {}
    for h in norm_map.values():
        sp = strict_spec_for_header(h)
        if sp is not None:
            for col, k in key_by_col.items():
                if k == sp.key:
                    col_to_header.setdefault(col, h)
    for k, sp in specs.items():
        for cand in (sp.column, sp.label, *(sp.aliases or [])):
            hit = norm_map.get(_norm(cand))
            if hit:
                col_to_header.setdefault(sp.column, hit)

    header_set = set(norm_map.values())
    mapped, mapping, skipped = [], [], set()
    for row in rows:
        out: dict = {}
        for col, v in row.items():
            target = col_to_header.get(col)
            if target is None and col in header_set:      # 逐字相同也认
                target = col
            if target is None:
                skipped.add(col)
                continue
            out[target] = v
            if target != col:
                mapping.append(f"{col} -> {target}")
        mapped.append(out)
    return mapped, sorted(set(mapping)), sorted(skipped)


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
        if not os.path.exists(target):
            # 文件不存在：按模板列名从头建（表头 + 数据）
            wres = _write_new_xlsx(target, headers, rows)
            return {"ok": True, "format": "xlsx", "write": wres,
                    "rows": len(rows), "columns": len(headers),
                    "headers": headers, "deduped": deduped,
                    "written_cells": wres["written_cells"], "column_mapping": [],
                    "skipped_columns": [],
                    "balance_added": balance_added,
                    "clean_report": report, "schema": sdef.to_dict()}

        tgt = excel_target()(target, cfg.load())
        target_headers = tgt.headers()
        if not any(h.strip() for h in target_headers):
            # 表存在但是空表：表头都没有，按模板列先建表头再写数据
            wres = _write_new_xlsx(target, headers, rows)
            return {"ok": True, "format": "xlsx", "write": wres,
                    "rows": len(rows), "columns": len(headers),
                    "headers": headers, "deduped": deduped,
                    "written_cells": wres["written_cells"], "column_mapping": [],
                    "skipped_columns": [],
                    "balance_added": balance_added,
                    "clean_report": report, "schema": sdef.to_dict()}

        # 关键一步：把「模板列名」换成「目标表真实列名」
        mapped_rows, mapping, skipped = _map_rows_to_headers(rows, specs, target_headers)
        if not any(mapped_rows):
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "目标表的列名与这一批数据的字段一列都对不上，已放弃写入"
                         "（没有改动你的表格）",
                "target": target,
                "target_headers": [h for h in target_headers if h.strip()][:30],
                "row_columns": sorted({c for r in rows for c in r})[:30],
                "hint": "把目标表首行改成模板里的列名（如 合金牌号 / Si / Mg），"
                        "或换一个本来就有这些表头的表。"})

        # 追加是"叠在最后一行下面"，不去重表里已有的内容。
        # 但重复点一次导出就会多写一批完全一样的行，所以这里**如实报告**
        # 有多少行与表里已有行完全相同（仍然写入，绝不擅自替用户删）。
        dup_of_existing = _count_duplicate_rows(target, tgt.sheet,
                                                target_headers, mapped_rows)

        # 填充率闸门：先算再写。"写进去了"不等于"抽到了"——工艺列只有 5% 时
        # 界面若只说"已写入 N 行"，用户会以为抽取成功，其实大半是空的。
        coverage = _fill_coverage(target_headers, mapped_rows)
        min_rate = float((cfg.load().get("writeback") or {}).get("min_fill_rate", 0) or 0)
        if min_rate > 0 and coverage["overall"] < min_rate:
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": f"这批数据的整体填充率只有 {coverage['overall']*100:.1f}%，"
                         f"低于设定下限 {min_rate*100:.0f}%，已放弃写入（没有改动你的表格）",
                "coverage": coverage, "target": target,
                "hint": "把 writeback.min_fill_rate 调低或设为 0 可强制写入；"
                        "填充率低通常是抽取模式只选了表格块、或该文献没报告这些参数。"})

        wres = tgt.write(mapped_rows, dry_run=False)
        written = int(wres.get("written_cells") or 0)
        return {"ok": written > 0, "format": "xlsx", "write": wres,
                "rows": len(mapped_rows), "columns": len(target_headers),
                "headers": [h for h in target_headers if h.strip()],
                "deduped": deduped,
                "written_cells": written,
                "column_mapping": mapping, "skipped_columns": skipped,
                "duplicate_of_existing": dup_of_existing,
                "coverage": coverage,
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
_NO_DIST_HTML = """<!doctype html><meta charset="utf-8">
<meta name="color-scheme" content="only light">
<title>前端未构建</title>
<body style="font:15px/1.7 'Microsoft YaHei',sans-serif;padding:40px;color:#000">
<h2>前端还没有构建</h2>
<p><code>rag/web/dist/</code> 不存在或缺少 <code>index.html</code>。</p>
<p>在项目根目录双击 <code>build_web.bat</code>（等价于
<code>python scripts/build_web.py</code>）构建一次即可。</p>
</body>"""


@app.get("/")
def index():
    """返回 Vite 构建产物。

    no-store：浏览器默认会对没有缓存头的 HTML 做"启发式缓存"，改过前端代码
    重新构建后，用户看到的还是旧页面（表现就是"修好了但界面没变"）。
    资产文件名带 hash，跟着 HTML 走即可，不需要额外处理。
    """
    built = os.path.join(_DIST, "index.html")
    if not os.path.isfile(built):
        return HTMLResponse(_NO_DIST_HTML, status_code=503)
    return FileResponse(built, headers=_NO_STORE)


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
