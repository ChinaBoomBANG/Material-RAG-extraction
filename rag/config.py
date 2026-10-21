"""配置加载：读取 config.yaml，解析项目内相对路径。"""

from __future__ import annotations

import os
from typing import Any

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

_config: dict[str, Any] | None = None


def load(path: str | None = None) -> dict[str, Any]:
    """加载并缓存配置。"""
    global _config
    if _config is None or path is not None:
        p = path or CONFIG_PATH
        with open(p, "r", encoding="utf-8") as f:
            _config = yaml.safe_load(f)
    return _config


def get(key: str, default: Any = None) -> Any:
    """按 "a.b.c" 取配置值。"""
    node: Any = load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def path(key: str) -> str:
    """取配置中的路径并解析为绝对路径。"""
    raw = get(key)
    if raw is None:
        raise KeyError(f"配置项不存在: {key}")
    p = raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
    return os.path.normpath(p)


def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p
