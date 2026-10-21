"""核心数据模型。

设计约束（对应"丝毫不差"的可核验要求）：
1. 任何进入 Excel 的字段，都必须能追溯到原文的一段引文 + 页码 + 块 ID。
2. 原文未提及的字段一律为 None，绝不用推测值填充。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


def _sid(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Chunk:
    """一个可检索、可引用的文本块。"""

    chunk_id: str
    doc_id: str
    doc_name: str
    page: int                 # 1-based 页码
    kind: str                 # "text" | "table"
    text: str
    bbox: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = list(self.bbox) if self.bbox else None
        return d

    @staticmethod
    def make(doc_id: str, doc_name: str, page: int, kind: str, text: str,
             bbox: tuple[float, float, float, float] | None = None) -> "Chunk":
        return Chunk(
            chunk_id=_sid(doc_id, str(page), kind, text[:80]),
            doc_id=doc_id,
            doc_name=doc_name,
            page=page,
            kind=kind,
            text=text,
            bbox=bbox,
        )


@dataclass
class Evidence:
    """一条字段值的原文证据。"""

    quote: str                      # 原文引文（必须能在 Chunk.text 中找到）
    chunk_id: str = ""
    page: int = 0
    doc_name: str = ""
    verified: bool = False          # 是否通过原文子串校验
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FieldValue:
    """单个字段值 + 证据。"""

    value: Any = None
    unit: str = ""
    evidence: Evidence | None = None
    parsed: dict[str, Any] | None = None   # 数值解析结果（single/range/...）

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "parsed": self.parsed,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass
class MaterialRecord:
    """一条"成分-工艺-组织-性能"记录。"""

    alloy: str = ""                 # 合金牌号 / 命名
    row_label: str = ""             # 原文中该行的标识（如 A1/D3/6013），逐字抄写
    composition: dict[str, FieldValue] = field(default_factory=dict)
    process: dict[str, FieldValue] = field(default_factory=dict)
    microstructure: dict[str, FieldValue] = field(default_factory=dict)
    properties: dict[str, FieldValue] = field(default_factory=dict)
    source_doc: str = ""
    source_page: int = 0
    record_id: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "alloy": self.alloy,
            "row_label": self.row_label,
            "composition": {k: v.to_dict() for k, v in self.composition.items()},
            "process": {k: v.to_dict() for k, v in self.process.items()},
            "microstructure": {k: v.to_dict() for k, v in self.microstructure.items()},
            "properties": {k: v.to_dict() for k, v in self.properties.items()},
            "source_doc": self.source_doc,
            "source_page": self.source_page,
            "warnings": self.warnings,
        }

    def all_fields(self) -> list[tuple[str, str, FieldValue]]:
        """展平为 (分组, 字段名, 值对象)。"""
        out: list[tuple[str, str, FieldValue]] = []
        for group, mapping in (
            ("composition", self.composition),
            ("process", self.process),
            ("microstructure", self.microstructure),
            ("properties", self.properties),
        ):
            for name, fv in mapping.items():
                out.append((group, name, fv))
        return out

    # ---------------- 反序列化（供服务端重建，保证核验权在服务端） ----------------

    @staticmethod
    def field_from_dict(d: dict) -> "FieldValue":
        if not isinstance(d, dict):
            return FieldValue()
        ev = None
        if isinstance(d.get("evidence"), dict):
            e = d["evidence"]
            ev = Evidence(quote=e.get("quote", ""), chunk_id=e.get("chunk_id", ""),
                          page=int(e.get("page", 0) or 0), doc_name=e.get("doc_name", ""),
                          verified=bool(e.get("verified")), note=e.get("note", ""))
        return FieldValue(value=d.get("value"), unit=d.get("unit", ""),
                          evidence=ev, parsed=d.get("parsed"))

    @staticmethod
    def record_from_dict(d: dict) -> "MaterialRecord":
        rec = MaterialRecord(alloy=d.get("alloy", ""), record_id=d.get("record_id", ""),
                             row_label=d.get("row_label", ""),
                             source_doc=d.get("source_doc", ""),
                             source_page=int(d.get("source_page", 0) or 0),
                             warnings=list(d.get("warnings", [])))
        for grp in ("composition", "process", "microstructure", "properties"):
            m = d.get(grp) or {}
            tgt = getattr(rec, grp)
            for k, v in m.items():
                tgt[k] = MaterialRecord.field_from_dict(v)
        return rec


@dataclass
class Answer:
    """RAG 问答结果。"""

    question: str
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    raw_context: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
