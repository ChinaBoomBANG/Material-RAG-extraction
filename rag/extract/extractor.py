"""结构化抽取引擎：文献 -> 成分/工艺/组织/性能记录。

与"LLM 直接吐数据"的做法有三点本质区别：

1. **转录而非生成**。提示词要求模型把原文的数值表示原样抄写（raw）并附上
   证据片段（quote），模型不做任何算术。
2. **证据强制回链**。每条字段都要在送入的文献片段里逐字命中，命中失败的
   字段被标记 unverified，不会被当作可信数据。
3. **缺失即空**。原文未提及的字段根本不输出，不存在"模型觉得应该是"的值。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from ..llm.client import LLMClient
from ..models import Chunk, Evidence, FieldValue, MaterialRecord
from .align import alignment_issue
from .normalize import (DEFAULT_MAX_WT, ELEMENT_MAX_WT, canonical_unit,
                        composition_sum, parse_value, sanity_issue,
                        verify_quote, verify_raw)
from .schema import ALLOY_SPEC, BY_KEY, FieldSpec, schema_brief

# 行标签黑名单：这些行是试验网格 / 统计值 / 参数名，不对应任何具体材料，
# 按用户要求一律不生成记录（原文有、但抽出来会污染数据集）。
_BAD_LABEL_RE = re.compile(
    r"^(?:steps|step|ranges|range|limits|limit|parameters|parameter|items|item|units|unit|"
    r"min|max|mean|std|median|average|avg|total|sum|name|"
    r"步长|范围|限值|上限|下限|参数|项目|名称|单位)(?![a-z])", re.I)
# note 里出现"取 Min/Max 列"这类说明，说明这条记录的数值来自统计列而非某个样品
_STAT_NOTE_RE = re.compile(r"取\s*(min|max|mean|std|median|最小|最大|平均|标准|中位)", re.I)
# 元素符号：若行标签是元素名（成分表按元素逐行列出），不能当作合金牌号
_ELEMENTS = {k.split(".", 1)[1].lower() for k in BY_KEY if k.startswith("composition.")}


def _norm_label(s: str) -> str:
    """去掉括号单位与标点，便于匹配行标签。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    return s.replace("%", " ").strip(" .:：-—–").lower()


def _is_bad_label(label: str) -> bool:
    return bool(_BAD_LABEL_RE.match(_norm_label(label)))


# 表题只写了"预测性能"的表（如 "Predicted performance and the corresponding
# composition"）：其中的性能值不是实测值，必须落到 *_pred 字段，否则会把
# 模型预测当成实验数据写进数据集。若有 measured/experimental/actual 字样，
# 说明表里两种情况并列，不能整体改写。
_PRED_HINT = re.compile(r"predict|预测", re.I)
_MEAS_HINT = re.compile(r"measur|experiment|actual|observ|实测|试验|实验", re.I)
_PROP_TO_PRED = {
    "properties.UTS": "properties.UTS_pred",
    "properties.YS": "properties.YS_pred",
    "properties.elongation": "properties.elongation_pred",
    "properties.hardness_HB": "properties.hardness_HB_pred",
    "properties.conductivity": "properties.EC_pred",
}


def _pred_only_caption(text: str) -> bool:
    first = (text or "").splitlines()[0] if text else ""
    cap = first[len("[表格]"):].strip() if first.startswith("[表格]") else ""
    return bool(_PRED_HINT.search(cap)) and not bool(_MEAS_HINT.search(cap))

SYSTEM_PROMPT = """你是一名材料科学文献数据抄录员，唯一的工作是把论文原文中的数据**原样转录**出来。

你必须遵守以下铁律：
1. 只转录原文明确写出的内容。原文没有的字段，一个字都不要编。
2. 每条数据都要给出 quote —— 从原文中**逐字复制**的连续片段（可含标点、单位、数值）。
   quote 必须能在原文中精确找到，不得改写、不得拼接、不得翻译。
3. 你**不做任何计算与换算**。原文写 "0.55"，就抄 "0.55"；原文写 "0.5~0.6"，
   就抄 "0.5~0.6"；原文写 "≥300"，就抄 "≥300"。不要取中值、不要换成别的单位。
4. 一段文字里如果出现多组相互独立的数据（例如表格的每一行是一组合金），
   就输出多条记录，不要合并。
5. 表格里同一行的不同列属于**不同字段**，禁止把两列拼成一个值或区间。
   例如表头是 Min / Max 两列，只能取其中一列的原值，绝不能写成 "4.25-17.4"；
   取了哪一列要写进 note（如 "取 Min 列"）。
6. 只为**具体样品/合金/处理状态**生成记录。下面这些行不是样品，直接跳过，
   不要为它们生成任何记录：行标签为 Step / Steps / Range / Ranges / Limit / Limits /
   Min / Max / Mean / Std / Median / Average / Parameter / 参数 / 单位 之类的
   （这些是试验网格、统计值或参数名，不对应任何具体材料）。
7. 表格同时给出**预测值与实测值**时，两者都要保留：预测值填入对应的 *_pred 字段
   （如 properties.UTS_pred），实测值填入实测字段（如 properties.UTS）。
   绝不能只取其中一列。若表里只有预测值，就只填 *_pred。
8. row_label 必须逐字抄写原文中该行的标识（如 A1、D3、6013、AMSY-Exp）；
   该行没有标识就留空字符串。若表题写明了合金名（如 "... of A2 alloy"），
   把它作为该表所有记录的 row_label。
9. 只输出 JSON，不要任何解释文字、不要 Markdown 代码块围栏。
10. **按位置对齐**（成分字段尤其严格）：先找到字段对应的**列**（表头里
    元素符号/中文名所在列），再取该列与当前行交叉的那个单元格。
    字段名、数值、单位必须来自**同一个单元格或同一列的表头**。
    - 表里**没有**该元素的列（也没有把它写在行首）→ 该字段留空，不要填；
    - 单元格是 "–" / "n/a" / 空白 → 留空；
    - 只有整行都对应不上、或看不出哪一列是这个字段时，才允许省略该字段，
      并在 note 里写「未识别：<字段名>」。
11. **单位以列为单位**：同一列的单位取该列表头括号里的单位（如 "Si (wt.%)"）；
    表头没写单位、正文也没说明时 unit 留空，**不要从相邻列或别的量纲抄**
    （kJ/mol、at.%、ppm 都不是 wt.%）。
12. **不要跨量纲搬运**：数值必须带它自己的量纲含义。若某张表的数值量纲明显
    不是质量分数（自由能 kJ/mol、原子比 at.%、温度 °C、强度 MPa），
    这些数**不能**填进成分字段。
13. 字段清单之外的成分列，统一归入 RE 字段（若清单中有 RE 且类型为 text）：
    表格里出现了清单中没有的元素列（如 Be、Ca、Sr、Ce 等不在清单里的元素）时，
    不要忽略，按「元素=数值」合并写进 RE，多个用英文分号分隔，
    如 "Be=0.05; Sr=0.01; Ce=0.2"；quote 取这些单元格所在行的原文片段。
    清单里没有 RE 字段、或表格没有清单外的成分列时，忽略本条，RE 留空。
14. 标注「参考上下文·仅供理解·不得从中抽取」的文字**不是抽取对象**：
    它只用来帮你判断表格里的单位、测试条件、样品对应关系（例如哪一组是预测值、
    时效温度是多少）。你**不得**从参考上下文里取任何数值填进字段——
    所有数据只能来自上方带 [序号] 的目标块。"""

USER_TEMPLATE = """【本次要抽取的字段清单】
{schema}

【文献原文片段】
{context}

【输出格式】
{{
  "records": [
    {{
      "row_label": "<该行的标识，逐字抄写，如 A1 / D3 / 6013 / AMSY-Exp；没有就留空>",
      "fields": {{
        "<字段key>": {{"raw": "<原文中的数值或表述，原样抄写>",
                        "quote": "<在该片段中逐字复制的证据片段>"}}
      }},
      "note": "<可选：这条记录的特殊条件，如测试温度、状态代号；没有就留空>"
    }}
  ]
}}

【补充要求】
- fields 里只放原文确实出现了的字段；未出现的字段直接省略该键。
- 一条 record 代表一组相互对应的数据（同一合金/同一处理状态）。
- 若原文片段中不含任何上述字段的数据，返回 {{"records": []}}。
- 字段 key 必须与上面的清单完全一致。"""


class Extractor:
    def __init__(self, llm: LLMClient, conf: dict):
        self.llm = llm
        self.conf = conf
        self.dropped: list[dict] = []      # 被过滤掉的记录及原因，供报告使用
        self.stopped_early = False         # 本次是否因「停止」提前收工（供上层报告）

    # ---------------------------------------------------------- 主入口

    def extract(self, chunks: list[Chunk], specs: dict[str, FieldSpec],
                workers: int = 1, on_event=None,
                should_stop=None,
                context_of=None) -> list[MaterialRecord]:
        """对一批 chunk 抽取，返回去重后的记录。

        ``on_event``：每完成一个批次回调一次 {"done","total","records","elapsed"}，
        供界面做实时进度（SSE）。不传则完全无感。
        ``should_stop``：返回 True 时提前收工，**已完成批次的记录照常返回**，
        剩下的块直接放弃（界面点「停止」用，避免干等几十分钟）。
        ``context_of``：``callable(chunk) -> str``，给表格块补充「所在页的正文」
        作为参考上下文（只用于理解单位/条件，不从中抽数据）。不改 chunk 本身，
        所以索引与召回完全不受影响。
        """
        import time as _time

        specs = dict(specs)
        specs.setdefault("alloy", ALLOY_SPEC)      # 每条记录都需要标识合金
        self.dropped = []
        batch = int(self.conf["extract"].get("batch_chunks", 6))
        batches = [chunks[i:i + batch] for i in range(0, len(chunks), batch)]
        if not batches:
            return []

        t0 = _time.time()
        total = len(batches)

        def run(b: list[Chunk]) -> list[MaterialRecord]:
            return self._extract_batch(b, specs, context_of=context_of)

        def emit(done: int, got: int, ids: list[str] | None = None) -> None:
            if not on_event:
                return
            try:
                on_event({"done": done, "total": total, "records": got,
                          "elapsed": round(_time.time() - t0, 1),
                          "chunk_ids": list(ids or [])})
            except Exception:
                pass          # 进度回调绝不能影响抽取本身

        records: list[MaterialRecord] = []
        done = 0
        stopped = False
        if workers > 1 and total > 1:
            from concurrent.futures import as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                fut2batch = {ex.submit(run, b): b for b in batches}
                futs = list(fut2batch)
                for fut in as_completed(futs):
                    records.extend(fut.result())
                    done += 1
                    emit(done, len(records),
                         [c.chunk_id for c in fut2batch[fut]])
                    if should_stop and should_stop():
                        stopped = True
                        for f in futs:      # 只取消还没开跑的，已在跑的让它收尾
                            f.cancel()
                        break
        else:
            for i, b in enumerate(batches, 1):
                if should_stop and should_stop():
                    stopped = True
                    break
                records.extend(run(b))
                done = i
                emit(i, len(records), [c.chunk_id for c in b])

        if stopped:
            emit(done, len(records))
            if on_event:
                try:
                    on_event({"done": done, "total": total, "records": len(records),
                              "elapsed": round(_time.time() - t0, 1), "stopped": True})
                except Exception:
                    pass
        self.stopped_early = stopped
        return _dedupe(records)

    # ---------------------------------------------------------- 单批

    def _extract_batch(self, chunks: list[Chunk], specs: dict[str, FieldSpec],
                       context_of=None) -> list[MaterialRecord]:
        context = "\n\n".join(
            f"[{i}] （{c.doc_name} 第{c.page}页 · {c.kind}）\n{c.text}"
            for i, c in enumerate(chunks, 1)
        )
        # 表格块附上「所在页正文」作参考：表格常把工艺条件、单位、样品说明写在
        # 正文里，表格单独成块后模型看不到，会把预测值当实测值、或让工艺字段空缺。
        # 明确标注"仅供理解"——铁律第 11 条禁止从这里抽值。
        if context_of is not None:
            refs = []
            for c in chunks:
                try:
                    txt = context_of(c) or ""
                except Exception:
                    txt = ""
                if txt.strip():
                    refs.append(
                        f"【参考上下文·仅供理解·不得从中抽取】"
                        f"（{c.doc_name} 第{c.page}页正文）\n{txt.strip()}")
            if refs:
                context = context + "\n\n" + "\n\n".join(refs)
        user = USER_TEMPLATE.format(schema=schema_brief(specs), context=context)
        try:
            rf = {"type": "json_object"} if self.conf["extract"].get(
                "json_object_mode", True) else None
            obj = self.llm.chat_json(user, system=SYSTEM_PROMPT, response_format=rf)
        except Exception as e:
            return [MaterialRecord(
                record_id="",
                source_doc=chunks[0].doc_name,
                source_page=chunks[0].page,
                warnings=[f"本批抽取失败：{e}"],
            )]
        return self._to_records(obj, chunks, specs)

    # ---------------------------------------------------------- 组装 + 校验

    def _to_records(self, obj: dict, chunks: list[Chunk],
                    specs: dict[str, FieldSpec]) -> list[MaterialRecord]:
        raw_records = obj.get("records") or []
        if not isinstance(raw_records, list):
            return []
        texts = [c.text for c in chunks]
        verify = bool(self.conf["extract"].get("verify_quotes", True))
        max_ev = int(self.conf["extract"].get("max_evidence_chars", 400))
        out: list[MaterialRecord] = []
        pred_only = _pred_only_caption(texts[0] if texts else "")

        # ---- 对齐 / 合理性校验的开关（都可在 config.yaml 的 extract 段调整）----
        align_conf = self.conf["extract"].get("alignment") or {}
        align_on = bool(align_conf.get("enabled", True)) and verify
        align_groups = set(align_conf.get("groups") or ["composition"])
        unconfirmed = str(align_conf.get("on_unconfirmed", "blank") or "blank")
        san = self.conf["extract"].get("sanity") or {}
        san_on = bool(san.get("enabled", True))
        on_violation = str(san.get("on_violation", "drop") or "drop")
        drop_row_if = int(san.get("drop_row_if_violations", 3) or 0)
        sum_max = float(san.get("sum_max_wt", 105) or 105)
        on_sum = str(san.get("on_sum_violation", "drop_row") or "drop_row")
        limits = dict(ELEMENT_MAX_WT)
        for k, v in (san.get("element_max_wt") or {}).items():
            try:
                limits[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
        default_max = float(san.get("default_max_wt", DEFAULT_MAX_WT) or DEFAULT_MAX_WT)

        for i, rr in enumerate(raw_records):
            if not isinstance(rr, dict):
                continue
            fields = rr.get("fields") or {}
            row_label = str(rr.get("row_label") or "").strip()[:60]
            rec = MaterialRecord(record_id=f"{chunks[0].doc_id}-p{chunks[0].page}-{i + 1}",
                                 source_doc=chunks[0].doc_name,
                                 source_page=chunks[0].page,
                                 row_label=row_label)
            violations = 0          # 本条记录里越界的成分字段数（用于整条丢弃）
            if pred_only:
                rec.warnings.append("该表表题为预测性能，性能值记入 *_pred 字段（非实测）")
            for key, val in fields.items():
                if not isinstance(val, dict):
                    continue
                if pred_only and key in _PROP_TO_PRED:
                    key = _PROP_TO_PRED[key]
                raw = (val.get("raw") if val.get("raw") is not None else val.get("value"))
                quote = str(val.get("quote", "") or "").strip()[:max_ev]
                if raw is None and not quote:
                    continue

                # ---- 证据回链 ----
                ev = Evidence(quote=quote, doc_name=chunks[0].doc_name)
                hit = verify_quote(quote, texts) if quote else -1
                if hit >= 0:
                    c = chunks[hit]
                    ev.verified = True
                    ev.chunk_id = c.chunk_id
                    ev.page = c.page
                else:
                    ev.verified = False
                    ev.page = chunks[0].page
                    if verify:
                        rec.warnings.append(f"字段 {key} 的引文未能在原文中定位，已标记为不可信")

                # ---- 数值解析（确定性代码，不经模型）----
                spec = specs.get(key)
                kind = spec.kind if spec else "number"
                parsed = parse_value(raw, kind)
                # 单位以"字段声明"为准（schema 就是契约）；模型给的单位不一致只
                # 记警告、不做换算。实测坑：延伸率表头是 "El (%)"，模型却照抄了
                # 相邻成分列的 "wt.%"，静默写进表格就等于污染数据集。
                declared = canonical_unit(spec.unit) if (spec and spec.unit) else ""
                model_unit = canonical_unit(val.get("unit", "") or "")
                if declared:
                    if model_unit and model_unit != declared:
                        rec.warnings.append(
                            f"字段 {key} 单位不符：模型给出 {model_unit}，字段声明 "
                            f"{declared}（按声明单位记录，数值未换算）")
                    unit = declared
                else:
                    unit = model_unit

                # ---- raw 逐字校验 ----
                # quote 命中只说明"引文来自原文"，不说明 raw 没被改写。典型错误：
                # 表格里 Min / Max 是两列，模型把它们拼成区间 "4.25-17.4"，
                # 而原文并不存在这个区间（quote 却能逐字命中 "4.25 | 17.4"）。
                # 所以数值型字段的 raw 本身也必须能在原文中逐字找到。
                if (verify and ev.verified and parsed["kind"] != "text"
                        and raw is not None and not verify_raw(str(raw), texts)):
                    ev.verified = False
                    rec.warnings.append(
                        f"字段 {key} 的 raw={raw!r} 未能在原文中逐字找到"
                        f"（疑似拼接/改写，已标记为不可信）")

                # ---- 字段-数值对齐 ----
                # 引文能在原文找到 ≠ 这个数属于这个字段。表头里根本没有 Si 这列时，
                # 模型照样能填出 Si=93.5（源头是另一张 kJ/mol 的表）。
                # 确认不了对应关系就留空并标注「未识别」——绝不猜一个填上。
                if (align_on and key != "alloy"
                        and (key.split(".", 1)[0] if "." in key else "properties") in align_groups):
                    src = chunks[hit] if hit >= 0 else chunks[0]
                    reason = alignment_issue(spec, quote, src.text or "",
                                             getattr(src, "kind", "text"))
                    if reason:
                        if unconfirmed == "keep":
                            rec.warnings.append(f"未识别：{reason}（保留原值，请人工复核）")
                        else:
                            rec.warnings.append(f"未识别：{reason}（已留空，未猜测填充）")
                            continue

                if key == "alloy":
                    rec.alloy = str(raw)          # 合金牌号单独存放，不入数据分组
                    continue

                fv = FieldValue(value=parsed.get("value") if parsed["kind"] in ("single", "text")
                                else parsed.get("raw"),
                                unit=unit, evidence=ev, parsed=parsed)

                # ---- 物理合理性校验 ----
                # 证据校验只能证明引文来自原文，证明不了它说的是这个字段。
                # 铝合金成分表普遍不列 Al（余量），模型却会抓个数字填上，
                # 引文校验照样通过——这层常识校验专门抓这种情况。
                sv = parsed.get("value") if parsed["kind"] == "single" else parsed.get("raw")
                issue = sanity_issue(key, sv, unit, limits, default_max) if san_on else ""
                if issue:
                    # 明显不符合常理的值不可能是真实数据，几乎都是"表不对题"的错位抽取。
                    # 实测：文献 Table 2 是 ΔGseg（晶界偏聚自由能，kJ/mol，含 −9.127），
                    # 列名带 of Mg / of Si / of Cu，模型就把它塞进成分字段，引文还能逐字命中；
                    # 另一类是把 90 多的数当成 Si/Mg 的质量分数。
                    # 质量分数为负、超过 100、或超出该元素常见上限 —— 这类值按配置直接删掉。
                    hard = (canonical_unit(unit) == "wt.%" and parsed["kind"] == "single"
                            and isinstance(sv, (int, float)))
                    if hard and on_violation == "drop":
                        rec.warnings.append(
                            f"常识校验：{issue}（已删除该字段，未写入记录）")
                        violations += 1
                        continue
                    rec.warnings.append(f"异常：{issue}（已保留，请人工复核）")

                target = self._bucket(rec, key)
                target[key.split(".", 1)[1] if "." in key else key] = fv

            note = str(rr.get("note") or "")

            # ---- 行标签即牌号：原文怎么写就怎么记（A1/D3/6013），
            #      但元素符号（成分表按元素逐行列）不能当牌号 ----
            if not rec.alloy and row_label and not _is_bad_label(row_label) \
                    and _norm_label(row_label) not in _ELEMENTS:
                rec.alloy = row_label

            # ---- 过滤：不对应具体样品的行 ----
            reason = ""
            if _is_bad_label(row_label):
                reason = f"行标签为网格/统计行（{row_label}）"
            elif _STAT_NOTE_RE.search(note):
                reason = f"取自统计列（{note[:30]}）"
            elif not rec.alloy and not rec.composition:
                reason = "无法绑定到具体样品（无牌号、无成分）"
            if reason:
                self.dropped.append({
                    "doc": chunks[0].doc_name, "page": chunks[0].page,
                    "row_label": row_label, "note": note[:40], "reason": reason,
                })
                continue

            # ---- 合理性兜底：越界太多 / 成分合计远超 100，整条都不可信 ----
            if drop_row_if and violations >= drop_row_if:
                self.dropped.append({
                    "doc": chunks[0].doc_name, "page": chunks[0].page,
                    "row_label": row_label, "note": note[:40],
                    "reason": f"成分越界字段达 {violations} 个，整条记录不可信（已删除）",
                })
                continue

            total = composition_sum(rec.composition)
            if total is not None and total > sum_max:
                if on_sum == "drop_row":
                    self.dropped.append({
                        "doc": chunks[0].doc_name, "page": chunks[0].page,
                        "row_label": row_label, "note": note[:40],
                        "reason": f"成分合计 {total:.1f} wt.% 超过上限 {sum_max:g}"
                                  f"（疑似列错位或量纲错误，整条已删除）",
                    })
                    continue
                rec.warnings.append(
                    f"异常：成分合计 {total:.1f} wt.% 超过 {sum_max:g}"
                    f"（疑似列错位或把非成分数值当成了成分，已保留待复核）")

            if note:
                rec.warnings.append(f"条件说明：{note}")
            if any(rec.composition) or any(rec.process) or any(rec.microstructure) or any(rec.properties):
                out.append(rec)
            elif violations:
                # 字段被越界/未识别规则删光后，这条记录会静默消失。
                # 必须落到 dropped 里说明原因，否则"明明抽到了却一条都没有"无从查起。
                self.dropped.append({
                    "doc": chunks[0].doc_name, "page": chunks[0].page,
                    "row_label": row_label, "note": note[:40],
                    "reason": f"{violations} 个字段因越界被删除，该行已无有效数据（整条丢弃）",
                })
        return out

    @staticmethod
    def _bucket(rec: MaterialRecord, key: str) -> dict:
        """把字段 key 归入记录的分组。未知分组落到 properties。"""
        group = key.split(".", 1)[0] if "." in key else "properties"
        return {
            "composition": rec.composition,
            "process": rec.process,
            "microstructure": rec.microstructure,
            "properties": rec.properties,
        }.get(group, rec.properties)


# ---------------------------------------------------------------- 去重

def _fingerprint(rec: MaterialRecord) -> str:
    """记录指纹：用于识别「同一块被重复抽取」产生的完全重复记录。"""
    parts = [rec.alloy or ""]
    for group, name, fv in rec.all_fields():
        if fv.evidence and fv.evidence.verified:
            parts.append(f"{name}={fv.value if fv.value is not None else ''}")
    return "|".join(sorted(parts))


def _identity(rec: MaterialRecord) -> tuple[str, str]:
    """同一样品的身份键：文献 + 合金牌号。

    为什么不用「所有字段值」做键（旧实现）：同一样品的成分往往在一张表、
    性能在另一张表，两条记录的字段集合不同 → 指纹不同 → 永远合不上，
    结果是一条记录只有成分、另一条只有性能。改成按身份键合并才能拼齐。
    """
    return (rec.source_doc or "", (rec.alloy or "").strip())


def _merge_into(dst: MaterialRecord, src: MaterialRecord) -> bool:
    """把 src 的字段补进 dst：无冲突返回 True，有冲突（同名字段值不同）返回 False。

    冲突意味着这不是同一批数据（例如同牌号在不同时效工艺下各有一条性能），
    此时必须保留为两条独立记录，绝不能覆盖或拼接。
    """
    for group, name, fv in src.all_fields():
        tgt = Extractor._bucket(dst, f"{group}.{name}")
        if name in tgt:
            a = tgt[name].value
            b = fv.value
            if a is not None and b is not None and str(a).strip() != str(b).strip():
                return False
    for group, name, fv in src.all_fields():
        tgt = Extractor._bucket(dst, f"{group}.{name}")
        if name not in tgt:
            tgt[name] = fv
    # 合并时把双方的警告都留着，避免丢掉「常识校验」等提示
    for w in (src.warnings or []):
        if w not in (dst.warnings or []):
            dst.warnings.append(w)
    return True


def _dedupe(records: list[MaterialRecord]) -> list[MaterialRecord]:
    """合并同一样品分散在不同块里的记录，同时保留完全重复的去除。

    规则：
      1. 无牌号的记录无法确定身份，原样保留，不参与合并；
      2. 同 (文献, 牌号) 且字段无冲突 → 合并成一条（成分表 + 性能表拼齐）；
      3. 同 (文献, 牌号) 但同名字段值冲突 → 视为不同条件的数据，各自保留；
      4. 指纹完全相同 → 视为重复抽取，丢弃后出现的那条。
    """
    out: list[MaterialRecord] = []
    by_identity: dict[tuple[str, str], list[int]] = {}
    seen_fp: set[str] = set()

    for r in records:
        fp = _fingerprint(r)
        if fp in seen_fp:
            continue                      # 完全重复
        doc, alloy = _identity(r)
        if alloy:
            for idx in by_identity.get((doc, alloy), []):
                if _merge_into(out[idx], r):
                    seen_fp.add(fp)       # 已并入，后续同样的也不要了
                    break
            else:
                by_identity.setdefault((doc, alloy), []).append(len(out))
                out.append(r)
                seen_fp.add(fp)
        else:
            out.append(r)
            seen_fp.add(fp)
    return out


def records_to_json(records: list[MaterialRecord]) -> str:
    return json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2)
