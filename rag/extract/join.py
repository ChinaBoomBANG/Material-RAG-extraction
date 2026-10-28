"""把散落的记录拼接成「一行一个样品」：继承 + 表连接 + 条件分行。

问题出在哪
----------
块级抽取会把同一篇文献的同一批样品拆成多条记录：成分表那个块出一条（只有成分）、
性能表那个块出一条（只有性能）、实验段落那个块出一条（只有工艺）。实测 979 条记录里
342 条只有性能，其中 167 条所在的文献明明有成分记录 —— 数据在库里，只是没挂到一起。

三段处理
--------
1. **样品级继承（确定性，零成本）**：成分与牌号是「样品属性」，不是「条件属性」。
   同（文献, 标签）的记录里，只要有任一条带成分，其余记录就继承它。
   这条规则救回实测 89 条同标签 + 78 条全篇唯一成分。
2. **表连接（先规则、再模型）**：成分表常写 `1`/`2`，性能表写 `AA6082`/`AA6005A`。
   先做确定性匹配（去前缀 AA/Al、数字核心相等、互相包含），匹配不上再让模型
   输出**只含映射关系**的小 JSON（几十个字符，不会撞输出上限）。
3. **按条件分行**：一个样品可能有多个时效条件（M3 在 175 °C/8 h 和 190 °C/12 h 各一条）。
   这时不能把两条并成一条（值会打架），而是**每个条件一行，成分复制到每一行**。
   这正是"一行一个样品"与"不丢数据"能同时成立的做法。
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict

LINK_PROMPT = """你在做文献表格的样品标签对齐。

给你两组标签：S 是**成分表里的样品标签**（可信、每个样品成分齐全），U 是**其他表里出现、
但成分表里找不到同名标签**的标签。

任务：判断 U 里每个标签对应 S 里的哪个样品。只输出映射，不要解释。

规则：
1. 只按字面能确定的关系匹配：去掉厂牌前缀（AA / Al / AISI 等）、数字或字母编号相同、
   或一个标签明确包含另一个。例如 AA6082 与 6082 是同一个样品。
2. 判断不了就写 null —— **宁可不配，也不要猜**。不同样品被错配，比留空更糟。
3. 只输出 JSON：{"<U里的标签>": "<S里的标签或 null>"}
"""


# ------------------------------------------------------------------ 基础工具

def _norm(s) -> str:
    return "".join(str(s or "").split()).casefold()


def _lab(rec) -> str:
    """样品的归一标签：牌号优先，退到行标签。"""
    return _norm(getattr(rec, "alloy", "") or getattr(rec, "row_label", ""))


def _has(bucket: dict) -> bool:
    return any(getattr(v, "value", None) not in (None, "") for v in (bucket or {}).values())


def _core(s: str) -> str:
    """标签的"核心编号"：去掉 AA/Al 等前缀与所有非字母数字。"""
    t = re.sub(r"[^0-9a-z]", "", str(s).casefold())
    return re.sub(r"^(aa|al|aisi|astm|en|jis)", "", t)


def _compatible(slot: dict, rec) -> bool:
    """这条记录的工艺/性能能否放进该行：同名字段要么没有、要么值相同。"""
    for bucket, name in ((rec.process, "process"), (rec.properties, "properties"),
                         (rec.microstructure, "microstructure")):
        for k, fv in bucket.items():
            cur = slot[name].get(k)
            if cur is None:
                continue
            a, b = getattr(cur, "value", None), getattr(fv, "value", None)
            if a not in (None, "") and b not in (None, "") and str(a).strip() != str(b).strip():
                return False
    return True


def _cond_key(rec) -> tuple:
    """条件指纹：工艺字段值不同就视为不同条件。"""
    return tuple(sorted((k, str(getattr(v, "value", ""))) for k, v in (rec.process or {}).items()))


# ------------------------------------------------------------------ 1. 样品级继承

def inherit_within_sample(records: list) -> dict:
    """同（文献, 标签）内把成分补到没成分的记录上。

    成分是样品属性：M3 在 8 h 和 12 h 两个时效条件各一条记录，
    两个条件都属于 M3 这个样品，成分必须都在 —— 这不是推断，是同一个样品的定义。
    """
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in records:
        lab = _lab(r)
        if lab:
            groups[(getattr(r, "source_doc", ""), lab)].append(r)

    stats = {"groups": 0, "filled": 0, "records": 0}
    for rs in groups.values():
        donor = max(rs, key=lambda r: sum(1 for v in (r.composition or {}).values()
                                         if getattr(v, "value", None) not in (None, "")))
        if not _has(getattr(donor, "composition", {})):
            continue
        stats["groups"] += 1
        for r in rs:
            if r is donor:
                continue
            got = 0
            for k, fv in donor.composition.items():
                cur = r.composition.get(k)
                if cur is not None and getattr(cur, "value", None) not in (None, ""):
                    continue
                new = copy.deepcopy(fv)
                new.parsed = dict(new.parsed or {}, provenance="inherit-sample")
                r.composition[k] = new
                got += 1
            if got:
                stats["filled"] += got
                stats["records"] += 1
    return stats


# ------------------------------------------------------------------ 2. 表连接

def link_labels(records: list, llm=None, max_calls: int = 200) -> dict:
    """把「没有成分记录的标签」连到「有成分记录的标签」上。

    先确定性匹配（AA6082 -> 6082），再可选地让模型给一个只含映射的小 JSON。
    """
    by_doc: dict[str, list] = defaultdict(list)
    for r in records:
        by_doc[getattr(r, "source_doc", "")].append(r)

    stats = {"docs": 0, "rule_linked": 0, "llm_linked": 0, "unmatched": 0,
             "calls": 0}
    mapping: dict[tuple[str, str], str] = {}

    for doc, rs in by_doc.items():
        samples = {_lab(r) for r in rs if _has(getattr(r, "composition", {}))}
        orphans = {_lab(r) for r in rs
                   if not _has(getattr(r, "composition", {}))} - {""} - samples
        if not samples or not orphans:
            continue
        stats["docs"] += 1

        # ---- 2a. 确定性匹配 ----
        left = set()
        for u in orphans:
            hit = ""
            if u in samples:
                hit = u
            else:
                cu = _core(u)
                for s in samples:
                    cs = _core(s)
                    if cu and cs and (cu == cs or (len(cs) >= 3 and cs in cu)
                                      or (len(cu) >= 3 and cu in cs)):
                        hit = s
                        break
            if hit:
                mapping[(doc, u)] = hit
                stats["rule_linked"] += 1
            else:
                left.add(u)

        # ---- 2b. 模型兜底（只输出映射，输出极小）----
        if left and llm is not None and stats["calls"] < max_calls:
            try:
                obj = llm.chat_json(
                    f"S = {sorted(samples)}\nU = {sorted(left)}", system=LINK_PROMPT)
                stats["calls"] += 1
            except Exception:                     # noqa: BLE001 连接失败不影响主流程
                obj = {}
            if isinstance(obj, dict):
                for u, s in obj.items():
                    ul, sl = _norm(u), _norm(s)
                    if ul in left and sl in samples:
                        mapping[(doc, ul)] = sl
                        stats["llm_linked"] += 1
                        left.discard(ul)
        stats["unmatched"] += len(left)

    # 把映射写成 record 上的标记（不覆盖原标签，保留可追溯性）
    for r in records:
        doc, lab = getattr(r, "source_doc", ""), _lab(r)
        target = mapping.get((doc, lab))
        if target and not _has(getattr(r, "composition", {})):
            r.warnings.append(f"表连接：标签 {r.alloy or r.row_label} 对应成分表里的 {target}")
            r.__dict__["linked_label"] = target
    return {**stats, "mapping": {f"{d}::{u}": s for (d, u), s in mapping.items()}}


def effective_label(rec) -> str:
    """参与分组的标签：被连接过的用连接结果。"""
    return rec.__dict__.get("linked_label") or _lab(rec)


def apply_links(records: list) -> int:
    """把连接结果落实成成分继承（连接后等同于同一个样品）。"""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in records:
        lab = effective_label(r)
        if lab:
            groups[(getattr(r, "source_doc", ""), lab)].append(r)
    n = 0
    for rs in groups.values():
        donor = max(rs, key=lambda r: sum(1 for v in (r.composition or {}).values()
                                         if getattr(v, "value", None) not in (None, "")))
        if not _has(getattr(donor, "composition", {})):
            continue
        for r in rs:
            if r is donor:
                continue
            for k, fv in donor.composition.items():
                cur = r.composition.get(k)
                if cur is not None and getattr(cur, "value", None) not in (None, ""):
                    continue
                new = copy.deepcopy(fv)
                new.parsed = dict(new.parsed or {}, provenance="inherit-linked")
                r.composition[k] = new
                n += 1
    return n


# ------------------------------------------------------------------ 3. 按条件成形

def _comp_sig(rec) -> tuple:
    """成分指纹：成分不同 = 不是同一个样品（同名也要分开，不能互相覆盖）。"""
    return tuple(sorted((k, str(getattr(v, "value", "")).strip())
                        for k, v in (getattr(rec, "composition", {}) or {}).items()
                        if getattr(v, "value", None) not in (None, "")))


def _rows_for_group(rs: list, doc: str) -> tuple[list, int]:
    """一个（文献, 标签）组 -> 若干行。成分签名不同的子组各算不同样品。"""
    from ..models import MaterialRecord

    sigs: dict[tuple, list] = {}
    blanks: list = []
    for r in rs:
        sig = _comp_sig(r)
        (sigs.setdefault(sig, []).append(r) if sig else blanks.append(r))

    out: list = []
    multi = 0
    # 有成分的子组：每个签名一个样品
    for sig, sub in sigs.items():
        donor = sub[0]
        sample_fields = copy.deepcopy(donor.composition)
        sample_alloy = donor.alloy or donor.row_label
        out.extend(_rows_for_sample(sub, doc, sample_alloy, donor, sample_fields))
    # 没成分的记录：只有该文献/标签下只有一个成分样品时才能安全挂靠
    if blanks:
        if len(sigs) == 1:
            donor = next(iter(sigs.values()))[0]
            out.extend(_rows_for_sample(blanks, doc, donor.alloy or donor.row_label,
                                        donor, copy.deepcopy(donor.composition)))
        else:
            for r in blanks:
                out.extend(_rows_for_sample([r], doc, r.alloy or r.row_label, r, {}))
    if len(sigs) + (1 if blanks else 0) > 1:
        multi = 1
    return out, multi


def _rows_for_sample(rs: list, doc: str, alloy: str, donor, sample_fields: dict) -> list:
    """同一（样品, 条件集合）-> 行：冲突的记录各占一行。"""
    from ..models import MaterialRecord

    slots: list[dict] = []

    def _fit(r) -> dict:
        for slot in slots:
            if _compatible(slot, r):
                return slot
        slot = {"process": {}, "properties": {}, "microstructure": {},
                "page": r.source_page, "rid": r.record_id}
        slots.append(slot)
        return slot

    for r in rs:
        if not (_has(r.process) or _has(r.properties) or _has(r.microstructure)):
            continue
        slot = _fit(r)
        for bucket, name in ((r.process, "process"), (r.properties, "properties"),
                             (r.microstructure, "microstructure")):
            for k, fv in bucket.items():
                cur = slot[name].get(k)
                if cur is None or getattr(cur, "value", None) in (None, ""):
                    slot[name][k] = fv

    if not slots:                       # 只有成分的样品也要占一行
        if sample_fields or alloy:
            return [MaterialRecord(record_id=f"{getattr(donor, 'record_id', '')}-s",
                                   source_doc=doc,
                                   source_page=getattr(donor, "source_page", 0),
                                   alloy=alloy,
                                   row_label=getattr(donor, "row_label", ""),
                                   composition=copy.deepcopy(sample_fields))]
        return []

    out = []
    for slot in slots:
        out.append(MaterialRecord(record_id=slot["rid"] + "-s", source_doc=doc,
                                  source_page=slot["page"], alloy=alloy,
                                  row_label=getattr(donor, "row_label", ""),
                                  composition=copy.deepcopy(sample_fields),
                                  process=slot["process"],
                                  properties=slot["properties"],
                                  microstructure=slot["microstructure"]))
    return out


def shape_rows(records: list) -> tuple[list, dict]:
    """重组为「一行一个样品（同一样品的不同条件各占一行）」并去重。"""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    loose = []
    for r in records:
        lab = effective_label(r)
        (groups[(getattr(r, "source_doc", ""), lab)] if lab else loose).append(r)

    out: list = []
    stats = {"groups": 0, "rows": 0, "multi_condition": 0, "split_same_label": 0}
    for (doc, _lab_), rs in groups.items():
        stats["groups"] += 1
        rows, multi = _rows_for_group(rs, doc)
        if multi:
            stats["split_same_label"] += 1
        if len(rows) > 1:
            stats["multi_condition"] += 1
        out.extend(rows)
    out.extend(loose)
    stats["rows"] = len(out)
    return _dedupe_rows(out), stats


def _dedupe_rows(rows: list) -> list:
    """完全相同的行只留一条（同一块被重复抽的产物）。"""
    seen, out = set(), []
    for r in rows:
        sig = (r.source_doc, (r.alloy or r.row_label or "").casefold(),
               tuple(sorted((k, str(getattr(v, "value", ""))) for k, v in r.composition.items())),
               tuple(sorted((k, str(getattr(v, "value", ""))) for k, v in r.process.items())),
               tuple(sorted((k, str(getattr(v, "value", ""))) for k, v in r.properties.items())))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


# ------------------------------------------------------------------ 驱动

def run_join(records: list, llm=None, conf: dict | None = None) -> tuple[list, dict]:
    """继承 -> 表连接 -> 再继承 -> 按条件成形。返回 (新行列表, 统计)。"""
    jc = ((conf or {}).get("extract", {}) or {}).get("join") or {}
    if not jc.get("enabled", True) or not records:
        return records, {}
    before = len(records)
    s1 = inherit_within_sample(records) if jc.get("inherit", True) else {}
    s2 = link_labels(records, llm if jc.get("llm_link", True) else None,
                     int(jc.get("max_calls", 200) or 200)) \
        if jc.get("link_labels", True) else {}
    if s2:
        s2["applied_cells"] = apply_links(records)
    rows, s3 = shape_rows(records)
    return rows, {"records_before": before, "rows_after": len(rows),
                  "inherit": s1, "link": s2, "shape": s3}
