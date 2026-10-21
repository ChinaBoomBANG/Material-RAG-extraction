"""Schema 模板的解析、校验、存取。

模板来源三处：
  1. data/schemas/builtin/*.yaml —— 内置模板（由原 94 字段词典导出，只读）
  2. data/schemas/*.yaml|json    —— 用户模板（可增删改）
  3. 请求体直接传入的文本/对象    —— 界面里即时编辑、不落盘也能抽

校验返回结构化错误（含定位路径），不抛异常——界面要能把错误逐条标在字段上。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace

import yaml

from .. import config as cfg
from ..extract.schema import FIELD_DICT
from .model import BUCKETS, TYPES, FieldDef, GroupDef, SchemaDef, _BAD_KEY

BUILTIN_NAME = "material_default"


# ---------------------------------------------------------------- 存取路径

def schemas_dir() -> str:
    d = cfg.get("project.schemas_dir", "data/schemas")
    p = d if os.path.isabs(d) else os.path.join(cfg.ROOT, d)
    return cfg.ensure_dir(os.path.normpath(p))


def builtin_dir() -> str:
    return cfg.ensure_dir(os.path.join(schemas_dir(), "builtin"))


# ---------------------------------------------------------------- 解析

def _coerce_field(raw: dict, where: str, errors: list[dict]) -> FieldDef | None:
    if not isinstance(raw, dict):
        errors.append({"path": where, "msg": "字段必须是对象（如 {key: Si, type: number}）"})
        return None
    key = str(raw.get("key") or "").strip()
    if not key:
        errors.append({"path": where, "msg": "缺少 key"})
        return None
    if _BAD_KEY.search(key):
        errors.append({"path": f"{where}.key",
                       "msg": f"key「{key}」不能含点号或空格（点号是内部分组分隔符）"})
        return None
    ftype = str(raw.get("type") or "number").strip().lower()
    if ftype not in TYPES:
        errors.append({"path": f"{where}.type",
                       "msg": f"type「{ftype}」不支持，可选：{'/'.join(TYPES)}"})
        ftype = "text"
    enum = [str(x) for x in (raw.get("enum") or [])]
    if ftype == "enum" and not enum:
        errors.append({"path": f"{where}.enum", "msg": "type=enum 时必须给出取值列表"})
    aliases = [str(x) for x in (raw.get("aliases") or []) if str(x).strip()]
    return FieldDef(key=key, label=str(raw.get("label") or key).strip(),
                    type=ftype, unit=str(raw.get("unit") or "").strip(),
                    required=bool(raw.get("required", False)),
                    aliases=aliases, enum=enum,
                    hint=str(raw.get("hint") or "").strip(),
                    example=str(raw.get("example") or "").strip())


def from_dict(d: dict, source: str = "user", path: str = "") -> tuple[SchemaDef | None, list[dict], list[dict]]:
    """dict -> (SchemaDef, errors, warnings)。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    if not isinstance(d, dict):
        return None, [{"path": "", "msg": "模板根节点必须是对象"}], []

    name = str(d.get("name") or "").strip()
    if not name:
        errors.append({"path": "name", "msg": "缺少模板名 name"})

    raw_groups = d.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        errors.append({"path": "groups", "msg": "groups 必须是非空数组"})
        return None, errors, warnings

    groups: list[GroupDef] = []
    seen_group: set[str] = set()
    for gi, rg in enumerate(raw_groups):
        where = f"groups[{gi}]"
        if not isinstance(rg, dict):
            errors.append({"path": where, "msg": "分组必须是对象"})
            continue
        gkey = str(rg.get("key") or "").strip()
        if not gkey:
            errors.append({"path": f"{where}.key", "msg": "分组缺少 key"})
            continue
        if gkey in seen_group:
            errors.append({"path": f"{where}.key", "msg": f"分组 key「{gkey}」重复"})
        seen_group.add(gkey)
        bucket = str(rg.get("bucket") or "").strip().lower()
        if bucket and bucket not in BUCKETS:
            warnings.append({"path": f"{where}.bucket",
                             "msg": f"bucket「{bucket}」无效，已按分组名自动推断"})
            bucket = ""
        raw_fields = rg.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            errors.append({"path": f"{where}.fields", "msg": "分组至少要有一个字段"})
            continue
        fields: list[FieldDef] = []
        for fi, rf in enumerate(raw_fields):
            f = _coerce_field(rf, f"{where}.fields[{fi}]", errors)
            if f is not None:
                fields.append(f)
        groups.append(GroupDef(key=gkey, label=str(rg.get("label") or gkey).strip(),
                               bucket=bucket, fields=fields))

    schema = SchemaDef(name=name, groups=groups,
                       version=int(d.get("version") or 1),
                       key_field=str(d.get("key_field") or "alloy").strip() or "alloy",
                       description=str(d.get("description") or "").strip(),
                       source=source, path=path)

    # ---- 内部字段名唯一性（桶.字段）----
    seen_key: dict[str, str] = {}
    for g in schema.groups:
        for f in g.fields:
            k = schema.internal_key(g, f)
            if k in seen_key:
                errors.append({"path": f"字段「{f.key}」",
                               "msg": f"内部名 {k} 与另一个字段冲突（两个分组落到了同一个桶，"
                                      f"请给字段改名或显式指定 bucket）"})
            else:
                seen_key[k] = g.key
    if schema.key_field and schema.key_field not in seen_key:
        warnings.append({"path": "key_field",
                         "msg": f"key_field「{schema.key_field}」不在字段里，去重将退化为按全部成分字段"})

    # ---- 别名冲突（只提醒，不阻断）----
    alias_owner: dict[str, str] = {}
    for g in schema.groups:
        for f in g.fields:
            for a in [f.label, *f.aliases]:
                a = (a or "").strip()
                if not a:
                    continue
                if a in alias_owner and alias_owner[a] != f.key:
                    warnings.append({"path": f"字段「{f.key}」",
                                     "msg": f"「{a}」同时是 {alias_owner[a]} 的别名，表头匹配可能歧义"})
                else:
                    alias_owner[a] = f.key

    if schema.field_count() > 120:
        warnings.append({"path": "", "msg": f"共 {schema.field_count()} 个字段，"
                                            f"提示词会很长；建议单块启用字段 ≤ 25（两段式召回）"})
    if not any(f.key == "alloy" for g in schema.groups for f in g.fields):
        warnings.append({"path": "", "msg": "模板没有 alloy 字段，记录将无法标识来源样品"})

    schema.schema_hash = schema.compute_hash()
    return (None if errors else schema), errors, warnings


def from_text(text: str, source: str = "user", path: str = "") -> tuple[SchemaDef | None, list[dict], list[dict]]:
    """YAML 或 JSON 文本 -> (SchemaDef, errors, warnings)。"""
    text = (text or "").strip()
    if not text:
        return None, [{"path": "", "msg": "模板内容为空"}], []
    try:
        if text[0] in "{[":
            obj = json.loads(text)
        else:
            obj = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        return None, [{"path": "", "msg": f"解析失败：{e}"}], []
    return from_dict(obj, source=source, path=path)


# ---------------------------------------------------------------- 读写

def save(schema: SchemaDef, path: str = "") -> str:
    """保存模板到 YAML；返回文件路径。

    同名覆盖：不指定 path 时，若 data/schemas/ 下已有 name 相同的用户模板，
    **覆盖那个文件**而不是按名字再生成一个新文件——否则界面会出现两个同名模板
    （实测踩到：在「铝合金精简模板」上加字段后点另存为，产生第二个同名文件）。
    """
    if not path:
        for item in list_schemas():
            if (item.get("name") == schema.name and item.get("ok")
                    and item.get("source") == "user"):
                path = item["path"]
                break
    if not path:
        d = schemas_dir()
        safe = re.sub(r"[^\w\-]+", "_", schema.name).strip("_") or "schema"
        path = os.path.join(d, f"{safe}.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(schema.to_yaml_dict(), f, allow_unicode=True,
                       sort_keys=False, default_flow_style=False)
    return path


def _load_file(path: str) -> tuple[SchemaDef | None, list[dict], list[dict]]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    src = "builtin" if os.path.join("schemas", "builtin") in path.replace("/", os.sep) else "user"
    schema, errs, warns = from_text(text, source=src, path=path)
    if schema is not None:
        schema.name = schema.name or os.path.splitext(os.path.basename(path))[0]
    return schema, errs, warns


def list_schemas() -> list[dict]:
    """列出内置与用户模板（按名称排序，内置在前）。"""
    ensure_builtin()
    out: list[dict] = []
    for sub, src in (("builtin", "builtin"), ("", "user")):
        d = os.path.join(schemas_dir(), sub) if sub else schemas_dir()
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith((".yaml", ".yml", ".json")):
                continue
            p = os.path.join(d, fn)
            schema, errs, _w = _load_file(p)
            if schema is None:
                out.append({"name": os.path.splitext(fn)[0], "source": src,
                            "path": p, "ok": False,
                            "error": errs[0]["msg"] if errs else "解析失败"})
                continue
            item = schema.to_dict()
            item["ok"] = True
            item["mtime"] = int(os.path.getmtime(p))
            out.append(item)
    out.sort(key=lambda x: (x.get("source") != "builtin", x.get("name", "")))
    return out


def resolve(name_or_path: str) -> tuple[SchemaDef | None, list[dict], list[dict]]:
    """按「文件路径 / 文件名 stem / 模板 name 字段」加载模板。

    模板的显示名（YAML 里的 ``name``）未必等于文件名（例如显示名是中文
    「铝合金精简模板」，文件名却是 example_al_alloy.yaml）。所以三层匹配：
    1) 直接给的是文件路径；2) 文件名 stem 命中；3) 都不中时，扫描所有模板
    文件，用 ``name`` 字段匹配，避免中文显示名点选时报「找不到模板」。
    """
    if name_or_path and os.path.isfile(name_or_path):
        return _load_file(name_or_path)
    stem = name_or_path or BUILTIN_NAME
    for cand in (os.path.join(schemas_dir(), f"{stem}.yaml"),
                 os.path.join(schemas_dir(), f"{stem}.yml"),
                 os.path.join(schemas_dir(), f"{stem}.json"),
                 os.path.join(builtin_dir(), f"{stem}.yaml"),
                 os.path.join(builtin_dir(), f"{stem}.json")):
        if os.path.isfile(cand):
            return _load_file(cand)
    # 按 name 字段兜底扫描：显示名与文件名不一致时（中文名尤其常见）
    for item in list_schemas():
        if item.get("name") == name_or_path and item.get("ok"):
            return _load_file(item["path"])
    return None, [{"path": "", "msg": f"找不到模板「{stem}」"}], []


def load_any(obj) -> tuple[SchemaDef | None, list[dict], list[dict]]:
    """统一入口：接受 SchemaDef / dict / 模板名 / 文件路径 / YAML-JSON 文本。

    管线里 `schema` 参数的形态由调用方决定（CLI 传名字，界面传文本或对象），
    这里一次性归一，避免每个入口各写一份判断。
    """
    if isinstance(obj, SchemaDef):
        return obj, [], []
    if isinstance(obj, dict):
        return from_dict(obj)
    text = str(obj or "")
    if not text.strip():
        return None, [{"path": "", "msg": "Schema 为空"}], []
    if os.path.isfile(text):
        return resolve(text)
    if "\n" in text or ":" in text or text.lstrip()[:1] in "{[":
        return from_text(text)
    return resolve(text)


# ---------------------------------------------------------------- 内置模板

def builtin_schema() -> SchemaDef:
    """把写死的 94 字段词典转成 SchemaDef（重构的回归基线）。"""
    label_of = {"composition": "化学成分", "process": "工艺",
                "microstructure": "组织", "properties": "力学性能", "meta": "标识"}
    order = ["meta", "composition", "process", "microstructure", "properties"]
    by_bucket: dict[str, list[FieldDef]] = {b: [] for b in order}
    for spec in FIELD_DICT:
        bucket = spec.group if spec.group in by_bucket else "properties"
        bare = spec.key.split(".", 1)[1] if "." in spec.key else spec.key
        # required 一律 False：内置模板必须与旧版 schema_brief 逐字符一致
        # （这是重构的回归基线），"必填"只留给用户自定义模板使用。
        by_bucket[bucket].append(FieldDef(
            key=bare, label=spec.label, type=spec.kind, unit=spec.unit,
            aliases=list(spec.aliases)))
    groups = [GroupDef(key=b, label=label_of[b], bucket=b, fields=by_bucket[b])
              for b in order if by_bucket[b]]
    schema = SchemaDef(name=BUILTIN_NAME, groups=groups, version=1,
                       key_field="alloy", description="内置材料模板（原 94 字段词典导出）",
                       source="builtin")
    schema.schema_hash = schema.compute_hash()
    return schema


def ensure_builtin() -> str:
    """确保内置模板文件存在，返回路径。"""
    p = os.path.join(builtin_dir(), f"{BUILTIN_NAME}.yaml")
    if not os.path.isfile(p):
        save(builtin_schema(), p)
    return p


def specs_from(schema: SchemaDef) -> dict:
    """便捷：SchemaDef -> 提取器 specs（仅为可读性保留）。"""
    return schema.to_specs()


# ---------------------------------------------------------------- 字段级编辑

_KEY_SAFE = re.compile(r"[^\w\-]+", re.UNICODE)


def parse_field_spec(raw: str) -> dict:
    """解析用户手输的一行字段定义。

    语法：``名称`` 或 ``名称:类型`` 或 ``名称:类型:单位``
      - 名称能匹配内置词典（中文名/别名/元素符号）时，复用它的类型、单位与中文标签；
        这样输入「抗拉强度」等价于内置的 properties.UTS（number / MPa）。
      - 匹配不到则按原样建字段：ASCII 名默认 number，中文名默认 text。
    返回 {key,label,type,unit}；类型非法时带 error。
    """
    from ..extract.schema import match_header   # 延迟导入，避免循环依赖

    raw = (raw or "").strip()
    if not raw:
        return {"error": "字段名为空"}
    parts = [p.strip() for p in raw.split(":")]
    name = parts[0]
    ftype = parts[1].lower() if len(parts) > 1 else ""
    unit = parts[2] if len(parts) > 2 else ""
    if not name:
        return {"error": "字段名为空"}

    spec = match_header(name)
    if spec is not None:
        key = spec.key.split(".", 1)[1] if "." in spec.key else spec.key
        label = spec.label
        ftype = ftype or spec.kind
        unit = unit or spec.unit
    else:
        key = _KEY_SAFE.sub("_", name).strip("_") or "field"
        label = name
        ftype = ftype or ("number" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", name) else "text")

    if ftype not in TYPES:
        return {"error": f"类型「{ftype}」不支持（可选 {'/'.join(TYPES)}）", "key": key}
    return {"key": key, "label": label, "type": ftype, "unit": unit}


def apply_field_edit(text: str, op: str = "add", group: str = "",
                     fields: list[str] | None = None, keys: list[str] | None = None,
                     before: str = ""
                     ) -> tuple[str | None, list[dict], dict]:
    """在模板文本上增删/移动字段，返回 (新文本, 错误, 详情)。

    为什么要放在后端做：YAML 是唯一事实源，前端没有 YAML 解析器；
    而且「输入中文名 → 复用内置类型/单位」需要词典，只有后端有。
    ``move``：把 keys[0] 移动到 group 组、插在 before（内部 key）之前；
    before 为空 = 追加到组末尾。界面字段拖拽排序用。
    """
    schema, errs, _w = from_text(text or "")
    if schema is None:
        return None, errs or [{"path": "", "msg": "模板无法解析"}], {}

    d = schema.to_yaml_dict()
    groups = d.get("groups") or []
    info: dict = {"added": [], "skipped": [], "removed": [],
                  "dropped_groups": [], "parsed": []}

    if op == "add":
        target = next((g for g in groups if g.get("key") == group), None)
        if target is None:
            return None, [{"path": "group", "msg": f"分组「{group}」不存在"}], info
        existing = {f.get("key") for g in groups for f in (g.get("fields") or [])}
        local_errs: list[dict] = []
        for raw in (fields or []):
            for piece in re.split(r"[,，;；\n]+", raw):
                piece = piece.strip()
                if not piece:
                    continue
                spec = parse_field_spec(piece)
                info["parsed"].append({**spec, "input": piece})
                if spec.get("error"):
                    local_errs.append({"path": piece, "msg": spec["error"]})
                    continue
                if spec["key"] in existing:
                    info["skipped"].append(spec["key"])
                    continue
                target.setdefault("fields", []).append({
                    "key": spec["key"], "label": spec["label"],
                    "type": spec["type"], "unit": spec["unit"], "required": False,
                })
                existing.add(spec["key"])
                info["added"].append(spec["key"])
        if local_errs and not info["added"]:
            return None, local_errs, info

    elif op == "remove":
        want = set(keys or [])
        for g in groups:
            keep = []
            for f in (g.get("fields") or []):
                bucket = g.get("bucket") or "properties"
                internal = f["key"] if bucket == "meta" else f"{bucket}.{f['key']}"
                if internal in want or f["key"] in want:
                    info["removed"].append(internal)
                else:
                    keep.append(f)
            g["fields"] = keep
        # 空分组直接移除——模板要求每组至少一个字段，否则校验会失败
        n_groups = len(groups)
        groups = [g for g in groups if g.get("fields")]
        d["groups"] = groups
        info["dropped_groups"] = [g["key"] for g in d["groups"]] if n_groups != len(groups) else []
        if not groups:
            return None, [{"path": "groups", "msg": "至少要保留一个字段"}], info

    elif op == "move":
        # 拖拽排序：把 keys[0] 从原组取出，插入 group 组的 before 之前（空=末尾）。
        # 注意顺序：先全组摘除、再插入、最后统一过滤空组，避免目标组被误删。
        want = (keys or [""])[0]
        if not want:
            return None, [{"path": "keys", "msg": "move 需要指定要移动的字段"}], info
        target = next((g for g in groups if g.get("key") == group), None)
        if target is None:
            return None, [{"path": "group", "msg": f"分组「{group}」不存在"}], info
        moved = None
        for g in groups:
            keep = []
            for f in (g.get("fields") or []):
                bucket = g.get("bucket") or "properties"
                internal = f["key"] if bucket == "meta" else f"{bucket}.{f['key']}"
                if moved is None and (internal == want or f["key"] == want):
                    moved = f
                    info["moved"] = internal
                else:
                    keep.append(f)
            g["fields"] = keep
        if moved is None:
            return None, [{"path": "keys", "msg": f"找不到字段「{want}」"}], info
        lst = target.setdefault("fields", [])
        idx = len(lst)
        if before:
            tb = target.get("bucket") or "properties"
            for i, f in enumerate(lst):
                fi = f["key"] if tb == "meta" else f"{tb}.{f['key']}"
                if fi == before or f["key"] == before:
                    idx = i
                    break
        lst.insert(idx, moved)
        d["groups"] = [g for g in groups if g.get("fields")]
        info["to"] = group
        info["before"] = before

    else:
        return None, [{"path": "op", "msg": f"未知操作「{op}」"}], info

    new_text = yaml.safe_dump(d, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    _s2, errs2, _w2 = from_text(new_text)
    if errs2:
        return None, errs2, info
    return new_text, [], info
