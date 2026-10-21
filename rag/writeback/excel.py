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

from ..extract.schema import FieldSpec, build_schema_from_headers
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

        for rec in records:
            row: dict = {}
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
              header_row: int = 1) -> dict:
        if dry_run is None:
            dry_run = bool(self.conf["writeback"].get("dry_run_default", True))
        if not rows:
            return {"ok": False, "reason": "没有可写入的行"}
        if not os.path.exists(self.path):
            return {"ok": False, "reason": f"目标文件不存在：{self.path}"}

        headers = self.headers(header_row)
        if dry_run:
            return {"ok": True, "dry_run": True,
                    "target": self.path, "sheet": self.sheet,
                    "would_append_rows": len(rows),
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
        start = max(ws.max_row + 1, header_row + 1)
        allowed: set[tuple[int, int]] = set()
        col_index = {h: i for i, h in enumerate(headers, 1)}

        written = 0
        for i, row in enumerate(rows):
            r = start + i
            for h, v in row.items():
                j = col_index.get(h)
                if j is None or v is None:
                    continue
                ws.cell(row=r, column=j, value=v)
                allowed.add((r, j))
                written += 1
        wb.save(self.path)
        wb.close()

        after = diffmod.snapshot(self.path)
        report = diffmod.diff(before, after, {self.sheet: allowed})
        report.update({"ok": report["unexpected_count"] == 0,
                       "dry_run": False, "target": self.path,
                       "sheet": self.sheet, "written_cells": written,
                       "backup": backup_path,
                       "appended_rows": len(rows)})
        return report


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
