"""动态抽取 Schema 子系统。

对外只需记住三件事：
  - ``builtin_schema()`` 拿内置模板（原 94 字段词典）；
  - ``resolve("名字或路径")`` 加载模板；
  - ``schema.to_specs()`` 得到提取器用的 ``{key: FieldSpec}``。
"""

from .loader import (BUILTIN_NAME, apply_field_edit, builtin_dir, builtin_schema,
                     ensure_builtin, from_dict, from_text, list_schemas, load_any,
                     parse_field_spec, resolve, save, schemas_dir)
from .model import BUCKETS, TYPES, FieldDef, GroupDef, SchemaDef

__all__ = [
    "BUILTIN_NAME", "BUCKETS", "TYPES", "FieldDef", "GroupDef", "SchemaDef",
    "builtin_dir", "builtin_schema", "ensure_builtin", "from_dict", "from_text",
    "apply_field_edit", "list_schemas", "load_any", "parse_field_spec",
    "resolve", "save", "schemas_dir",
]
