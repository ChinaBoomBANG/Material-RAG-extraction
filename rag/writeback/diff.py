"""工作簿快照与逐单元格 diff。

对应工作规范："任何 xlsx 修改前先备份，再逐 cell 对比 before/after，
确保零非预期变更。" 本模块只做读取与比较，不写文件。
"""

from __future__ import annotations

import os
from typing import Any

import openpyxl


def snapshot(path: str, sheet: str | None = None) -> dict[str, Any]:
    """把工作簿（或单个 sheet）读成 {sheet: {(row, col): value}}。

    只读 data_only=False，保留公式文本，避免因缓存缺失把公式读成 None。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    wb = openpyxl.load_workbook(path, data_only=False)
    sheets = [sheet] if sheet else wb.sheetnames
    snap: dict[str, Any] = {}
    for name in sheets:
        ws = wb[name]
        cells: dict[tuple[int, int], Any] = {}
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    cells[(c.row, c.column)] = c.value
        snap[name] = {"cells": cells,
                      "max_row": ws.max_row,
                      "max_col": ws.max_column}
    wb.close()
    return snap


def diff(before: dict, after: dict,
         allowed: dict[str, set[tuple[int, int]]] | None = None) -> dict:
    """比较两次快照。

    allowed: {"sheet名": {(row, col), ...}} —— 本次写入预期触及的单元格。
    不在该集合内的差异会被计入 unexpected，这是判断"有没有误伤原表"的依据。
    """
    changed: list[dict] = []
    unexpected: list[dict] = []
    sheets = sorted(set(before) | set(after))

    for name in sheets:
        b = before.get(name, {}).get("cells", {})
        a = after.get(name, {}).get("cells", {})
        allow = (allowed or {}).get(name, set())
        for key in sorted(set(b) | set(a), key=lambda k: (k[0], k[1])):
            bv, av = b.get(key), a.get(key)
            if bv == av:
                continue
            item = {"sheet": name, "cell": f"{_col(key[1])}{key[0]}",
                    "row": key[0], "col": key[1],
                    "before": bv, "after": av}
            if allow and key not in allow:
                unexpected.append(item)
            changed.append(item)

    return {
        "added": sum(1 for c in changed if c["before"] is None),
        "modified": sum(1 for c in changed if c["before"] is not None and c["after"] is not None),
        "removed": sum(1 for c in changed if c["after"] is None),
        "total_changed": len(changed),
        "changed": changed,
        "unexpected_count": len(unexpected),
        "unexpected": unexpected,
    }


def _col(idx: int) -> str:
    s = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def format_report(report: dict, limit: int = 40) -> str:
    lines = [
        f"变更单元格总数：{report['total_changed']}"
        f"（新增 {report['added']} / 修改 {report['modified']} / 清空 {report['removed']}）",
        f"非预期变更：{report['unexpected_count']}",
    ]
    if report["unexpected_count"]:
        lines.append("!! 存在非预期变更，请检查：")
        for c in report["unexpected"][:limit]:
            lines.append(f"   {c['sheet']}!{c['cell']}: {c['before']!r} -> {c['after']!r}")
    lines.append("明细：")
    for c in report["changed"][:limit]:
        lines.append(f"   {c['sheet']}!{c['cell']}: {c['before']!r} -> {c['after']!r}")
    if len(report["changed"]) > limit:
        lines.append(f"   ...（其余 {len(report['changed']) - limit} 条见 JSON 报告）")
    return "\n".join(lines)
