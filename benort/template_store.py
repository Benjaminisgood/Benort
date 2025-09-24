"""用于从磁盘加载公用 LaTeX 模板的工具函数。"""

import os
from functools import lru_cache

import yaml
from flask import current_app

from .config import DEFAULT_TEMPLATE_FILENAME, FALLBACK_TEMPLATE, template_library_root


def _template_root() -> str:
    """返回模板资源所在的目录，优先读取应用配置。"""

    try:
        # Flask 配置在应用上下文存在时优先被使用
        return current_app.config["TEMPLATE_LIBRARY"]  # type: ignore[index]
    except (KeyError, RuntimeError):
        # 若无应用上下文则退回包内 temps 目录
        return template_library_root()


def _template_path(name: str = DEFAULT_TEMPLATE_FILENAME) -> str:
    """拼接模板文件完整路径。"""

    return os.path.join(_template_root(), name)


@lru_cache(maxsize=8)
def load_template(name: str = DEFAULT_TEMPLATE_FILENAME) -> dict[str, str]:
    """从 YAML 载入模板结构；缺失时回退到默认值。

    使用 ``@lru_cache`` 可以缓存最近读取的模板，避免频繁 I/O。
    ``maxsize=8`` 表示最多缓存 8 个模板文件，命中后直接返回内存数据。
    """

    path = _template_path(name)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
                return {
                    "header": str(data.get("header") or FALLBACK_TEMPLATE["header"]).strip(),
                    "beforePages": str(data.get("beforePages") or FALLBACK_TEMPLATE["beforePages"]).strip() or FALLBACK_TEMPLATE["beforePages"],
                    "footer": str(data.get("footer") or FALLBACK_TEMPLATE["footer"]).strip() or FALLBACK_TEMPLATE["footer"],
                }
    except Exception as exc:  # pragma: no cover - defensive fallback
        # 出现读取异常时打印提示并退回默认模板
        print(f"加载模板失败 {path}: {exc}")
    return dict(FALLBACK_TEMPLATE)


def get_default_template() -> dict[str, str]:
    """返回默认模板结构，供创建新项目时引用。"""

    return load_template(DEFAULT_TEMPLATE_FILENAME)


def get_default_header() -> str:
    """获取默认模板中的 header 段落。"""

    return get_default_template()["header"]


def refresh_template_cache() -> None:
    """清空 LRU 缓存，编辑模板文件后调用即可强制重新加载。"""

    load_template.cache_clear()  # type: ignore[attr-defined]


def list_templates() -> list[dict[str, str]]:
    """列出可用模板文件及其内容。"""

    root = template_library_root()
    templates: list[dict[str, str]] = []
    if os.path.isdir(root):
        for fname in sorted(os.listdir(root)):
            if not fname.lower().endswith(('.yaml', '.yml')):
                continue
            data = load_template(fname)
            templates.append({
                'name': fname,
                'header': data.get('header', ''),
                'beforePages': data.get('beforePages', ''),
                'footer': data.get('footer', ''),
            })
    return templates


__all__ = [
    "get_default_template",
    "get_default_header",
    "load_template",
    "refresh_template_cache",
    "list_templates",
]
