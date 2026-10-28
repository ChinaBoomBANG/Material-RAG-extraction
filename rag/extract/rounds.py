"""多轮定向补全：在已有结果上反复补缺字段，直到某一轮不再有任何变化。

为什么不是"整条重抽多轮"
------------------------
把同一段文本整篇重问三遍，模型给出的还是同一批值 —— 后面的轮次几乎不产生新信息，
成本却按轮数线性增长（单块实测 9~210 秒）。真正能补出东西的是"换证据来源"：
首轮抽的是表格块，缺的工艺参数往往在实验段落里，所以要**按牌号把相关段落检索出来**，
再只问缺的那几个字段。

三条硬规则（少一条都不行）
--------------------------
1. 新值必须有逐字引文（`verify_quote`）且 raw 能在原文找到（`verify_raw`）；
2. 原值为空 -> 填（`added`）；原值**没有**逐字证据、新值有 -> 可替换（`corrected`）；
   两者都有证据且不同 -> **不覆盖**（`conflict`），因为那通常是不同工艺条件的数据；
3. 连续一轮既无 `added` 也无 `corrected` 即停（收敛），上限 `max_rounds`。
"""

from __future__ import annotations

from .normalize import (DEFAULT_MAX_WT, ELEMENT_MAX_WT, canonical_unit, parse_value,
                        quantity_issue, quantity_kind, sanity_issue, verify_quote,
                        verify_raw)

FILL_PROMPT = """你是材料文献数据抄录员。下面给出【一条已有记录】和【文献片段】。

任务：只针对下面列出的**缺失字段**，在片段里找原文写出的值并抄录。
另外，如果你在片段里看到与【已有值】**明显冲突**的表述（例如原文写 175 °C，而已有值是 1 °C），
把它放进 corrections，并在 why 里说明依据。

硬性规则：
1. 只抄原文明确写出的内容，**不许推断、不许换算、不许补全**；原文没有就返回空。
2. 每个字段必须给出 quote：从片段里逐字复制的连续片段，其中要含该数值及其单位。
   不得改写、不得拼接多句、不得翻译。
3. 数值原样抄写（"0.5~0.6"、"≤0.05"、"Bal." 都照抄）；不要取中值。
4. 字段 key 必须与下面清单完全一致。只输出 JSON，不要解释文字、不要代码围栏：
{"fields": {"<key>": {"raw": "", "quote": ""}},
 "corrections": {"<key>": {"raw": "", "quote": "", "why": ""}}}
5. 什么都没找到时返回 {"fields": {}, "corrections": {}}。
"""

_ROLE_KW = {
    "composition": ("成分", "含量", "wt", "wt.%", "chemical composition", "composition"),
    "process": ("固溶", "时效", "淬火", "solution", "aging", "ageing", "quench",
                "homogeni", "anneal", "extrud", "热处理"),
    "microstructure": ("组织", "晶粒", "析出", "microstructure", "grain", "precipitat"),
    "properties": ("强度", "硬度", "延伸", "伸长", "腐蚀", "strength", "hardness",
                   "elongation", "corrosion", "ductility", "tensile"),
}


# ------------------------------------------------------------------ 缺什么

def missing_fields(rec, specs: dict, groups: set[str] | None = None) -> list[str]:
    """该记录还缺哪些字段（只返回目标表里真实存在的字段）。"""
    groups = groups or {"composition", "process", "microstructure", "properties"}
    have: dict[str, dict] = {"composition": rec.composition, "process": rec.process,
                             "microstructure": rec.microstructure,
                             "properties": rec.properties}
    out: list[str] = []
    for key, sp in specs.items():
        if key == "alloy":
            continue
        group = key.split(".", 1)[0] if "." in key else "properties"
        if group not in groups:
            continue
        short = key.split(".", 1)[1] if "." in key else key
        fv = have.get(group, {}).get(short)
        if fv is None or getattr(fv, "value", None) in (None, ""):
            out.append(key)
    return out


# ------------------------------------------------------------------ 找证据

def _field_patterns(key: str, specs: dict) -> list:
    """某个字段在原文里"值"可能长什么样（用于给片段打分，而不是靠猜）。"""
    import re as _re
    qk = quantity_kind((specs.get(key).label if specs.get(key) else "") or "", key)
    pats = []
    if qk.endswith("temp") or qk == "temperature":
        pats.append(_re.compile(r"\d{2,4}\s*(?:°\s*C|℃|K\b)"))
    elif qk.endswith("time") or qk == "time":
        pats.append(_re.compile(r"\d+(?:\.\d+)?\s*(?:h\b|hours?\b|min\b|minutes?\b)"))
    elif qk == "strength":
        pats.append(_re.compile(r"\d{2,4}\s*MPa"))
    elif qk == "elongation":
        pats.append(_re.compile(r"\d{1,2}(?:\.\d+)?\s*%"))
    return pats


def evidence_pool(store, rec, keys: list[str], specs: dict, top_k: int = 6) -> list:
    """给每个缺失字段各自找最相关的片段，再合并。

    为什么按字段分别找：一次只看"整体关键词"时，工艺列会被别的主题挤掉
    （实测：找固溶温度，检索到的是应力-应变曲线那一段）。所以每个字段给
    自己的配额，宁可少给也别让一个字段占满。
    """
    doc = getattr(rec, "source_doc", "") or ""
    alloy = (getattr(rec, "alloy", "") or getattr(rec, "row_label", "") or "").strip()
    cands = [c for c in store.chunks
             if not (doc and getattr(c, "doc_name", "") and c.doc_name != doc)]
    if not cands:
        return []

    picked: dict[str, object] = {}
    per_field = max(1, top_k // max(1, len(keys)))
    for key in keys:
        group = key.split(".", 1)[0] if "." in key else "properties"
        sp = specs.get(key)
        kws = set(_ROLE_KW.get(group, ()))
        if sp and sp.label:
            kws.add(sp.label.lower())
        pats = _field_patterns(key, specs)
        scored = []
        for i, c in enumerate(cands):
            low = (c.text or "").lower()
            score = 0.0
            if alloy and alloy.lower() in low:
                score += 6                      # 同一牌号出现，优先级最高
            for k in kws:
                if k and k in low:
                    score += 1
            for pat in pats:
                if pat.search(c.text or ""):
                    score += 3                  # 片段里真有"数值+单位"
            if group in ("composition", "properties") and getattr(c, "kind", "") == "table":
                score += 1.5
            if group == "process" and getattr(c, "kind", "") == "text":
                score += 1
            if score > 0:
                scored.append((score, -i, c))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        for _s, _i, c in scored[:per_field]:
            picked[getattr(c, "chunk_id", str(id(c)))] = c
    if len(picked) < top_k:                     # 配额没用满，用相对最好的补上
        rest = sorted(cands, key=lambda c: -sum(
            1 for k in ("固溶", "时效", "solution", "aging", "quench")
            if k in (c.text or "").lower()))
        for c in rest:
            if len(picked) >= top_k:
                break
            picked.setdefault(getattr(c, "chunk_id", str(id(c))), c)
    return list(picked.values())[:top_k]


# ------------------------------------------------------------------ 问一轮

def _schema_brief(keys: list[str], specs: dict) -> str:
    lines = []
    for key in keys:
        sp = specs.get(key)
        unit = f"（单位 {sp.unit}）" if sp and sp.unit else ""
        label = sp.label if sp else key
        lines.append(f"- {key}：{label}{unit}")
    return "\n".join(lines)


def _existing_brief(rec, specs: dict) -> str:
    lines = []
    for group, bucket in (("成分", rec.composition), ("工艺", rec.process),
                          ("组织", rec.microstructure), ("性能", rec.properties)):
        if not bucket:
            continue
        pairs = [f"{k}={getattr(v, 'value', '')}" for k, v in bucket.items()
                 if getattr(v, "value", None) not in (None, "")]
        if pairs:
            lines.append(f"{group}：{'；'.join(pairs)}")
    if rec.alloy:
        lines.insert(0, f"合金/样品：{rec.alloy}")
    return "\n".join(lines) or "（暂无字段值）"


def ask_round(llm, rec, keys: list[str], specs: dict, chunks: list) -> dict:
    """只问缺的字段，返回模型原始 JSON。"""
    if not keys or not chunks:
        return {}
    context = "\n\n".join(f"[{i + 1}] {c.text}" for i, c in enumerate(chunks))
    user = (f"【缺失字段清单】\n{_schema_brief(keys, specs)}\n\n"
            f"【这条记录已有的值】\n{_existing_brief(rec, specs)}\n\n"
            f"【文献片段】\n{context}")
    try:
        obj = llm.chat_json(user, system=FILL_PROMPT)
    except Exception:                      # 单条记录补全失败不该毁掉整轮
        return {}
    return obj if isinstance(obj, dict) else {}


# ------------------------------------------------------------------ 并一轮

def _verified_value(val: dict, chunks: list) -> dict | None:
    """校验模型给的值：引文逐字命中 + raw 逐字命中，任一不过就丢弃。"""
    if not isinstance(val, dict):
        return None
    raw = str(val.get("raw") or "").strip()
    quote = str(val.get("quote") or "").strip()
    if not raw or not quote:
        return None
    texts = [(c.text or "") for c in chunks]
    hit = verify_quote(quote, texts)
    if hit < 0:
        return None
    if not verify_raw(raw, texts):
        return None
    return {"raw": raw, "quote": quote, "chunk": chunks[hit],
            "unit": str(val.get("unit") or "").strip()}


def merge_round(rec, obj: dict, specs: dict, chunks: list, round_no: int,
                log: list[dict]) -> dict:
    """把一轮的候选值并入记录，返回 {added, corrected, conflict, skipped}。"""
    from ..models import Evidence, FieldValue

    stats = {"added": 0, "corrected": 0, "conflict": 0, "skipped": 0}
    fields = (obj or {}).get("fields") or {}
    corr = (obj or {}).get("corrections") or {}
    cand: dict[str, dict] = {}
    for key, val in list(fields.items()) + list(corr.items()):
        if key not in specs:
            continue
        v = _verified_value(val, chunks)
        if v is None:
            stats["skipped"] += 1
            log.append(_log_row(rec, key, round_no, "unverified",
                                "", val.get("raw") if isinstance(val, dict) else "",
                                "", ""))
            continue
        cand[key] = v

    for key, v in cand.items():
        sp = specs[key]
        group = key.split(".", 1)[0] if "." in key else "properties"
        bucket = {"composition": rec.composition, "process": rec.process,
                  "microstructure": rec.microstructure}.get(group, rec.properties)
        short = key.split(".", 1)[1] if "." in key else key
        unit = canonical_unit(sp.unit) if sp.unit else canonical_unit(v["unit"])

        # ---- 量纲 / 常识校验（新值同样要过）----
        parsed = parse_value(v["raw"], sp.kind or "number")
        scalar = parsed.get("value") if parsed["kind"] == "single" else parsed.get("raw")
        qk = quantity_kind(sp.label or "", key)
        if qk and isinstance(scalar, (int, float)) and quantity_issue(qk, scalar, v["quote"]):
            stats["skipped"] += 1
            log.append(_log_row(rec, key, round_no, "rejected-scale", "", v["raw"], "", v["quote"]))
            continue
        if canonical_unit(unit) == "wt.%" and isinstance(scalar, (int, float)):
            if sanity_issue(key, scalar, unit, ELEMENT_MAX_WT, DEFAULT_MAX_WT):
                stats["skipped"] += 1
                log.append(_log_row(rec, key, round_no, "rejected-sanity", "",
                                    v["raw"], "", v["quote"]))
                continue

        old = bucket.get(short)
        old_val = getattr(old, "value", None) if old is not None else None
        new_val = parsed.get("value") if parsed["kind"] in ("single", "text") \
            else parsed.get("raw")
        ev = Evidence(quote=v["quote"], doc_name=getattr(rec, "source_doc", ""),
                      chunk_id=getattr(v["chunk"], "chunk_id", ""),
                      page=getattr(v["chunk"], "page", 0), verified=True)

        if old_val in (None, ""):
            bucket[short] = FieldValue(value=new_val, unit=unit, evidence=ev,
                                       parsed=dict(parsed, round=round_no, action="added"))
            stats["added"] += 1
            log.append(_log_row(rec, key, round_no, "added", old_val, new_val,
                                v["quote"], v["quote"]))
            continue

        if str(old_val).strip() == str(new_val).strip():
            stats["skipped"] += 1
            continue

        old_verified = bool(getattr(getattr(old, "evidence", None), "verified", False))
        if not old_verified:
            # 原值没有逐字证据（首轮可能来自文档级工艺卡或文本推断），新值有 -> 可替换
            bucket[short] = FieldValue(value=new_val, unit=unit, evidence=ev,
                                       parsed=dict(parsed, round=round_no,
                                                   action="corrected",
                                                   replaced=str(old_val)))
            stats["corrected"] += 1
            log.append(_log_row(rec, key, round_no, "corrected", old_val, new_val,
                                v["quote"], v["quote"]))
        else:
            # 两边都有逐字证据却是不同值 -> 很可能是不同工艺条件，绝不覆盖
            stats["conflict"] += 1
            rec.warnings.append(
                f"第 {round_no} 轮发现与已有值不同的值：{key} 已有 {old_val}、"
                f"片段里写 {new_val}（两份都有原文证据，保留原值，请人工确认是否不同条件）")
            log.append(_log_row(rec, key, round_no, "conflict", old_val, new_val,
                                v["quote"], v["quote"]))
    return stats


def _log_row(rec, key, round_no, action, old, new, quote, quote2) -> dict:
    return {"round": round_no, "record_id": getattr(rec, "record_id", ""),
            "doc": getattr(rec, "source_doc", ""),
            "alloy": getattr(rec, "alloy", "") or getattr(rec, "row_label", ""),
            "field": key, "action": action,
            "old": "" if old is None else str(old),
            "new": "" if new is None else str(new),
            "quote": (quote or quote2 or "")[:300]}


# ------------------------------------------------------------------ 驱动

def fill_rate(records: list, specs: dict) -> dict:
    """逐字段填充率（分母=记录数），用于报告"补全前/后"。"""
    n = len(records) or 1
    per: dict[str, float] = {}
    for key in specs:
        if key == "alloy":
            continue
        group = key.split(".", 1)[0] if "." in key else "properties"
        short = key.split(".", 1)[1] if "." in key else key
        bucket_of = {"composition": "composition", "process": "process",
                     "microstructure": "microstructure"}.get(group, "properties")
        got = 0
        for r in records:
            fv = getattr(r, bucket_of, {}).get(short)
            if fv is not None and getattr(fv, "value", None) not in (None, ""):
                got += 1
        per[key] = round(got / n, 4)
    overall = round(sum(per.values()) / len(per), 4) if per else 0.0
    return {"records": len(records), "overall": overall, "columns": per}


def run_rounds(llm, store, records: list, specs: dict, conf: dict,
               on_event=None, should_stop=None) -> dict:
    """反复补缺，直到一轮内没有 added/corrected，或达到 max_rounds。"""
    rc = conf["extract"].get("rounds") or {}
    max_rounds = max(1, int(rc.get("max_rounds", 3) or 3))
    top_k = max(1, int(rc.get("top_k_chunks", 6) or 6))
    only_missing = bool(rc.get("only_missing", True))
    budget = int(rc.get("max_calls_total", 400) or 400)
    groups = rc.get("groups") or ["composition", "process", "microstructure", "properties"]

    before = fill_rate(records, specs)
    log: list[dict] = []
    calls = 0
    rounds_run = 0
    converged = False
    stats = {"added": 0, "corrected": 0, "conflict": 0, "skipped": 0}

    for rnd in range(1, max_rounds + 1):
        if should_stop and should_stop():
            break
        added = corrected = 0
        touched = 0
        for rec in records:
            if calls >= budget:
                break
            keys = missing_fields(rec, specs, set(groups)) if only_missing else list(specs)
            keys = [k for k in keys if k != "alloy"]
            if not keys:
                continue
            chunks = evidence_pool(store, rec, keys, specs, top_k=top_k)
            if not chunks:
                continue
            obj = ask_round(llm, rec, keys, specs, chunks)
            calls += 1
            if not obj:
                continue
            st = merge_round(rec, obj, specs, chunks, rnd, log)
            for k in ("added", "corrected", "conflict", "skipped"):
                stats[k] += st[k]
            added += st["added"]
            corrected += st["corrected"]
            if st["added"] or st["corrected"]:
                touched += 1
        rounds_run = rnd
        if on_event:
            on_event({"type": "round", "round": rnd, "added": added,
                      "corrected": corrected, "records": touched})
        if added == 0 and corrected == 0:
            converged = True            # 收敛：这一轮什么都没补出来
            break

    after = fill_rate(records, specs)
    delta = {k: round(after["columns"][k] - before["columns"].get(k, 0), 4)
             for k in after["columns"]}
    return {"rounds_run": rounds_run, "max_rounds": max_rounds, "calls": calls,
            "added": stats["added"], "corrected": stats["corrected"],
            "conflict": stats["conflict"], "skipped": stats["skipped"],
            "converged": converged,
            "fill_before": before, "fill_after": after, "fill_delta": delta,
            "log": log}
