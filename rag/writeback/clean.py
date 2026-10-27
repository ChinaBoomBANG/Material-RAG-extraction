"""导出前的基础清洗：只做确定性、可审计的字符级修正。

用户要的是「去空格、修错乱」，不是改数据语义，所以这里刻意**只碰字符层**：
  - trim             去首尾空白，内部连续空白压成一个空格
  - unicode          全角转半角（NFKC）+ 统一减号/波浪号
  - space_in_number  修被空格劈开的数字："1. 59" → "1.59"、"0 . 62" → "0.62"
  - strip_unit       值末尾重复了列单位时去掉（单位另存一列），如 "0.62 wt.%"

每一处改动都写进 report，前端能显示「修正 N 处」并逐条展开，便于复核。
**不做单位换算、不取中值、不改写数值** —— 与抽取阶段的原则一致。
"""

from __future__ import annotations

import re
import unicodedata

# 只对「长得像数值/带单位」的串修内部空格，避免把 "Fig. 5" 也粘成 "Fig.5"
_NUMISH = re.compile(r"^[\s±+\-]*(?:[<>≤≥~～]|约|approx\.?)?\s*[\d.,\s×xX^\-–—~±]+\s*[a-zA-Z%‰°³²/·]*$")


def _fix_number_spaces(s: str) -> str:
    if not _NUMISH.match(s):
        return s
    t = re.sub(r"(?<=\d)[ \t]+(?=[.,]\s*\d)", "", s)     # "0 . 62" -> "0. 62"
    t = re.sub(r"(?<=[.,])[ \t]+(?=\d)", "", t)          # "0. 62" -> "0.62"
    return t


def _norm_unicode(s: str) -> str:
    t = unicodedata.normalize("NFKC", s)                 # 全角数字/字母/％ -> 半角
    for a, b in (("−", "-"), ("–", "–"), ("—", "—"), ("～", "~"), ("≈", "~")):
        t = t.replace(a, b)
    return t


def _strip_unit(s: str, unit: str) -> str:
    """去掉值末尾与列单位重复的后缀："0.62 wt.%"（列单位 wt.%）-> "0.62"。"""
    if not unit:
        return s
    u = unit.strip()
    if not u:
        return s
    # 允许 "0.62wt.%" / "0.62 wt.%" / "0.62 wt%" 这几种写法
    tail = re.search(r"[\s]*([^\d\s]+)\s*$", s)
    if tail:
        got = tail.group(1).replace(" ", "")
        if got.lower() == u.lower().replace(" ", "") or got.lower() == u.lower().replace("%", ""):
            return s[: tail.start()].rstrip()
    return s


def clean_value(value, rules: list[str], unit: str = "") -> tuple[object, list[str]]:
    """返回 (清洗后的值, 命中的规则名列表)。非字符串原样返回。"""
    if not isinstance(value, str) or not value.strip():
        return value, []
    original = value
    used: list[str] = []
    v = value
    if "unicode" in rules:
        nv = _norm_unicode(v)
        if nv != v:
            v = nv
            used.append("unicode")
    if "trim" in rules:
        nv = " ".join(v.split())
        if nv != v:
            v = nv
            used.append("trim")
    if "space_in_number" in rules:
        nv = _fix_number_spaces(v)
        if nv != v:
            v = nv
            used.append("space_in_number")
    if "strip_unit" in rules:
        nv = _strip_unit(v, unit)
        if nv != v:
            v = nv
            used.append("strip_unit")
    return (v if v != original else original), used


def clean_rows(rows: list[dict], rules: list[str],
               unit_map: dict[str, str] | None = None,
               sample_limit: int = 20) -> tuple[list[dict], dict]:
    """对整批行做清洗，返回 (新行, 报告)。"""
    unit_map = unit_map or {}
    out: list[dict] = []
    scanned = changed = 0
    by_rule: dict[str, int] = {}
    samples: list[dict] = []

    for i, row in enumerate(rows):
        new_row: dict = {}
        for col, val in row.items():
            scanned += 1
            nv, used = clean_value(val, rules, unit_map.get(col, ""))
            new_row[col] = nv
            if used:
                changed += 1
                for r in used:
                    by_rule[r] = by_rule.get(r, 0) + 1
                if len(samples) < sample_limit:
                    samples.append({"row": i + 1, "column": col,
                                    "before": val, "after": nv, "rules": used})
        out.append(new_row)

    report = {
        "rules": [r for r in rules],
        "scanned_cells": scanned,
        "changed_cells": changed,
        "by_rule": by_rule,
        "samples": samples,
        "sample_truncated": changed > len(samples),
    }
    return out, report


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """去掉完全重复的行（同一批重复抽取是主要来源）。返回 (新行, 去掉条数)。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        key = tuple(sorted((k, str(v)) for k, v in row.items() if v not in (None, "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out, len(rows) - len(out)
