"""字段-数值对齐校验：确认「这个数值」确实属于「这个字段」。

证据回链（`verify_quote` 证明引文能在原文里逐字找到）只说明引文来自原文，
**证明不了它说的是这个字段**。最常见的错就是表格列错位：文献里一张
ΔGseg（晶界偏聚自由能，kJ/mol）的表，列名带 of Mg / of Si / of Cu，
模型把 −9.127 塞进 Mg 的质量分数，引文校验照样逐字通过。

所以再补一层**位置对齐**：字段名必须能在表头（或行首列、或引文上下文）里
找到对应。找不到就判「未识别」，按配置留空——宁可留空，绝不猜一个填上。
"""

from __future__ import annotations

import re

from .schema import FieldSpec

# 表格块文本以 Markdown 管道表呈现：
#   [表格] <表题>
#   [表头分组] ...（可选）
#   | col1 | col2 | ... |
#   |---|---|---|
#   | ... |
_SEP_RE = re.compile(r"^\s*\|?[\s:\-\|]+\|?\s*$")
_CJK_RE = re.compile(r"[一-龥]")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z一-龥]+")


def _is_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and s.startswith("|") and bool(_SEP_RE.match(s))


def table_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("|")]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|") if c.strip()]


def header_cells(text: str) -> list[str]:
    """表头单元格：分隔符 `|---|---|` 之前的所有管道行（多层表头会占多行）。"""
    lines = table_lines(text)
    if not lines:
        return []
    out: list[str] = []
    for ln in lines:
        if _is_sep(ln):
            break
        out.extend(_cells(ln))
    return out


def row_first_cells(text: str) -> list[str]:
    """每行第一个单元格：元素按行排列的表（第一列是元素名）靠它对齐。"""
    lines = table_lines(text)
    try:
        start = next(i for i, ln in enumerate(lines) if _is_sep(ln)) + 1
    except StopIteration:
        return []
    out: list[str] = []
    for ln in lines[start:]:
        cs = _cells(ln)
        if cs:
            out.append(cs[0])
    return out


def field_tokens(spec: FieldSpec | None) -> list[str]:
    """字段的可匹配写法：标签 + 别名（含中文别名），统一小写。"""
    if spec is None:
        return []
    toks: list[str] = []
    for s in [spec.label or "", *(spec.aliases or [])]:
        s = (s or "").strip()
        if s:
            toks.append(s.lower())
    return toks


# 表头里常见的"装饰词"：单位、括号内容、以及 ΔG 表那种 "of Mg" / "in Si" 的
# 前置介词。去掉它们之后再比对，才能区分「Si 这一列」和「of Si 这一列」。
# 只清掉"单位/括号"这类装饰；介词（of / in / for）**保留**——
# "of Mg" 里虽然出现了 Mg，但它不是"Mg 的质量分数"这一列，留着才能区分开。
_UNIT_JUNK_RE = re.compile(
    r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|（(?:[^）]*)）"
    r"|wt\.?%|mass%|at\.?%|ppm|%|mpa|gpa|kj/mol|j/mol|°c", re.I)


def _bare_tokens(text: str) -> list[str]:
    """去掉单位/括号/介词后的实义词，用于判断"这一列到底是不是这个字段"。"""
    cleaned = _UNIT_JUNK_RE.sub(" ", text or "")
    return [t for t in _TOKEN_SPLIT_RE.split(cleaned.lower()) if t]


def _has_token(token: str, text: str) -> bool:
    """整词匹配（英文/数字）或直接子串匹配（中文）。"""
    if not token or not text:
        return False
    low = text.lower()
    if _CJK_RE.search(token):
        return token in low
    return token in set(t for t in _TOKEN_SPLIT_RE.split(low) if t)


# 热力学/能量类表头：这类列根本不是成分列（ΔGseg、Gb、kJ/mol…）
_NON_COMPOSITION = {"gseg", "dg", "gb", "energy", "enthalpy", "ev", "kj", "j", "mol"}
# 介词：紧跟在元素名前面的 of/in/for/at，说明这一列是"某物的 Si"而不是"Si 含量"
_PREP = {"of", "in", "for", "at", "to", "per"}


def _cell_is_field(token: str, cell: str) -> bool:
    """判断一个表头单元格**这一列**是不是这个字段（而不是"提到"这个字段）。

    算：`Si`、`Si (wt.%)`、`Si, wt.%`、`Element Si`、`Mass fraction of Si`
    不算：`of Mg`（ΔG 表里"Mg 的晶界偏聚能"）、`ΔGseg Gb of Mg`
    ——它们虽然出现了 Mg 这个词，但那一列不是 Mg 的质量分数。
    """
    if not token or not cell:
        return False
    if _CJK_RE.search(token):
        return token in cell.lower()
    t = token.lower()
    toks = _bare_tokens(cell)
    if not toks:
        return False
    if any(x in _NON_COMPOSITION for x in toks):
        return False
    try:
        i = toks.index(t)
    except ValueError:
        return False
    # 短表头里被介词带着（"of Mg"）就不是成分列；长表头（"Mass fraction of Si"）
    # 是描述性写法，仍算。
    if i > 0 and toks[i - 1] in _PREP and len(toks) <= 3:
        return False
    return True


def has_token_in(token: str, cells: list[str]) -> bool:
    return any(_cell_is_field(token, c) for c in cells)


def alignment_issue(spec: FieldSpec | None, quote: str,
                    chunk_text: str = "", kind: str = "text",
                    window: int = 120) -> str:
    """返回空串表示能确认对齐；否则返回无法确认的原因（供「未识别」标注用）。"""
    toks = field_tokens(spec)
    if not toks:
        return ""
    label = (spec.label if spec else "") or "该字段"

    if kind == "table":
        pool = header_cells(chunk_text) + row_first_cells(chunk_text)
        if any(has_token_in(t, pool) for t in toks):
            return ""
        head = " / ".join(header_cells(chunk_text)[:8]) or "—"
        return (f"表格里没有与「{label}」对应的表头或行标签"
                f"（表头：{head}），无法确认该数值属于这个字段")

    # 正文块：字段名要出现在引文里，或出现在引文前后一小段上下文里
    if quote and chunk_text:
        pos = chunk_text.find(quote)
        if pos >= 0:
            seg = chunk_text[max(0, pos - window): pos + len(quote) + window]
            if any(_has_token(t, seg) for t in toks):
                return ""
            return f"引文上下文里找不到「{label}」的任何写法，无法确认对应关系"
    return ""
