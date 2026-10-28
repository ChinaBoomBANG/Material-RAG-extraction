"""抽取字段词典。

设计原则：
  1. **Excel 表头说了算**。目标表的列名是抽取契约，本词典只负责把列名
     映射到标准字段（key），并附带单位、类型与同义表述，供提示词与校验使用。
  2. 词典里没有的列不会被丢弃，而是以"自定义列"身份原样送模型抽取，
     保证用户随时能在表里加一列而不改代码。
  3. 所有字段都允许缺失；缺失即置空，绝不用模型推测填充。

分组：composition(成分) / process(工艺) / microstructure(组织) / properties(性能)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FieldSpec:
    key: str                       # 标准字段名，如 "composition.Si"
    label: str                     # 中文/展示名
    group: str                     # composition|process|microstructure|properties|meta
    unit: str = ""
    kind: str = "number"           # number|text|range|bool
    aliases: list[str] = field(default_factory=list)   # 表头/正文中的同义写法
    header: str = ""               # 目标 Excel 中的原始列名（写回时以此为准）
    required: bool = False         # 必填字段（自定义 Schema 用）
    enum: list[str] = field(default_factory=list)      # kind=enum 时的取值域
    hint: str = ""                 # 额外提示语，直接进提示词
    example: str = ""              # 少样本示例值

    @property
    def column(self) -> str:
        """写回 Excel 时使用的列名：优先原始表头。"""
        return self.header or self.label


def _f(key, label, group, unit="", kind="number", aliases=None) -> FieldSpec:
    return FieldSpec(key, label, group, unit, kind, aliases or [])


# ------------------------------------------------------------------ 成分

COMPOSITION_ELEMENTS = [
    ("Al", "Al", ["铝", "Aluminium", "Aluminum", "balance", "余量", "Bal"]),
    ("Si", "Si", ["硅", "Silicon"]),
    ("Mg", "Mg", ["镁", "Magnesium"]),
    ("Cu", "Cu", ["铜", "Copper"]),
    ("Fe", "Fe", ["铁", "Iron"]),
    ("Mn", "Mn", ["锰", "Manganese"]),
    ("Zn", "Zn", ["锌", "Zinc"]),
    ("Cr", "Cr", ["铬", "Chromium"]),
    ("Ti", "Ti", ["钛", "Titanium"]),
    ("Zr", "Zr", ["锆", "Zirconium"]),
    ("Ni", "Ni", ["镍", "Nickel"]),
    ("Sr", "Sr", ["锶", "Strontium"]),
    ("B", "B", ["硼", "Boron"]),
    ("Na", "Na", ["钠", "Sodium"]),
    ("Ca", "Ca", ["钙", "Calcium"]),
    ("Pb", "Pb", ["铅", "Lead"]),
    ("Sn", "Sn", ["锡", "Tin"]),
    ("V", "V", ["钒", "Vanadium"]),
    ("Sc", "Sc", ["钪", "Scandium"]),
    ("Li", "Li", ["锂", "Lithium"]),
    ("Ag", "Ag", ["银", "Silver"]),
    ("RE", "RE", ["稀土", "Rare Earth", "La", "Ce", "Y"]),
    ("Others", "其他/杂质", ["Others", "Impurities", "杂质", "其他"]),
]

# ------------------------------------------------------------------ 词典主体

FIELD_DICT: list[FieldSpec] = []

# 记录标识：每条记录必须能指明是哪个合金/牌号
FIELD_DICT.append(_f("alloy", "合金牌号", "meta", "", "text",
                     ["合金", "牌号", "材料", "合金名称", "alloy", "AA", "designation"]))

for _sym, _lab, _al in COMPOSITION_ELEMENTS:
    FIELD_DICT.append(_f(f"composition.{_sym}", _lab, "composition", "wt.%", "number", _al))

# 机器学习：成分的**预测值**。论文常把"预测成分 vs 实测成分"并列给出，
# 两组都要保留（用户明确要求），所以每个元素额外给一个 *_pred 字段。
for _sym, _lab, _al in COMPOSITION_ELEMENTS:
    FIELD_DICT.append(_f(f"composition.{_sym}_pred", f"{_lab} 预测值", "composition",
                         "wt.%", "number", [f"预测{_lab}", f"predicted {_sym}"]))

FIELD_DICT += [
    # ---- 工艺 ----
    _f("process.casting", "铸造/制备方式", "process", "", "text",
       ["铸造", "DC casting", "半连续铸造", "casting", "熔炼", "制备"]),
    _f("process.homogenization_temp", "均匀化温度", "process", "°C", "number",
       ["均匀化温度", "homogenization temperature", "Homogenizing"]),
    _f("process.homogenization_time", "均匀化时间", "process", "h", "number",
       ["均匀化时间", "homogenization time"]),
    _f("process.solution_temp", "固溶温度", "process", "°C", "number",
       ["固溶温度", "solution temperature", "solution treatment", "固溶处理"]),
    _f("process.solution_time", "固溶时间", "process", "h", "number",
       ["固溶时间", "solution time", "固溶保温"]),
    _f("process.quench", "淬火方式/冷速", "process", "", "text",
       ["淬火", "quenching", "water quench", "水淬", "空冷", "冷却速率", "cooling rate"]),
    _f("process.aging_temp", "时效温度", "process", "°C", "number",
       ["时效温度", "aging temperature", "artificial aging", "人工时效", "T6", "T5"]),
    _f("process.aging_time", "时效时间", "process", "h", "number",
       ["时效时间", "aging time", "时效保温"]),
    _f("process.aging_stage2_temp", "二级时效温度", "process", "°C", "number",
       ["二级时效温度", "second stage aging", "双级时效"]),
    _f("process.aging_stage2_time", "二级时效时间", "process", "h", "number",
       ["二级时效时间", "second step aging time"]),
    _f("process.pre_aging", "预时效", "process", "", "text",
       ["预时效", "pre-aging", "preaging", "自然时效", "natural aging"]),
    _f("process.hot_deform_temp", "热变形温度", "process", "°C", "number",
       ["热变形温度", "热加工温度", "extrusion temperature", "挤压温度", "rolling temperature"]),
    _f("process.extrusion_ratio", "挤压比", "process", "", "number",
       ["挤压比", "extrusion ratio"]),
    _f("process.rolling_reduction", "轧制压下量", "process", "%", "number",
       ["轧制压下量", "rolling reduction", "压下率"]),
    _f("process.cold_work", "冷变形量", "process", "%", "number",
       ["冷变形量", "cold work", "冷加工率", "预变形"]),
    _f("process.ramp_rate", "升温速率", "process", "°C/min", "number",
       ["升温速率", "heating rate", "加热速率"]),

    # ---- 组织 ----
    _f("microstructure.grain_size", "晶粒尺寸", "microstructure", "μm", "number",
       ["晶粒尺寸", "grain size", "平均晶粒", "晶粒度"]),
    _f("microstructure.grain_morphology", "晶粒形貌", "microstructure", "", "text",
       ["晶粒形貌", "grain morphology", "纤维组织", "等轴晶", "柱状晶"]),
    _f("microstructure.precipitate_type", "析出相类型", "microstructure", "", "text",
       ["析出相", "precipitate", "β''", "β'", "Q'", "GP zone", "GP区", "Mg2Si", "Al3Zr"]),
    _f("microstructure.precipitate_size", "析出相尺寸", "microstructure", "nm", "number",
       ["析出相尺寸", "precipitate size", "析出相直径", "粒子尺寸"]),
    _f("microstructure.precipitate_fraction", "析出相体积分数", "microstructure", "%", "number",
       ["体积分数", "volume fraction", "面积分数", "areal fraction"]),
    _f("microstructure.recrystallization", "再结晶分数", "microstructure", "%", "number",
       ["再结晶分数", "recrystallization fraction", "再结晶比例"]),
    _f("microstructure.texture", "织构", "microstructure", "", "text",
       ["织构", "texture", "取向", "Brass", "Cube", "Goss"]),
    _f("microstructure.dislocation_density", "位错密度", "microstructure", "m^-2", "number",
       ["位错密度", "dislocation density"]),
    _f("microstructure.pfz_width", "PFZ 宽度", "microstructure", "nm", "number",
       ["PFZ", "无沉淀析出带", "precipitate free zone"]),
    _f("microstructure.second_phase", "第二相/夹杂", "microstructure", "", "text",
       ["第二相", "second phase", "夹杂物", "inclusion", "AlFeSi", "Mg2Si"]),

    # ---- 性能 ----
    _f("properties.YS", "屈服强度 Rp0.2", "properties", "MPa", "number",
       ["屈服强度", "yield strength", "Rp0.2", "YS", "0.2% offset"]),
    _f("properties.UTS", "抗拉强度 Rm", "properties", "MPa", "number",
       ["抗拉强度", "tensile strength", "UTS", "Rm", "σb"]),
    _f("properties.elongation", "延伸率 A", "properties", "%", "number",
       ["延伸率", "elongation", "断后伸长率", "δ", "A50mm"]),
    _f("properties.hardness_HV", "硬度 HV", "properties", "HV", "number",
       ["维氏硬度", "hardness", "HV", "Vickers", "显微硬度"]),
    _f("properties.hardness_HB", "硬度 HB", "properties", "HB", "number",
       ["布氏硬度", "HB", "Brinell"]),
    _f("properties.E", "弹性模量", "properties", "GPa", "number",
       ["弹性模量", "Young's modulus", "modulus", "E"]),
    _f("properties.density", "密度", "properties", "g/cm³", "number",
       ["密度", "density"]),
    _f("properties.conductivity", "导电率", "properties", "%IACS", "number",
       ["导电率", "conductivity", "IACS", "电导率"]),
    _f("properties.thermal_conductivity", "热导率", "properties", "W/(m·K)", "number",
       ["热导率", "thermal conductivity"]),
    _f("properties.fatigue", "疲劳强度", "properties", "MPa", "number",
       ["疲劳强度", "fatigue strength", "fatigue limit", "疲劳极限"]),
    _f("properties.fracture_toughness", "断裂韧性", "properties", "MPa·m^0.5", "number",
       ["断裂韧性", "fracture toughness", "KIC"]),
    _f("properties.IGC", "晶间腐蚀", "properties", "", "text",
       ["晶间腐蚀", "intergranular corrosion", "IGC"]),
    _f("properties.EXCO", "剥落腐蚀", "properties", "", "text",
       ["剥落腐蚀", "exfoliation corrosion", "EXCO"]),
    _f("properties.corrosion_rate", "腐蚀速率", "properties", "mm/a", "number",
       ["腐蚀速率", "corrosion rate"]),
    _f("properties.test_temp", "性能测试温度", "properties", "°C", "number",
       ["测试温度", "test temperature", "室温", "RT", "ambient"]),
    _f("properties.test_condition", "性能测试条件", "properties", "", "text",
       ["应变速率", "strain rate", "拉伸速率", "crosshead speed", "测试标准", "ASTM"]),

    # ---- 机器学习：预测值（与实测值成对保留，不覆盖实测）----
    _f("properties.UTS_pred", "抗拉强度 预测值", "properties", "MPa", "number",
       ["预测抗拉强度", "UTS predicted", "predicted UTS", "UTS-Pre", "抗拉强度预测"]),
    _f("properties.YS_pred", "屈服强度 预测值", "properties", "MPa", "number",
       ["预测屈服强度", "YS predicted", "predicted YS", "YS-Pre", "屈服强度预测"]),
    _f("properties.elongation_pred", "延伸率 预测值", "properties", "%", "number",
       ["预测延伸率", "elongation predicted", "predicted elongation", "EL-Pre"]),
    _f("properties.hardness_HB_pred", "硬度 HB 预测值", "properties", "HB", "number",
       ["预测硬度", "HB predicted", "predicted hardness"]),
    _f("properties.EC_pred", "导电率 预测值", "properties", "%IACS", "number",
       ["预测导电率", "EC predicted", "predicted conductivity"]),
]

BY_KEY: dict[str, FieldSpec] = {f.key: f for f in FIELD_DICT}

# 便捷引用：合金牌号字段
ALLOY_SPEC: FieldSpec = BY_KEY["alloy"]


# ------------------------------------------------------------------ 表头匹配

_NORM = re.compile(r"[\s_\-/\\(\\)\[\]（）]+")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")")
    s = _NORM.sub("", s)
    return s


# 反向索引：归一化别名 -> key
_ALIAS_INDEX: dict[str, str] = {}
for _spec in FIELD_DICT:
    _ALIAS_INDEX[_norm(_spec.key.split(".")[-1])] = _spec.key
    _ALIAS_INDEX[_norm(_spec.label)] = _spec.key
    for _a in _spec.aliases:
        _ALIAS_INDEX.setdefault(_norm(_a), _spec.key)


def match_header(header: str) -> FieldSpec | None:
    """把 Excel 表头匹配到标准字段。匹配不到返回 None（按自定义列处理）。"""
    n = _norm(header)
    if not n:
        return None
    if n in _ALIAS_INDEX:
        return BY_KEY[_ALIAS_INDEX[n]]
    # 成分列的常见写法：Si(wt.%) / Si 含量 / w(Si)
    m = re.search(r"^(?:w\()?([a-z]{1,3})(?:\))?(?:含量|wt|mas|质量分数)?", n)
    if m:
        sym = m.group(1).capitalize()
        key = f"composition.{sym}"
        if key in BY_KEY:
            return BY_KEY[key]
    # 子串包含：表头更长时兜底
    for alias, key in _ALIAS_INDEX.items():
        if len(alias) >= 2 and alias in n:
            return BY_KEY[key]
    return None


def strict_spec_for_header(header: str) -> FieldSpec | None:
    """保守版表头匹配：只接受「别名原样」和「元素+单位」两种确定性写法。

    与 match_header 的区别：去掉了最后那步"子串包含兜底"。
    抽取时用宽松匹配无妨（不确定就变成了自定义列）；但**写回 Excel** 时
    子串兜底会把数据塞进不该去的列（例如表头"Al 余量说明"命中了 Al 列），
    属于擅自改写用户的表。这里宁可对不上、明确告诉用户，也不猜。
    """
    n = _norm(header)
    if not n:
        return None
    key = _ALIAS_INDEX.get(n)
    if key:
        return BY_KEY.get(key)
    # 中文/全称写法带了单位尾巴：硅含量 / 屈服强度 MPa —— 去掉尾巴再认一次
    tail = re.sub(r"(含量|质量分数|wt\.?%|at\.?%|wt|at|ppm|mpa|gpa|hv|hb|%)$", "", n)
    if tail and tail != n and tail in _ALIAS_INDEX:
        return BY_KEY[_ALIAS_INDEX[tail]]
    # 成分列的常见写法：Si(wt.%) / Si 含量 / w(Si)
    # 必须用 fullmatch：元素符号之后只允许跟单位尾巴。用 search 时
    # "siwt.%" 会被贪心截成 "siw"，连 Si 都对不上（实测踩过）。
    m = re.fullmatch(
        r"w?([a-z]{1,3})(?:含量|质量分数|wt\.?%|at\.?%|wt|at|ppm|%)?", n)
    if m:
        return BY_KEY.get(f"composition.{m.group(1).capitalize()}")
    return None


def build_schema_from_headers(headers: list[str]) -> dict:
    """由 Excel 表头生成抽取 schema。

    返回 {"matched": [...], "custom": [...], "specs": {key: FieldSpec}}
    每个 spec 的 header 字段被设为目标表里的原始列名，写回时以它为准。
    """
    import dataclasses

    matched, custom, specs = [], [], {}
    used_keys: set[str] = set()
    for h in headers:
        h = (h or "").strip()
        if not h:
            continue
        spec = match_header(h)
        if spec and spec.key not in used_keys:
            used_keys.add(spec.key)
            spec = dataclasses.replace(spec, header=h)      # 绑定原始表头
            matched.append({"header": h, "key": spec.key, "label": spec.label,
                            "group": spec.group, "unit": spec.unit, "kind": spec.kind})
            specs[spec.key] = spec
        elif spec and spec.key in used_keys:
            continue
        else:
            key = "custom." + h
            custom.append({"header": h, "key": key})
            specs[key] = FieldSpec(key=key, label=h, group="custom",
                                   unit=_guess_unit(h), kind=_guess_kind(h),
                                   aliases=[h], header=h)
    return {"matched": matched, "custom": custom, "specs": specs}


def _guess_unit(header: str) -> str:
    m = re.search(r"[\(\（]([^)\）]+)[\)\）]", header)
    return m.group(1).strip() if m else ""


def _guess_kind(header: str) -> str:
    return "text" if re.search(r"[型类方式织构组织描述]", header) else "number"


def schema_brief(specs: dict) -> str:
    """把 schema 渲染成提示词里的一段说明。

    刻意不输出同义写法（aliases）—— 那些只用于表头匹配，放进提示词会白白
    拉长输入、拖慢本地推理。这里只给「key（中文名，类型）单位」。

    自定义 Schema 的 required / enum / hint / example 只在非空时追加，
    因此内置词典渲染出来与旧版逐字符一致（重构回归基线）。
    """
    lines = []
    for key, spec in specs.items():
        unit = f" 单位:{spec.unit}" if spec.unit else ""
        extra = ""
        if getattr(spec, "required", False):
            extra += "，必填"
        enum = getattr(spec, "enum", None) or []
        if enum:
            extra += f"，取值限于 {'/'.join(str(e) for e in enum)}"
        hint = (getattr(spec, "hint", "") or "").strip()
        if hint:
            extra += f"—— {hint[:60]}"
        example = (getattr(spec, "example", "") or "").strip()
        if example:
            extra += f"（例：{example[:30]}）"
        lines.append(f"- {key}（{spec.label}，{spec.kind}）{unit}{extra}")
    return "\n".join(lines)
