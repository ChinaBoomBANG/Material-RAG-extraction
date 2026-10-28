"""Excel 写回：表头驱动 schema、预览、备份、写入、逐格 diff。

工作流（对应"先填后验 diff"规范）：
    1. 读取目标表的表头 -> 生成抽取 schema
    2. 抽取得到记录 -> 生成预览行（不落盘）
    3. 用户确认后：copy2 备份 -> 写入 -> 再快照 -> 逐 cell diff
    4. 报告必须给出"非预期变更数"，正常应为 0
"""

from __future__ import annotations

import json
import os
import shutil
import time

import openpyxl
import pandas as pd

from ..extract.schema import (FieldSpec, build_schema_from_headers,
                              strict_spec_for_header as header_spec)
from ..models import MaterialRecord
from . import diff as diffmod


class ExcelTarget:
    """目标工作簿的读写门面。"""

    def __init__(self, path: str, conf: dict, sheet: str | None = None):
        self.path = os.path.abspath(path)
        self.conf = conf
        self.sheet_name = sheet
        self.output_dir = conf["project"]["output_dir"]
        if not os.path.isabs(self.output_dir):
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.output_dir = os.path.join(root, self.output_dir)

    # ---------------------------------------------------------- 表头 / schema

    @property
    def sheet(self) -> str:
        if self.sheet_name:
            return self.sheet_name
        wb = openpyxl.load_workbook(self.path, read_only=True)
        name = wb.sheetnames[0]
        wb.close()
        return name

    def headers(self, header_row: int = 1) -> list[str]:
        wb = openpyxl.load_workbook(self.path, read_only=True)
        ws = wb[self.sheet]
        values = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True), ())
        wb.close()
        return [str(v).strip() if v is not None else "" for v in values]

    def schema(self, header_row: int = 1) -> dict:
        return build_schema_from_headers(self.headers(header_row))

    # ---------------------------------------------------------- 记录 -> 行

    @staticmethod
    def to_rows(records: list[MaterialRecord], specs: dict[str, FieldSpec],
                trusted_only: bool = True) -> tuple[list[dict], list[dict]]:
        """把记录转成 {列名: 值} 列表，并同时产出证据明细。

        列名一律取 spec.column（即目标 Excel 里的**原始表头**），
        不能取字段中文名——否则表头写"屈服强度"而字段名是"屈服强度 Rp0.2"就会对不上。

        返回 (rows, evidence)；evidence 每项含 record_id / 列 / 值 / 单位 /
        页码 / 文献 / 引文 / 是否已核验。
        """
        rows: list[dict] = []
        evidence: list[dict] = []
        alloy_spec = specs.get("alloy")
        groups = ("composition", "process", "microstructure", "properties")

        # 目标表里有「文献 / 来源」列时，把来源写进去：没有它，出了错也追不回是哪篇
        doc_col = ""
        for key, sp in specs.items():
            lab = f"{sp.label or ''} {sp.column or ''}".lower()
            if any(h in lab for h in ("文献", "来源", "source", "论文", "doi")):
                doc_col = sp.column or sp.label
                break

        for rec in records:
            row: dict = {}
            if doc_col and rec.source_doc:
                row[doc_col] = rec.source_doc
            if rec.alloy and alloy_spec is not None:
                row[alloy_spec.column] = rec.alloy
                evidence.append({
                    "record_id": rec.record_id, "来源文献": rec.source_doc,
                    "列": alloy_spec.column, "值": rec.alloy, "单位": "",
                    "页码": rec.source_page, "引文": "", "已核验": "—",
                    "备注": "; ".join(rec.warnings)})

            for group, name, fv in rec.all_fields():
                key = f"{group}.{name}" if group in groups else name
                spec = specs.get(key)
                if spec is None:
                    continue
                col = spec.column
                verified = bool(fv.evidence and fv.evidence.verified)
                if trusted_only and not verified:
                    # 未通过原文核验的字段不进数据表，但证据仍保留供人工复核
                    evidence.append(_ev(rec, col, fv, verified))
                    continue
                value = _cell_value(fv)
                if value is None:
                    continue
                row[col] = value
                evidence.append(_ev(rec, col, fv, verified))
            rows.append(row)
        return rows, evidence

    # ---------------------------------------------------------- 预览

    def preview(self, rows: list[dict], headers: list[str] | None = None) -> pd.DataFrame:
        cols = headers or self.headers()
        return pd.DataFrame([{h: r.get(h) for h in cols} for r in rows], columns=cols)

    def write_preview(self, rows: list[dict], evidence: list[dict],
                      tag: str = "preview") -> dict:
        """把预览结果与证据写到一个独立目录，不动目标表。"""
        out = os.path.join(self.output_dir, f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(out, exist_ok=True)
        df = self.preview(rows)
        p_csv = os.path.join(out, "preview_rows.csv")
        df.to_csv(p_csv, index=False, encoding="utf-8-sig")
        p_ev = os.path.join(out, "evidence.csv")
        pd.DataFrame(evidence).to_csv(p_ev, index=False, encoding="utf-8-sig")
        return {"dir": out, "rows_csv": p_csv, "evidence_csv": p_ev, "row_count": len(rows)}

    # ---------------------------------------------------------- 写入

    def write(self, rows: list[dict], dry_run: bool | None = None,
              header_row: int = 1, key_header: str = "") -> dict:
        """写入目标表：默认 **按牌号回填**（upsert），不是无脑追加。

        规则（对应"数据罗列在已有数据上"这个问题）：
          1. 用牌号列找到已有行；只往**空格**里填，非空单元格一律不动；
          2. 已有行在本次要写的列上有**不同**的值 -> 认为不是同一批数据，
             另起新行（绝不覆盖）；
          3. 牌号为空、或表里没有该牌号 -> 另起新行；
          4. 同一批内部先按牌号合并，避免自己和自己打架。
        """
        if dry_run is None:
            dry_run = bool(self.conf["writeback"].get("dry_run_default", True))
        if not rows:
            return {"ok": False, "reason": "没有可写入的行"}
        if not os.path.exists(self.path):
            return {"ok": False, "reason": f"目标文件不存在：{self.path}"}

        headers = self.headers(header_row)
        col_index = {h: i for i, h in enumerate(headers, 1)}
        keys = ([key_header] if key_header else self._key_headers(headers))
        keys = [k for k in keys if k in col_index]
        upsert = bool(self.conf["writeback"].get("upsert", True)) and bool(keys)
        key = " + ".join(keys)
        rows = _coalesce_multi(rows, keys if upsert else [])

        if dry_run:
            return {"ok": True, "dry_run": True,
                    "target": self.path, "sheet": self.sheet,
                    "would_append_rows": len(rows), "key_header": key,
                    "note": "dry_run=True，未修改任何文件。确认后请以 dry_run=False 再执行。"}

        before = diffmod.snapshot(self.path)
        backup_path = ""
        if self.conf["writeback"].get("backup", True):
            bdir = os.path.join(self.output_dir, "backups")
            os.makedirs(bdir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(
                bdir, f"{os.path.splitext(os.path.basename(self.path))[0]}_{stamp}.xlsx")
            shutil.copy2(self.path, backup_path)

        wb = openpyxl.load_workbook(self.path)
        ws = wb[self.sheet]
        allowed: set[tuple[int, int]] = set()
        index = self._index_rows(ws, col_index, keys, header_row) if upsert else {}
        next_free = max(ws.max_row + 1, header_row + 1)
        filled = appended = conflicts = ambiguous = 0
        used: dict[str, set[int]] = {}

        for row in rows:
            cells = {col_index[h]: v for h, v in row.items()
                     if h in col_index and v is not None}
            kv = _key_of_multi(row, keys) if upsert else ""
            target = None
            if kv:
                cands = self._compatible(ws, index.get(kv, []), cells,
                                         used.setdefault(kv, set()))
                if len(cands) == 1:
                    target = cands[0]
                elif len(cands) > 1:
                    # 同一牌号在表里有多行（来自不同文献/不同状态），而表里没有
                    # 足以区分的列 -> 不猜，另起一行，并把这件事报出来。
                    ambiguous += 1
            if target is None:
                target = next_free
                next_free += 1
                appended += 1
                if kv:
                    index.setdefault(kv, []).append(target)
                    used.setdefault(kv, set()).add(target)
            else:
                filled += 1
            for j, v in cells.items():
                cur = ws.cell(row=target, column=j).value
                if cur is not None and str(cur).strip() != "":
                    if not _same(cur, v):
                        conflicts += 1
                    continue
                ws.cell(row=target, column=j, value=v)
                allowed.add((target, j))
        wb.save(self.path)
        wb.close()

        written = len(allowed)
        after = diffmod.snapshot(self.path)
        report = diffmod.diff(before, after, {self.sheet: allowed})
        report.update({"ok": report["unexpected_count"] == 0,
                       "dry_run": False, "target": self.path,
                       "sheet": self.sheet, "written_cells": written,
                       "backup": backup_path,
                       "key_header": key, "upsert": upsert,
                       "appended_rows": appended,
                       "updated_rows": filled,
                       "conflict_cells": conflicts,
                       "ambiguous_keys": ambiguous,
                       "rows_in": len(rows)})
        return report

    # ---------------------------------------------------------- upsert 辅助

    def _key_headers(self, headers: list[str]) -> list[str]:
        """主键列：牌号列 +（表里有的话）文献/来源列。

        单靠牌号不够：同一个 6061 在表里可能有十几行，来自不同文献。
        只用牌号当键，会把 A 文献的工艺填进 B 文献那一行——这是不可接受的数据污染。
        所以表里只要有文献列，就一起当键；没有的话，遇到同牌号多行就另起一行。
        """
        conf_keys = self.conf["writeback"].get("key_columns") or []
        if conf_keys:
            return [str(k) for k in conf_keys if str(k).strip()]
        out: list[str] = []
        for h in headers:
            sp_ = header_spec(h)
            if sp_ is not None and sp_.key == "alloy":
                out.append(h)
                break
        doc_hints = ("文献", "来源", "source", "文献名", "论文", "doi")
        for h in headers:
            if h and h.strip() and any(k in h.lower() for k in doc_hints):
                out.append(h)
                break
        return out or (headers[:1] if headers else [])

    @staticmethod
    def _index_rows(ws, col_index: dict[str, int], keys: list[str],
                    header_row: int) -> dict[str, list[int]]:
        idx: dict[str, list[int]] = {}
        js = [col_index[k] for k in keys if k in col_index]
        if not js:
            return idx
        for r in range(header_row + 1, ws.max_row + 1):
            kv = "|".join(_key_of(ws.cell(row=r, column=j).value) for j in js)
            if kv.strip("|"):
                idx.setdefault(kv, []).append(r)
        return idx

    @staticmethod
    def _compatible(ws, candidates: list[int], cells: dict[int, object],
                    banned: set[int]) -> list[int]:
        """能在这些列上回填且**不冲突**的候选行（冲突=已有不同的值）。"""
        out: list[int] = []
        for r in candidates:
            if r in banned:
                continue
            conflict = False
            fillable = 0
            for j, v in cells.items():
                cur = ws.cell(row=r, column=j).value
                if cur is None or str(cur).strip() == "":
                    fillable += 1
                elif not _same(cur, v):
                    conflict = True
                    break
            if not conflict and fillable > 0:
                out.append(r)
        return out


# ---------------------------------------------------------------- 辅助

def _cell_value(fv) -> object:
    """决定写入单元格的表示。

    区间值写回原文表达（如 "0.20~0.50"），不做中值折算——中值不是原文数据。
    """
    p = fv.parsed or {}
    kind = p.get("kind")
    if kind == "single" and p.get("value") is not None:
        return p["value"]
    if kind in ("range", "upper_bound", "lower_bound") and p.get("raw"):
        return str(p["raw"]).strip()
    if fv.value is None:
        return None
    return fv.value


def _ev(rec: MaterialRecord, col: str, fv, verified: bool) -> dict:
    ev = fv.evidence
    return {
        "record_id": rec.record_id,
        "来源文献": rec.source_doc,
        "列": col,
        "值": _cell_value(fv),
        "单位": fv.unit,
        "页码": ev.page if ev else "",
        "引文": ev.quote if ev else "",
        "已核验": "是" if verified else "否",
        "备注": "; ".join(rec.warnings),
    }


def save_report(report: dict, output_dir: str, tag: str = "diff") -> str:
    os.makedirs(output_dir, exist_ok=True)
    p = os.path.join(output_dir, f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return p


# ---------------------------------------------------------------- upsert 用的纯函数

def _key_of(v) -> str:
    """牌号归一：去空白、统一大小写。'6005 A' 与 '6005a' 视为同一合金。"""
    if v is None:
        return ""
    s = "".join(str(v).split())
    return s.casefold()


def _same(a, b) -> bool:
    """两个单元格的值是否"同一个数"：先试数值，再退字符串（去空白、统一大小写）。"""
    try:
        fa, fb = float(str(a).replace(" ", "")), float(str(b).replace(" ", ""))
        return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))
    except (TypeError, ValueError):
        pass
    return str(a).strip() == str(b).strip()


def _key_of_multi(row: dict, keys: list[str]) -> str:
    """复合键：各键列归一后拼起来。任一键为空则返回空串（不参与回填）。"""
    vals = [_key_of(row.get(k)) for k in keys]
    if not all(vals):
        return ""
    return "|".join(vals)


def _coalesce_multi(rows: list[dict], keys: list[str]) -> list[dict]:
    """同一批内部先按键合并，避免"成分一行、工艺一行"各占一行。

    冲突（同列不同值）不硬合——那是两种状态的数据，必须各自保留一行。
    """
    if not keys:
        return rows
    order: list[str] = []
    buckets: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        kv = _key_of_multi(row, keys)
        if not kv:
            out.append(row)
            continue
        cur = buckets.get(kv)
        if cur is None:
            buckets[kv] = dict(row)
            order.append(kv)
            continue
        conflict = any(h in cur and cur[h] is not None and v is not None
                       and not _same(cur[h], v)
                       for h, v in row.items())
        if conflict:
            out.append(row)
            continue
        for h, v in row.items():
            if v is not None and cur.get(h) is None:
                cur[h] = v
    return [buckets[k] for k in order] + out
