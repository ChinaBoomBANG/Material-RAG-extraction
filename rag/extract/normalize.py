"""数值与单位的规范化，以及原文证据校验。

分工原则（这是"丝毫不差"的关键）：
    LLM 只负责**定位与转录**——把原文里的数值表示原样抄下来并给出证据片段；
    数值解析、区间识别、单位换算**全部由本模块的确定性代码完成**。
模型不做算术，错误就不会从计算环节引入。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- 文本归一

def norm_text(s: str) -> str:
    """全半角统一、去所有空白，用于证据比对。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("−", "-").replace("–", "-").replace("—", "-").replace("~", "~")
    return re.sub(r"\s+", "", s)


def verify_raw(raw: str, candidates: list[str]) -> bool:
    """校验 raw 本身是否**逐字**出现在原文中。

    quote 命中只证明引文来自原文，不证明 raw 没被改写。典型错误：表格里
    Min / Max 是两列，模型把它们拼成区间 "4.25-17.4"，而原文并不存在这个
    区间（quote 却能逐字命中 "4.25 | 17.4"）。数值型字段因此要额外校验 raw。
    """
    nr = norm_text(raw)
    if not nr:
        return False
    return any(nr in norm_text(c) for c in candidates)


def verify_quote(quote: str, candidates: list[str]) -> int:
    """在候选文本中查找引文，返回命中的候选下标；未命中返回 -1。

    允许引文与原文之间存在空白、全半角差异；不允许改写。
    """
    nq = norm_text(quote)
    if not nq:
        return -1
    for i, text in enumerate(candidates):
        if nq in norm_text(text):
            return i
    # 放宽一级：引文较长时允许首尾截断差异（模型偶尔多抄一个标点）
    if len(nq) > 12:
        core = nq[1:-1]
        for i, text in enumerate(candidates):
            if core and core in norm_text(text):
                return i
    return -1


# ---------------------------------------------------------------- 数值解析

_NUM = r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
_RANGE_SEP = r"(?:~|～|—|–|-|至|到)"
# "380 ± 2" 是"均值±标准差"，不是区间 2~380。必须先于区间判定，
# 否则会把低值当成 2、高值当成 380，写进数据集就是错的。
_PM_RE = re.compile(rf"^\s*({_NUM})\s*±\s*({_NUM})\s*$")
_RANGE_RE = re.compile(rf"^\s*(?:约|about|approx\.?|~)?\s*({_NUM})\s*{_RANGE_SEP}\s*({_NUM})\s*$", re.I)
_BOUND_RE = re.compile(
    rf"^\s*(≤|≥|<=|>=|<|>|不超过|不低于|至少|最多|以上|以下|max\.?|min\.?|"
    rf"up to|at least|no more than)\s*({_NUM})", re.I)
_SINGLE_RE = re.compile(rf"^\s*(?:约|about|approx\.?|~)?\s*({_NUM})\s*$", re.I)
_LEAD_NUM_RE = re.compile(rf"({_NUM})")

_UPPER_WORDS = {"≤", "<=", "<", "不超过", "最多", "以下", "max", "max.", "up to", "no more than"}
_LOWER_WORDS = {"≥", ">=", ">", "不低于", "至少", "以上", "min", "min.", "at least"}


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parse_value(raw: str, kind: str = "number") -> dict:
    """把原始数值表示解析为结构化结果。

    返回 {"kind": single|range|text|missing, "value": float|None,
          "low": float|None, "high": float|None, "raw": str}
    区间值**不取中值**，因为中值不是原文数据，会污染数据集。
    """
    out = {"kind": "missing", "value": None, "low": None, "high": None, "raw": raw}
    if raw is None:
        return out
    raw = str(raw).strip()
    if not raw or raw.lower() in {"none", "null", "n/a", "na", "-", "—", "未报道", "未提及"}:
        return out

    if kind == "text":
        out["kind"], out["value"] = "text", raw
        return out

    cleaned = raw.strip()

    # 0) 均值±标准差："380 ± 2"、"8.52 ± 0.91"。保留原文写法，均值/偏差单列。
    m = _PM_RE.match(cleaned)
    if m:
        mu, sd = _to_float(m.group(1)), _to_float(m.group(2))
        if mu is not None and sd is not None:
            out.update(kind="mean_pm", value=None, low=None, high=None)
            out["mean"], out["pm"] = mu, abs(sd)
            return out

    # 1) 显式数值区间："0.5~0.6"、"0.5-0.6"、"10 至 20"
    m = _RANGE_RE.match(cleaned)
    if m:
        lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
        if lo is not None and hi is not None:
            out.update(kind="range", low=min(lo, hi), high=max(lo, hi), value=None)
            return out

    # 2) 上界/下界："≤0.05"、"≥300"、"不超过 0.2"、"at least 250"
    #    必须在单值之前判断，否则比较符会被忽略、把界限值当成实测值。
    m = _BOUND_RE.match(cleaned)
    if m:
        v = _to_float(m.group(2))
        if v is not None:
            token = m.group(1).strip().lower()
            if token in _UPPER_WORDS:
                out.update(kind="upper_bound", high=v, value=None)
            elif token in _LOWER_WORDS:
                out.update(kind="lower_bound", low=v, value=None)
            else:
                out.update(kind="single", value=v)
            return out

    # 3) 单值
    m = _SINGLE_RE.match(cleaned)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            out.update(kind="single", value=v)
            return out

    # 4) 兜底：取串首数字
    m = _LEAD_NUM_RE.search(cleaned)
    if m:
        v = _to_float(m.group(1))
        if v is not None:
            out.update(kind="single", value=v)
            return out

    out.update(kind="text", value=raw)     # 形如 "A级"、"β''相"、"余量"
    return out


# ---------------------------------------------------------------- 单位处理

UNIT_ALIASES = {
    # 注意：裸 "%" 不能映射成 wt.%。"%" 是延伸率/导电率的百分数单位，
    # 早先的映射把延伸率静默写成了质量分数（实测踩过），这里不再归一到 wt.%。
    "wt%": "wt.%", "wt.%": "wt.%", "mass%": "wt.%", "质量分数": "wt.%",
    "at%": "at.%", "at.%": "at.%", "原子分数": "at.%",
    "mpa": "MPa", "gpa": "GPa", "hv": "HV", "hb": "HB",
    "μm": "μm", "um": "μm", "微米": "μm", "nm": "nm",
    "℃": "°C", "°c": "°C", "摄氏度": "°C",
    "h": "h", "hr": "h", "min": "min", "s": "s",
    "%iacs": "%IACS", "iacs": "%IACS",
    "g/cm3": "g/cm³", "g/cm^3": "g/cm³",
}


# ---------------------------------------------------------------- 物理合理性校验
# 背景：证据校验（quote / raw 逐字命中）只能证明"这段文字来自原文"，
# 证明不了"这段文字说的就是这个字段"。实测踩到的坑：铝合金文献的成分表普遍
# **不列 Al**（Al 是余量，领域惯例），但模板里有 Al 字段，模型于是从正文或
# 表格别处抓一个数字填上（如 Al=20 wt.%），引文逐字校验照样通过。
# 所以补一层常识校验：值能填进来，但必须打上警告让人一眼看见。
# 只警告、绝不自动改值——数据保真优先。

# 基体元素的合理下限（wt.%）：低于此值几乎可以断定是抽错位或单位错误
MATRIX_MIN_WT = {
    "Al": 50.0,      # 铝合金基体通常 85~99 wt.%
}


# 铝合金中各元素的质量分数上限（wt.%）。超过就几乎不可能是真实成分：
# 要么是列错位/单位错（kJ/mol、at.%、ppm 当成 wt.%），要么根本不是这张表的数。
# 取值口径：按常见变形铝合金/铸造铝合金的**实际使用上限**再放宽一档，
# 例如 Si 过共晶铝硅合金最高约 25，Mg 在 Al-Mg 系最高约 12，Fe/Mn 是杂质级。
ELEMENT_MAX_WT: dict[str, float] = {
    "Si": 25.0, "Mg": 12.0, "Cu": 10.0, "Zn": 12.0, "Fe": 2.0, "Mn": 2.0,
    "Cr": 0.6, "Ti": 0.4, "Zr": 0.5, "Ni": 4.0, "Sc": 1.0, "Li": 4.0,
    "V": 0.5, "Ga": 1.0, "Ca": 1.0, "Sr": 0.5, "Na": 0.1, "B": 0.5,
    "Be": 0.5, "RE": 2.0, "Ce": 2.0, "La": 2.0, "Nd": 2.0, "Y": 2.0,
    "Er": 1.0, "Ag": 1.0, "Sn": 1.0, "Pb": 1.0, "Bi": 1.0, "Co": 2.0,
    "Mo": 1.0, "W": 1.0, "Nb": 0.5, "Ta": 0.5, "Cd": 1.0, "In": 1.0,
    "Sb": 1.0, "Ge": 1.0, "P": 0.5, "S": 0.5, "C": 1.0, "N": 1.0,
    "O": 1.0, "H": 0.1,
}
DEFAULT_MAX_WT = 30.0        # 表里没列到的元素：铝合金里 >30 wt.% 极不寻常


def sanity_issue(key: str, value, unit: str,
                 limits: dict[str, float] | None = None,
                 default_max: float = DEFAULT_MAX_WT) -> str:
    """对单个字段值做常识校验，返回问题说明；没问题返回空串。

    覆盖三类：
      1) 成分 wt.% 超出 0~100；
      2) 基体元素（如 Al）低于合理下限；
      3) 元素质量分数超出该类合金常见上限（抓列错位/单位混淆：
         实测 Si、Mg 被抽成 90 多，源头是 kJ/mol 的表被当成 wt.% 的成分表）。

    limits 缺省用内置 ELEMENT_MAX_WT，可用 config 的 extract.sanity.element_max_wt
    覆盖（不同合金体系区间不同，留了口子）。
    """
    if value is None:
        return ""
    if canonical_unit(unit) != "wt.%":
        return ""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if v < 0 or v > 100:
        return f"{key}={value} wt.% 超出 0~100，成分占比不可能为此值"
    bare = key.split(".", 1)[1] if "." in key else key
    low = MATRIX_MIN_WT.get(bare)
    if low is not None:
        # 基体元素（Al）：只检查下限，不做上限——它本就该是 85~99 wt.%，
        # 按"合金元素上限"去卡会把它误判成越界。
        if v < low:
            return (f"{key}={value} wt.% 偏低：{bare} 作为基体通常 ≥{low:.0f} wt.%"
                    f"（原文可能未列 {bare}——含量以「余量/Bal.」表示，"
                    f"或此处是抽取错位/单位错误）")
        return ""
    table = ELEMENT_MAX_WT if limits is None else limits
    high = table.get(bare, default_max)
    if high and v > high:
        return (f"{key}={value} wt.% 超出 {bare} 的常见上限 {high:g} wt.%"
                f"（疑似列错位/单位混淆：把别的量纲当成了质量分数）")
    return ""


def composition_sum(comp: dict) -> float | None:
    """把所有 wt.% 成分值加起来，用于「总和明显超过 100」的校验。"""
    total = 0.0
    n = 0
    for _k, fv in (comp or {}).items():
        val = getattr(fv, "value", None)
        unit = canonical_unit(getattr(fv, "unit", "") or "")
        if unit != "wt.%" or val is None:
            continue
        try:
            total += float(str(val).strip())
            n += 1
        except (TypeError, ValueError):
            continue
    return total if n else None


def canonical_unit(unit: str) -> str:
    if not unit:
        return ""
    key = unit.strip().lower().replace(" ", "")
    return UNIT_ALIASES.get(key, unit.strip())


# ------------------------------------------------ 工艺/性能字段的量纲与范围校验

# 默认物理范围（可按合金体系在 config 的 extract.quantity_ranges 覆盖）。
# 依据：铝合金固溶 450~580 °C、时效 100~250 °C 是常规区间；这里放宽到
# 边界之外仍能容下异常工艺（如 300 °C 低温固溶、600 °C 高温时效）。
QUANTITY_RANGES: dict[str, tuple[float, float]] = {
    "solution_temp": (300.0, 660.0),   # 固溶温度 °C
    "aging_temp": (20.0, 320.0),       # 时效温度 °C
    "solution_time": (0.05, 300.0),    # 固溶时间 h
    "aging_time": (0.05, 800.0),       # 时效时间 h
    "temperature": (-80.0, 1400.0),    # 其他温度
    "time": (0.001, 5000.0),           # 其他时间
    "strength": (0.0, 3000.0),         # 强度 MPa
    "hardness": (0.0, 2000.0),         # 硬度 HV/HB
    "elongation": (0.0, 100.0),        # 延伸率 %
}

# 量纲词 -> 单位模式（用词边界匹配，避免 "kJ/mol" 里的 k、"hours" 之外的 h 误命中）
_UNIT_PATTERNS = {
    "temp": r"°\s*c|℃|\bdegc\b|\bkelvin\b",
    "time": (r"\b(?:h|hr|hrs|hour|hours|min|mins|minute|minutes|"
             r"s|sec|secs|second|seconds|day|days)\b|小时|分钟|秒|天"),
    "pct": r"\s*[%％]|\bpct\b|\bpercent\b",
}


def quantity_kind(label: str, key: str = "") -> str:
    """按字段标签判定它是什么物理量（决定用哪条范围规则）。

    工艺字段是"最容易被搬错列"的地方——时效温度=1、固溶温度=6 这类值
    在成分校验里永远不会被拦下，因为它们的单位不是 wt.%。
    """
    s = f"{label or ''} {key or ''}".lower()
    has_sol = ("固溶" in s) or ("solution" in s) or ("solubil" in s)
    has_age = ("时效" in s) or ("aging" in s) or ("ageing" in s) or ("age " in s)
    if "温度" in s or "temp" in s:
        if has_sol:
            return "solution_temp"
        if has_age:
            return "aging_temp"
        return "temperature"
    if "时间" in s or "time" in s or "hour" in s or "duration" in s:
        if has_sol:
            return "solution_time"
        if has_age:
            return "aging_time"
        return "time"
    if "强度" in s or "strength" in s or "uts" in s or "ys" in s:
        return "strength"
    if "硬度" in s or "hardness" in s or s.strip().endswith("hv") or "hb" in s:
        return "hardness"
    if "延伸" in s or "伸长" in s or "elongation" in s or s.strip() == "el":
        return "elongation"
    return ""


def quantity_issue(kind: str, value, quote: str = "",
                   ranges: dict[str, tuple[float, float]] | None = None,
                   ) -> str:
    """对工艺/性能数值做量纲与范围校验，返回问题说明；没问题返回空串。

    两类硬错误（都说明"这个数不属于这个字段"）：
      1) 数值超出该字段的物理范围（时效温度=1、固溶温度=6）；
      2) 引文里紧跟该数字的单位与该字段的量纲冲突（"1 h" 被当成时效温度）。
    只在有**阳性证据**时才报，单位缺失不报错——原文常常不写单位。
    """
    if not kind or value is None:
        return ""
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return ""
    table = dict(QUANTITY_RANGES)
    table.update(ranges or {})
    lo, hi = table.get(kind) or table.get(kind.split("_")[-1], (-1e18, 1e18))
    if not (lo <= v <= hi):
        return (f"{v:g} 超出该字段的物理范围 {lo:g}~{hi:g}"
                f"（疑似把别的量纲/别的列写进了这个字段）")
    want = _modal_kind(kind)
    got = _unit_near(quote, v)
    if want and got and got != want:
        return (f"引文里 {v:g} 后面跟的是「{got}」单位，但这个字段是「{want}」"
                f"（疑似时间/温度落错列）")
    return ""


def _modal_kind(kind: str) -> str:
    if kind.endswith("temp") or kind == "temperature":
        return "temp"
    if kind.endswith("time") or kind == "time":
        return "time"
    if kind in ("elongation", "pct", "percentage"):
        return "pct"
    return ""


def _unit_near(quote: str, v: float) -> str:
    """在引文里找到这个数字，看它后面紧邻（8 个字符内）是什么单位。

    用词边界匹配：否则 "kJ/mol" 里的小写 k 会被当成开尔文，
    "strength" 里的 s 会被当成秒。
    """
    if not quote:
        return ""
    s = str(quote).lower()
    for token in {f"{v:g}", str(int(v)) if float(v).is_integer() else f"{v:g}"}:
        i = s.find(token)
        while i >= 0:
            window = s[i + len(token): i + len(token) + 8]
            for kind, pat in _UNIT_PATTERNS.items():
                if re.search(pat, window):
                    return kind
            i = s.find(token, i + 1)
    return ""
