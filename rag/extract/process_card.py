"""文档级工艺配方卡：整篇文献的工艺路线抽一次，再补进每一条记录。

为什么需要它
------------
工艺参数（固溶温度/时间、淬火方式、时效温度/时间）在论文里通常**只说一次**——
写在 Experimental 段落里，或者写在某张表的表头/脚注里，而成分数据在另一张表。
块级抽取（一个块一次模型调用）永远看不到跨块的信息，于是：

    成分表那个块 -> 抽出只有成分的记录
    实验段落那个块 -> 抽出只有工艺的记录（往往没有牌号）

结果是同一批样品被拆成好几行、工艺列几乎全空。实测用户表格里工艺列填充率
0.8%~9.6%，就是这个原因，不是模型不会抽。

本模块的做法
------------
1. 每篇文献跑**一次**专门的调用，把该文献用到的工艺路线抄成结构化卡片，
   **每个字段带自己的逐字引文**。实测教训：一条路线常跨两句话，若要求整条路线
   一个 quote，模型会自己加省略号拼起来，逐字校验必然失败，等于白抽。
2. 再按「牌号匹配 -> 全篇唯一路线 -> 留空」三级规则把它填进缺工艺的记录；
3. 每条被补上的值都带出处标记（`per-alloy` / `paper-level`）并写进 warnings，
   方便人工复核——**绝不猜**：匹配不到、或文献有多条路线且对不上牌号时，一律留空。
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from .normalize import quantity_issue, verify_quote

CARD_PROMPT = """你是材料文献的工艺路线抄录员。下面是**同一篇文献**的正文片段。

任务：列出这篇文献里出现的**全部工艺路线**（一次完整的制备 + 热处理流程算一条）。

硬性规则：
1. 只抄原文明确写出的参数。原文没有的字段给空对象 {"raw": "", "quote": ""}——
   不要推断、不要补全、不要换算单位。
2. **每个字段单独给自己的 quote**：从下面片段里逐字复制、且**包含该数值及其单位**的
   那一小段。不要把两句话用省略号或逗号拼成一条 quote，也不要改写原文。
3. label 填这条路线对应的合金牌号/样品标识（原文怎么写就怎么抄，如 6061、Alloy A、A2）。
   文献只有一条通用路线、原文没写适用牌号时，label 留空。
4. 同一文献若报告了多组不同工艺（不同时效温度、不同固溶制度），**每条都要列出来**，
   不要合并、也不要只取第一组。
5. 只输出 JSON，不要解释文字、不要代码围栏：
{"routes": [{
  "label": "",
  "casting":       {"raw": "", "quote": ""},
  "solution_temp": {"raw": "", "quote": ""},
  "solution_time": {"raw": "", "quote": ""},
  "quench":        {"raw": "", "quote": ""},
  "aging_temp":    {"raw": "", "quote": ""},
  "aging_time":    {"raw": "", "quote": ""}
}]}
6. 片段里没有任何工艺信息时返回 {"routes": []}。
"""

ROLES = ("casting", "solution_temp", "solution_time", "quench",
         "aging_temp", "aging_time")

# 角色 -> 目标表字段标签里的关键词（判定哪一列承担这个角色）
ROLE_HINTS = {
    "casting": ("制备", "铸造", "加工", "成形", "成型", "casting",
                "preparation", "processing", "fabricat", "extrud", "roll"),
    "solution_temp": ("固溶温度", "solution temp", "solution treatment temp", "solutionizing"),
    "solution_time": ("固溶时间", "solution time", "solution treatment time",
                      "solutionizing time"),
    "quench": ("淬火", "冷却", "冷速", "quench", "cooling rate", "cooling"),
    "aging_temp": ("时效温度", "aging temp", "ageing temp", "artificial aging"),
    "aging_time": ("时效时间", "aging time", "ageing time"),
}

_CARD_CACHE = "process_cards.json"
_KW = re.compile(r"固溶|淬火|时效|solution|aging|ageing|quench|homogeni|anneal|extrud|rolling",
                 re.I)


# ------------------------------------------------------------------ 目标字段定位

def role_map(specs: dict) -> dict[str, str]:
    """找出目标表里承担各工艺角色的字段 key（找不到就不出现）。"""
    out: dict[str, str] = {}
    for role, hints in ROLE_HINTS.items():
        for key, sp in specs.items():
            # 不限于 process.*：按 Excel 表头建 schema 时，认不出的工艺列会落到
            # custom.*（进 properties 桶），但它的语义仍然是工艺，一样要能补。
            text = f"{sp.label or ''} {key}".lower()
            if any(h.lower() in text for h in hints):
                out[role] = key
                break
    return out


# ------------------------------------------------------------------ 收集文献正文

def collect_doc_text(store, doc_id: str, max_chars: int = 12000) -> str:
    """取该文献的正文，优先含工艺关键词的块（工艺往往只在一两段里出现）。"""
    chunks = [c for c in store.chunks if c.doc_id == doc_id]
    hot = [c for c in chunks if _KW.search(c.text or "")]
    cold = [c for c in chunks if c not in hot]
    buf: list[str] = []
    used = 0
    for c in hot + cold:
        if c.kind == "table":
            continue
        t = (c.text or "").strip()
        if not t:
            continue
        if used + len(t) > max_chars:
            t = t[: max(0, max_chars - used)]
        if not t:
            break
        buf.append(t)
        used += len(t)
        if used >= max_chars:
            break
    return "\n".join(buf)


# ------------------------------------------------------------------ 建卡 / 用卡

def _field(val, text: str) -> dict:
    """把一个字段解析成 {raw, quote, verified}，并核验引文。

    兼容两种模型输出：{"raw":..,"quote":..} 与直接给字符串。
    """
    if isinstance(val, dict):
        raw = str(val.get("raw") or "").strip()
        quote = str(val.get("quote") or "").strip()
    else:
        raw = str(val or "").strip()
        quote = raw
    if not raw:
        return {}
    ok = bool(quote) and verify_quote(quote, [text]) >= 0
    return {"raw": raw, "quote": quote, "verified": ok}


def build_card(llm, doc_id: str, doc_name: str, text: str) -> list[dict]:
    """一次模型调用，得到该文献的工艺路线列表（每个字段各自核验）。"""
    if not text.strip():
        return []
    obj = llm.chat_json(text, system=CARD_PROMPT)
    routes = obj.get("routes") if isinstance(obj, dict) else None
    if not isinstance(routes, list):
        return []
    out: list[dict] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        item = {"label": str(r.get("label") or "").strip(),
                "doc": doc_name, "doc_id": doc_id, "fields": {}}
        for role in ROLES:
            f = _field(r.get(role), text)
            if f:
                item["fields"][role] = f
        if item["fields"]:
            out.append(item)
    return out


def _norm_label(s: str) -> str:
    return "".join(str(s or "").split()).casefold()


def pick_route(rec_alloy: str, routes: list[dict]) -> tuple[dict | None, str]:
    """三级匹配：牌号精确 -> 牌号包含 -> 全篇唯一路线。

    匹配不到就返回 (None, "")——宁可留空，也不把 A 合金的工艺写到 B 合金上。
    """
    a = _norm_label(rec_alloy)
    if a:
        for r in routes:
            if _norm_label(r.get("label")) == a:
                return r, "per-alloy"
        for r in routes:
            lab = _norm_label(r.get("label"))
            if lab and (lab in a or a in lab):
                return r, "per-alloy"
    if len(routes) == 1:
        return routes[0], "paper-level"
    return None, ""


def apply_card(records: list, routes: list[dict], roles: dict[str, str]) -> dict:
    """把工艺卡片补进缺工艺的记录：只填空、不覆盖、不猜。"""
    from ..models import Evidence, FieldValue

    stats = {"filled": 0, "records_touched": 0, "per_alloy": 0,
             "paper_level": 0, "unmatched": 0, "unverified": 0}
    if not routes or not roles:
        return stats
    for rec in records:
        route, how = pick_route(getattr(rec, "alloy", "") or "", routes)
        if route is None:
            stats["unmatched"] += 1
            continue
        touched = False
        for role, spec_key in roles.items():
            f = (route.get("fields") or {}).get(role)
            if not f or not f.get("verified"):
                if f:
                    stats["unverified"] += 1
                continue
            raw = str(f.get("raw") or "").strip()
            if not raw:
                continue
            # 卡片里的值同样要过量纲校验（"时效温度 = 1 h" 一律不写）
            num = _num(raw)
            if num is not None and quantity_issue(role, num, f.get("quote", "")):
                stats["unverified"] += 1
                continue
            short = spec_key.split(".", 1)[1] if "." in spec_key else spec_key
            group = spec_key.split(".", 1)[0] if "." in spec_key else "properties"
            bucket = {"composition": rec.composition, "process": rec.process,
                      "microstructure": rec.microstructure}.get(group, rec.properties)
            if short in bucket and getattr(bucket[short], "value", None) not in (None, ""):
                continue                      # 已有值一律不动
            ev = Evidence(quote=f.get("quote", ""), doc_name=route.get("doc", ""),
                          verified=True)
            bucket[short] = FieldValue(value=raw, unit="", evidence=ev,
                                       parsed={"kind": "text", "raw": raw,
                                               "provenance": how})
            rec.warnings.append(
                f"工艺补全（{how}）：{role}={raw}，出处见该文献的工艺卡片")
            stats["filled"] += 1
            touched = True
        if touched:
            stats["records_touched"] += 1
            stats["per_alloy" if how == "per-alloy" else "paper_level"] += 1
    return stats


def _num(raw: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(raw).replace(",", ""))
    return float(m.group()) if m else None


# ------------------------------------------------------------------ 缓存

def _cache_path(conf: dict) -> str:
    idx = conf["project"]["index_dir"]
    if not os.path.isabs(idx):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        idx = os.path.join(root, idx)
    return os.path.join(idx, _CARD_CACHE)


def _load_cache(conf: dict) -> dict:
    p = _cache_path(conf)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(conf: dict, data: dict) -> None:
    p = _cache_path(conf)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _key(doc_id: str, model: str) -> str:
    return hashlib.md5(f"{doc_id}|{model}".encode("utf-8")).hexdigest()[:16]


def apply_process_cards(llm, store, records: list, specs: dict, conf: dict,
                        on_event=None) -> dict:
    """对本次抽取涉及的每篇文献建卡/读缓存，然后把工艺补进记录。"""
    card_conf = conf["extract"].get("process_card") or {}
    max_chars = int(card_conf.get("max_chars", 12000) or 12000)
    use_cache = bool(card_conf.get("cache", True))
    model = str(conf["llm"].get("model") or "")
    roles = role_map(specs)

    docs: list[str] = []
    for rec in records:
        if rec.source_doc and rec.source_doc not in docs:
            docs.append(rec.source_doc)

    cache = _load_cache(conf) if use_cache else {}
    total = {"filled": 0, "records_touched": 0, "per_alloy": 0,
             "paper_level": 0, "unmatched": 0, "unverified": 0,
             "cards": 0, "docs": len(docs)}
    for doc_name in docs:
        doc_id = _doc_id_of(store, doc_name)
        key = _key(doc_id or doc_name, model)
        routes = cache.get(key)
        if routes is None:
            text = collect_doc_text(store, doc_id, max_chars) if doc_id else ""
            routes = build_card(llm, doc_id or doc_name, doc_name, text)
            if use_cache:
                cache[key] = routes
                _save_cache(conf, cache)
        if on_event:
            on_event({"type": "process_card", "doc": doc_name, "routes": len(routes)})
        total["cards"] += 1
        st = apply_card([r for r in records if r.source_doc == doc_name], routes, roles)
        for k in ("filled", "records_touched", "per_alloy", "paper_level",
                  "unmatched", "unverified"):
            total[k] += st[k]
    total["roles"] = sorted(roles)
    return total


def _doc_id_of(store, doc_name: str) -> str:
    for c in store.chunks:
        if c.doc_name == doc_name:
            return c.doc_id
    return ""
