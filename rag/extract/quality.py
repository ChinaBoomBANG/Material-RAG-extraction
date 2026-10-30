"""行级常识校验：把物理上不可能的行剔掉，把"可疑但可能真实"的行保留并标注。

为什么不能"屈服强度低于 200 MPa 就删"
----------------------------------------
实测本项目 979 条记录里，屈服强度 <200 MPa 的有 58 条（占 293 条有屈服强度记录的 20%）。
逐条看下来它们不是错抽，而是**低强度状态**：

    AA6016 自然时效 4 周   YS 57 / UTS 110
    6082 固溶 540 °C 欠时效 YS 64 / UTS 178 / EL 27
    6013 预时效态          YS 185

这些恰好是热处理数据集最需要的那部分（自然时效、欠时效、预时效）。
一律按 200 MPa 删掉，等于把"时效不足"的样本全部抹掉，只剩下 T6。

所以这里分两档：
  **hard（剔除）**：物理上不可能——UTS < YS、强度非正、延伸率超 0~60、
                   硬度与强度差一个数量级、温度/时间越界、成分合计 >105。
  **soft（保留 + 警告）**：可疑但真实存在——T6 状态却 YS 很低、延伸率极低配高强度、
                   YS/UTS 比值过小。写进 warnings，交人复核，不删数据。
"""

from __future__ import annotations

import re

# 状态 -> 屈服强度下限（MPa）。低于此值只提示，不删：判定依据可能只是行标签猜出来的。
YS_FLOOR_BY_STATE = {
    "t6": 150.0, "t651": 150.0, "t66": 150.0, "峰时效": 150.0, "peak": 150.0,
    "t4": 80.0, "t451": 80.0, "自然时效": 60.0, "t5": 90.0,
    "o": 30.0, "退火": 30.0, "annealed": 30.0, "as-quenched": 60.0, "淬火态": 60.0,
}
DEFAULT_YS_FLOOR = 30.0

# 硬度与抗拉强度的换算带：铝合金 UTS(MPa) ≈ 3.0~3.6 × HV
HV_UTS_LOW, HV_UTS_HIGH = 1.5, 7.5        # 超出即 hard（差一个数量级；宁可放宽也别误删）
HV_UTS_SOFT = (2.4, 4.6)                  # 超出只提示

_STATE_RE = re.compile(r"(T6\d*|T4\d*|T5\d*|T66|O态|退火|峰时效|过时效|自然时效|欠时效|"
                       r"peak[- ]?ag\w*|under[- ]?ag\w*|over[- ]?ag\w*|as[- ]?quench\w*|"
                       r"solution\w*|固溶态|淬火态)", re.I)


def _num(bucket: dict, name: str) -> float | None:
    fv = (bucket or {}).get(name)
    v = getattr(fv, "value", None)
    if v in (None, ""):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _state_of(rec) -> str:
    """从行标签/牌号/备注里认出热处理状态；认不出返回空串。"""
    for text in (getattr(rec, "row_label", "") or "", getattr(rec, "alloy", "") or "",
                 " ".join(getattr(rec, "warnings", []) or [])):
        m = _STATE_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _ys_floor(state: str, override: dict | None = None) -> float:
    table = dict(YS_FLOOR_BY_STATE)
    for k, v in (override or {}).items():
        try:
            table[str(k).strip().lower()] = float(v)
        except (TypeError, ValueError):
            continue
    s = (state or "").strip().lower()
    if not s:
        return DEFAULT_YS_FLOOR
    if s in table:
        return table[s]
    for k, v in table.items():
        if k and k in s:
            return v
    return DEFAULT_YS_FLOOR


def row_issues(rec, override: dict | None = None) -> list[dict]:
    """返回这一行的所有问题：{severity: hard|soft, code, msg}。"""
    out: list[dict] = []
    ys = _num(rec.properties, "YS")
    uts = _num(rec.properties, "UTS")
    el = _num(rec.properties, "elongation")
    hv = _num(rec.properties, "hardness_HV")
    hb = _num(rec.properties, "hardness_HB")

    # ---- hard：物理上不可能 ----
    if uts is not None and ys is not None and uts < ys:
        out.append({"severity": "hard", "code": "uts_lt_ys",
                    "msg": f"抗拉强度 {uts:g} < 屈服强度 {ys:g}，物理上不可能"})
    for name, v in (("屈服强度", ys), ("抗拉强度", uts)):
        if v is not None and v <= 0:
            out.append({"severity": "hard", "code": "nonpositive_strength",
                        "msg": f"{name} {v:g} ≤ 0"})
    if el is not None and not (0 < el <= 60):
        out.append({"severity": "hard", "code": "elongation_range",
                    "msg": f"延伸率 {el:g}% 超出 0~60"})
    if hv is not None and uts is not None:
        r = uts / hv if hv else 0
        if not (HV_UTS_LOW <= r <= HV_UTS_HIGH):
            out.append({"severity": "hard", "code": "hardness_strength_ratio",
                        "msg": f"UTS/HV = {r:.1f}，与铝合金经验值 3.0~3.6 差一个数量级"
                               f"（疑似把硬度与强度写串了）"})
    elif hb is not None and uts is not None:
        r = uts / hb if hb else 0
        if not (HV_UTS_LOW <= r <= HV_UTS_HIGH):
            out.append({"severity": "hard", "code": "hardness_strength_ratio",
                        "msg": f"UTS/HB = {r:.1f}，与经验值差一个数量级"})

    # ---- soft：可疑但可能真实，保留并提示 ----
    state = _state_of(rec)
    floor = _ys_floor(state, override)
    if ys is not None and ys < floor:
        out.append({"severity": "soft", "code": "ys_below_state_floor",
                    "msg": f"{state or '未知状态'} 的屈服强度只有 {ys:g} MPa"
                           f"（该状态通常 ≥{floor:g}），请确认是否欠时效/自然时效或抽错"})
    if uts is not None and ys is not None and uts > 0:
        ratio = ys / uts
        if ratio < 0.35:
            out.append({"severity": "soft", "code": "ys_uts_ratio",
                        "msg": f"屈服/抗拉 = {ratio:.2f} 偏低（常见 0.4~0.95），"
                               f"除非是退火态，否则请复核"})
    if hv is not None and uts is not None and hv:
        r = uts / hv
        if not (HV_UTS_SOFT[0] <= r <= HV_UTS_SOFT[1]):
            out.append({"severity": "soft", "code": "hardness_strength_ratio_soft",
                        "msg": f"UTS/HV = {r:.1f} 偏离经验带 2.4~4.6，建议复核"})
    if el is not None and uts is not None and el < 3 and uts > 450:
        out.append({"severity": "soft", "code": "brittle_high_strength",
                    "msg": f"延伸率 {el:g}% 配抗拉 {uts:g} MPa，塑性与强度组合异常"})
    return out


def filter_rows(rows: list, conf: dict | None = None) -> tuple[list, list]:
    """按 hard 问题剔除整行，soft 问题写进 warnings 保留。返回 (保留, 剔除明细)。"""
    rc = (((conf or {}).get("extract") or {}).get("row_checks") or {})
    if not rc.get("enabled", True):
        return rows, []
    kept, dropped = [], []
    for r in rows:
        issues = row_issues(r, rc.get("state_ys_floor") or {})
        hard = [i for i in issues if i["severity"] == "hard"]
        soft = [i for i in issues if i["severity"] == "soft"]
        for i in soft:
            r.warnings.append(f"常识复核：{i['msg']}")
        if hard:
            dropped.append({"doc": getattr(r, "source_doc", ""),
                            "row_label": getattr(r, "alloy", "") or getattr(r, "row_label", ""),
                            "reason": "；".join(i["msg"] for i in hard)})
            continue
        kept.append(r)
    return kept, dropped
