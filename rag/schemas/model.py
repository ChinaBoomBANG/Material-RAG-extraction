"""动态抽取 Schema 的数据模型。

设计要点（与重构方案的对应关系）：

1. **分组只是展示层**。用户可以用任意分组名（化学成分 / 工艺 / 力学性能……），
   但数据最终落到 4 个固定桶里（composition/process/microstructure/properties），
   这样 MaterialRecord、写回 Excel、前端展示都不需要改数据结构。
   分组 → 桶的映射由 ``bucket`` 显式指定或按关键字推断。
2. **内部字段名 = 桶.字段名**（meta 组例外，直接裸名，如 alloy）。
   这是提示词里出现的 key，也是缓存指纹的一部分。
3. ``to_specs()`` 产出 ``dict[str, FieldSpec]``，直接喂给现有提取器——
   下游（extractor / normalize / writeback）零改动。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ..extract.schema import FieldSpec

BUCKETS = ("composition", "process", "microstructure", "properties", "meta")
TYPES = ("number", "text", "range", "enum", "bool", "list")

# 分组名 -> 桶 的推断规则（用户没显式写 bucket 时用）
_BUCKET_HINTS = (
    ("composition", ("composition", "chemical", "chem", "element", "成分", "化学成分", "元素")),
    ("process", ("process", "processing", "工艺", "加工", "热处理")),
    ("microstructure", ("microstructure", "structure", "phase", "组织", "显微", "相")),
    ("properties", ("prop", "property", "mechanical", "performance", "性能", "力学")),
    ("meta", ("meta", "material", "identity", "标识", "材料")),
)

# 非法字段键（含点号/空白）：schema/loader.py 校验用户手输字段时用
_BAD_KEY = re.compile(r"[.\s]")


@dataclass
class FieldDef:
    key: str
    label: str = ""
    type: str = "number"
    unit: str = ""
    required: bool = False
    aliases: list[str] = field(default_factory=list)
    enum: list[str] = field(default_factory=list)
    hint: str = ""
    example: str = ""

    def to_dict(self) -> dict:
        """空值不输出——模板要给人看，一串 `hint: ''` 只会淹没真正的信息。"""
        d = {"key": self.key, "label": self.label or self.key, "type": self.type}
        if self.unit:
            d["unit"] = self.unit
        if self.required:
            d["required"] = True
        if self.aliases:
            d["aliases"] = list(self.aliases)
        if self.enum:
            d["enum"] = list(self.enum)
        if self.hint:
            d["hint"] = self.hint
        if self.example:
            d["example"] = self.example
        return d


@dataclass
class GroupDef:
    key: str
    label: str = ""
    bucket: str = ""
    fields: list[FieldDef] = field(default_factory=list)

    def resolved_bucket(self) -> str:
        if self.bucket in BUCKETS:
            return self.bucket
        hay = f"{self.key} {self.label}".lower()
        for bucket, words in _BUCKET_HINTS:
            if any(w in hay for w in words):
                return bucket
        return "properties"

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label or self.key,
                "bucket": self.resolved_bucket(),
                "fields": [f.to_dict() for f in self.fields]}


@dataclass
class SchemaDef:
    name: str
    groups: list[GroupDef] = field(default_factory=list)
    version: int = 1
    key_field: str = "alloy"
    description: str = ""
    source: str = "user"          # user | builtin
    path: str = ""                # 模板文件路径（内置模板也有）
    schema_hash: str = ""

    # ---------------------------------------------------------------- 派生

    def internal_key(self, group: GroupDef, f: FieldDef) -> str:
        bucket = group.resolved_bucket()
        return f.key if bucket == "meta" else f"{bucket}.{f.key}"

    def to_specs(self) -> dict[str, FieldSpec]:
        """转成提取器使用的 {内部key: FieldSpec}。顺序即提示词顺序。"""
        specs: dict[str, FieldSpec] = {}
        for g in self.groups:
            for f in g.fields:
                key = self.internal_key(g, f)
                specs[key] = FieldSpec(
                    key=key,
                    label=f.label or f.key,
                    group=g.resolved_bucket(),
                    unit=f.unit,
                    kind=f.type,
                    aliases=list(f.aliases),
                    required=bool(f.required),
                    enum=list(f.enum),
                    hint=f.hint,
                    example=f.example,
                )
        return specs

    def field_count(self) -> int:
        return sum(len(g.fields) for g in self.groups)

    def compute_hash(self) -> str:
        payload = json.dumps(
            {"name": self.name, "version": self.version, "key_field": self.key_field,
             "groups": [g.to_dict() for g in self.groups]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "key_field": self.key_field, "description": self.description,
                "source": self.source, "path": self.path,
                "schema_hash": self.schema_hash or self.compute_hash(),
                "field_count": self.field_count(),
                "groups": [g.to_dict() for g in self.groups]}

    def to_yaml_dict(self) -> dict:
        """导出为模板格式（不带运行时字段，便于人工编辑）。"""
        return {"name": self.name, "version": self.version,
                "key_field": self.key_field,
                **({"description": self.description} if self.description else {}),
                "groups": [{"key": g.key, "label": g.label or g.key,
                            "bucket": g.resolved_bucket(),
                            "fields": [f.to_dict() for f in g.fields]}
                           for g in self.groups]}
