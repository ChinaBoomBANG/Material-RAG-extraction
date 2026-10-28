"""高层管线：把解析、索引、检索、生成、抽取、写回串成可用流程。

典型用法见 README。核心原则：
  - 检索与生成默认全在本机完成（LLM 走 LM Studio），文献不出机器；
  - 抽取结果必须带原文证据，写回 Excel 前先出预览与 diff。
"""

from __future__ import annotations

import glob
import os
import re

from . import config as cfg
from .extract.extractor import Extractor
from .extract.schema import FieldSpec, build_schema_from_headers
from .index.embedder import Embedder
from .index.retriever import Retriever, format_context
from .index.store import Store
from .llm.client import LLMClient
from .models import Answer, Chunk, MaterialRecord
from .schemas import builtin_schema, load_any

# 注意：以下两个模块**故意不在顶层导入**
#   .ingest        -> 拉起 pymupdf（约 63 ms）
#   .writeback.excel -> 拉起 pandas + openpyxl（约 485 ms）
# 它们只在"解析 PDF"和"写 Excel"时才用得上，顶层导入会让每次启动白付这 0.5 秒。
# 见 ingest() / extract() / writeback() 内的局部导入。

# auto 模式的判定：必须是「数字 + 单位」同时出现。
# 只用关键词会误伤——材料论文几乎每页都有"合金/强度/热处理"这类词，
# 实测会把 498 个文本块中的 386 个判为"含数据"（约 7 小时的活）。
# 收紧成「数值紧跟单位」后降到 131 个，且真正带数据的段落一个没漏。
DATA_VALUE = re.compile(
    r"\d\s*(?:wt\.?\s*%|at\.?\s*%|MPa|GPa|HB|HV|%IACS|g/cm\^?[23]?|W/\(?m|"
    r"%|℃|°C|℉|K\b|min\b|\bh\b|\bs\b|rpm|m/s|mm|µm|μm|nm|GPa|kJ)",
    re.I,
)

SYSTEM_QA = """你是材料科学文献助手。你只能依据给定的文献片段回答问题。

规则：
1. 回答必须基于片段内容。片段中没有的信息，直接说明"给出的片段中没有相关信息"，不得用常识补全。
2. 每个结论后面用 [序号] 标注来源，序号对应片段编号。
3. 涉及数值时，原样引用原文的数值与单位，不做换算、不做四舍五入。
4. 如果不同片段数据冲突，把冲突如实列出。
5. 用中文回答，结构清晰，不要复述问题。"""

USER_QA = """【文献片段】
{context}

【问题】
{question}

请依据上述片段回答。"""


class RagPipeline:
    def __init__(self, conf: dict | None = None, lazy: bool = True):
        self.conf = conf or cfg.load()
        self.raw_dir = cfg.path("project.raw_dir")
        self.index_dir = cfg.path("project.index_dir")
        self.output_dir = cfg.path("project.output_dir")
        for d in (self.raw_dir, self.index_dir, self.output_dir):
            cfg.ensure_dir(d)

        self.store = Store(self.index_dir).load()
        self.embedder = Embedder(self.conf)
        self.llm = LLMClient(self.conf)
        self.retriever = Retriever(self.store, self.embedder, self.conf)
        self.extractor = Extractor(self.llm, self.conf)
        self._embedded = False
        if not lazy:
            self.ensure_models()

    # ---------------------------------------------------------- 模型

    def ensure_models(self) -> None:
        if not self._embedded and self.embedder.backend != "none":
            self.embedder.load()
            self._embedded = True

    # ---------------------------------------------------------- 索引

    def ingest(self, pdf_glob: str | None = None, force: bool = False) -> dict:
        from .ingest import parse_pdf      # 延迟导入：拉起 pymupdf
        self.ensure_models()
        pattern = pdf_glob or os.path.join(self.raw_dir, "*.pdf")
        files = sorted(glob.glob(pattern))
        result = {"files": 0, "skipped": 0, "chunks": 0, "tables": 0, "details": []}

        for fp in files:
            doc_id = os.path.splitext(os.path.basename(fp))[0]
            if self.store.has_doc(doc_id) and not force:
                result["skipped"] += 1
                continue
            if force:
                self.store.remove_doc(doc_id)

            chunks, meta = parse_pdf(fp, self.conf)
            if not chunks:
                result["details"].append({"file": os.path.basename(fp),
                                          "status": "无可解析内容"})
                continue

            vectors = None
            if self.embedder.backend != "none":
                vectors = self.embedder.encode_documents([c.text for c in chunks])
            self.store.add(chunks, vectors)
            self.store.set_meta(doc_id, meta)

            n_tab = sum(1 for c in chunks if c.kind == "table")
            result["files"] += 1
            result["chunks"] += len(chunks)
            result["tables"] += n_tab
            result["details"].append({
                "file": os.path.basename(fp), "status": "已索引",
                "pages": meta["page_count"], "chunks": len(chunks), "tables": n_tab})

        self.store.save()
        result["index_stats"] = self.store.stats()
        return result

    # ---------------------------------------------------------- 问答

    def _translate_query(self, question: str) -> str:
        """把中文问题译成英文，用于跨语言检索。

        本机 embedding 是 bge-small-zh（中文专用），文献正文却是英文：
        中文提问在英文库上的相似度普遍很低（实测 0.016，基本等于噪声），
        因此这里先补一次英译，再与原始问题一起检索、按 chunk 取较优分数。
        只做一次短输出，实测增加约 5–10 秒。
        """
        try:
            txt = self.llm.chat(
                f"把下面的问题翻译成英文，只输出英文译文，不要解释、不要编号：\n{question}",
                system="你是翻译器，只输出英文译文。", temperature=0.0, max_tokens=160)
            txt = (txt or "").strip().splitlines()[0].strip()
            return txt if txt and len(txt) < 400 else ""
        except Exception:
            return ""

    def ask(self, question: str, top_k: int | None = None,
            kinds: list[str] | None = None, doc_ids: list[str] | None = None) -> Answer:
        self.ensure_models()
        hits = self.retriever.search(question, top_k=top_k, kinds=kinds, doc_ids=doc_ids)

        # 中文提问 + 英文库：补一路英文检索，合并后按 chunk 保留较高分。
        if re.search(r"[\u4e00-\u9fff]", question or ""):
            q_en = self._translate_query(question)
            if q_en:
                extra = self.retriever.search(q_en, top_k=top_k, kinds=kinds, doc_ids=doc_ids)
                merged: dict[str, dict] = {}
                for h in hits + extra:
                    cid = h["chunk"].chunk_id
                    if cid not in merged or h["score"] > merged[cid]["score"]:
                        merged[cid] = h
                hits = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
                hits = hits[:int(top_k or self.conf["retrieval"]["top_k_final"])]

        if not hits:
            return Answer(question=question, answer="索引中没有可用内容，请先执行 ingest。")
        context = format_context(hits)
        text = self.llm.chat(USER_QA.format(context=context, question=question),
                             system=SYSTEM_QA)
        citations = []
        for i, h in enumerate(hits, 1):
            c: Chunk = h["chunk"]
            citations.append({"no": i, "doc": c.doc_name, "page": c.page,
                              "kind": c.kind, "chunk_id": c.chunk_id,
                              "score": round(h["score"], 4),
                              "excerpt": c.text[:200]})
        return Answer(question=question, answer=text, citations=citations,
                      raw_context=[{"chunk_id": h["chunk"].chunk_id,
                                    "text": h["chunk"].text} for h in hits])

    # ---------------------------------------------------------- 抽取

    def pick_chunks(self, mode: str = "auto", doc_ids: list[str] | None = None,
                    max_chunks: int = 0, skip: int = 0,
                    exclude_ids: list[str] | None = None) -> list[Chunk]:
        """挑选送抽取的块。

        mode: table = 只要表格；auto = 表格 + 出现「数值+单位」的段落；all = 全部段落
        max_chunks: >0 时只取前 N 块（表格优先、按页码顺序），0 = 不限
        exclude_ids: 已抽过的块 id 列表。界面与 CLI 的「继续抽取」都走这条路：
                     它与块列表顺序无关，切换文献范围或抽取模式后仍然准确。
        skip: >0 时先跳过排序后的前 N 块。**仅适用于块列表不变**的场景
              （脚本 / 自测里按数量取样），界面续抽不再使用它。
        """
        chunks = self.store.chunks
        if doc_ids:
            chunks = [c for c in chunks if c.doc_id in doc_ids]
        if mode == "table":
            picked = [c for c in chunks if c.kind == "table"]
        elif mode == "all":
            picked = list(chunks)
        else:
            picked = [c for c in chunks
                      if c.kind == "table" or DATA_VALUE.search(c.text)]
        # 表格优先，其次按页码顺序
        picked.sort(key=lambda c: (0 if c.kind == "table" else 1, c.doc_id, c.page))
        if exclude_ids:
            done = set(exclude_ids)
            picked = [c for c in picked if c.chunk_id not in done]
        if skip > 0:
            picked = picked[skip:]
        if max_chunks > 0:
            picked = picked[:max_chunks]
        return picked

    def extract(self, excel_path: str | None = None,
                doc_ids: list[str] | None = None,
                mode: str = "auto",
                max_chunks: int = 0,
                exclude_ids: list[str] | None = None,
                headers: list[str] | None = None,
                schema=None,
                workers: int | None = None,
                on_event=None,
                should_stop=None) -> dict:
        """抽取结构化记录。

        Schema 来源优先级（用户 2026-10-17 定的规则）：
          1. 指定了目标 Excel -> 读表头覆盖 Schema（表头即契约）
          2. ``schema`` 参数（模板名 / 文件路径 / YAML 文本 / dict / SchemaDef）
          3. 都没有 -> 内置模板（原 94 字段）
        """
        self.ensure_models()
        if headers is None and excel_path:
            from .writeback.excel import ExcelTarget as _X       # 延迟导入：拉起 pandas
            headers = _X(excel_path, self.conf).headers()

        schema_def = None
        if headers:
            built = build_schema_from_headers(headers)
            specs = built["specs"]
            matched_info = {"mode": "excel_headers",
                            "matched": built["matched"], "custom": built["custom"]}
        elif schema is not None:
            schema_def, errs, warns = load_any(schema)
            if schema_def is None:
                raise ValueError("；".join(e["msg"] for e in errs) or "Schema 无法解析")
            specs = schema_def.to_specs()
            matched_info = {"mode": "schema", "matched": [], "custom": [],
                            "schema": schema_def.to_dict(),
                            "warnings": [w["msg"] for w in warns]}
        else:
            schema_def = builtin_schema()
            specs = schema_def.to_specs()
            matched_info = {"mode": "builtin", "matched": [], "custom": [],
                            "schema": schema_def.to_dict(),
                            "note": "未指定 Schema，使用内置模板"}

        chunks = self.pick_chunks(mode=mode, doc_ids=doc_ids,
                                  max_chunks=max_chunks, exclude_ids=exclude_ids)
        if not chunks:
            return {"records": [], "specs": specs, "schema": matched_info,
                    "schema_def": schema_def, "picked_chunks": 0,
                    "note": "没有可抽取的块，请先 ingest，或调整 mode/max_chunks"}

        if workers is None:
            workers = int(cfg.get("extract.workers", 1) or 1)
        # 并发硬上限：界面传多少都夹到配置范围内，避免一次开太多把云端配额打爆。
        cap = int(cfg.get("extract.max_workers", 8) or 8)
        workers = max(1, min(int(workers), cap))

        # 表格块的参考上下文：同页正文（只读 store，不改 chunk、不重建索引，
        # 因此索引与召回完全不受影响）。
        ctx_conf = cfg.get("extract.table_context", {}) or {}
        context_of = None
        if bool(ctx_conf.get("enabled", True)):
            max_chars = int(ctx_conf.get("max_chars", 400) or 400)
            page_text: dict[tuple[str, int], list[str]] = {}
            for c in self.store.chunks:
                if c.kind == "text":
                    page_text.setdefault((c.doc_id, c.page), []).append(c.text)

            def context_of(chunk):          # noqa: F811  闭包，按块查同页正文
                if chunk.kind != "table":
                    return ""
                txt = "\n".join(page_text.get((chunk.doc_id, chunk.page), []))
                return txt[:max_chars]

        records = self.extractor.extract(chunks, specs, workers=max(1, int(workers)),
                                         on_event=on_event, should_stop=should_stop,
                                         context_of=context_of)
        # 文档级工艺补全：工艺参数在论文里常常只说一次（实验段落），
        # 块级抽取看不到跨块信息，工艺列就会几乎全空。这里每篇补一次。
        card_stats = None
        card_conf = self.conf.get("extract", {}).get("process_card", {}) or {}
        if card_conf.get("enabled", True) and records:
            from .extract.process_card import apply_process_cards
            try:
                card_stats = apply_process_cards(self.llm, self.store, records,
                                                 specs, self.conf, on_event=on_event)
            except Exception as e:          # 卡片只是增强，失败不能把整批抽取带崩
                card_stats = {"error": f"{type(e).__name__}: {e}"}
        return {"records": records, "specs": specs, "schema": matched_info,
                "schema_def": schema_def, "picked_chunks": len(chunks),
                "records_count": len(records), "workers": max(1, int(workers)),
                "process_card": card_stats,
                # 用抽取器记录的真实状态：不能在这里再调 should_stop()——
                # 用户点过停止后 cancel 标记会一直为 True，会把没被中断的任务误报成「提前停止」
                "stopped_early": bool(self.extractor.stopped_early),
                "dropped": self.extractor.dropped}

    # ---------------------------------------------------------- 写回

    def writeback(self, records: list[MaterialRecord], specs: dict[str, FieldSpec],
                  excel_path: str, dry_run: bool | None = None,
                  trusted_only: bool = True) -> dict:
        from .writeback.excel import ExcelTarget, save_report   # 延迟导入：拉起 pandas
        target = ExcelTarget(excel_path, self.conf)
        rows, evidence = ExcelTarget.to_rows(records, specs, trusted_only=trusted_only)
        preview = target.write_preview(rows, evidence)
        result = {"preview": preview,
                  "rows": len(rows),
                  "cells": sum(len(r) for r in rows),
                  "unverified_evidence_rows": sum(1 for e in evidence if e["已核验"] == "否"),
                  "evidence_rows": len(evidence)}
        w = target.write(rows, dry_run=dry_run)
        result["write"] = w
        result["report_json"] = save_report(result, self.output_dir)
        return result

    # ---------------------------------------------------------- 状态

    def status(self) -> dict:
        s = self.store.stats()
        return {
            "index": s,
            "index_dir": self.index_dir,
            "raw_dir": self.raw_dir,
            "embedding_backend": self.conf["embedding"]["backend"],
            "llm": self.llm.health(),
            # 界面上的「服务生命周期」文案直接读这里，避免把秒数写死在模板里
            # （曾经写死 "8 秒"，配置改成 15 秒后界面仍在骗人）
            "web": {
                "shutdown_on_page_close": bool(
                    cfg.get("web.shutdown_on_page_close", True)),
                "shutdown_grace_s": float(cfg.get("web.shutdown_grace_s", 15)),
                "idle_timeout_s": float(cfg.get("web.idle_timeout_s", 180)),
            },
        }
