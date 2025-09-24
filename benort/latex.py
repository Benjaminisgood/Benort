"""LaTeX 文本处理与相关资源整理工具。"""

import os
import re
import shutil
from collections.abc import Iterable


# 匹配 ``\includegraphics{...}`` 以及自定义 ``\img{...}`` 包装
_INCLUDEGRAPHICS_RE = re.compile(r'(\\includegraphics(?:\[[^]]*\])?)(\{+)([^{}]+?)(\}+)')
_CUSTOM_IMG_RE = re.compile(r'(\\img(?:\[[^]]*\])?)(\{+)([^{}]+?)(\}+)')


def _clean_latex_path(path: str) -> str:
    """规范化 LaTeX 路径，返回安全的文件名。"""
    if not path:
        return path
    cleaned = path.strip()
    cleaned = cleaned.split("?", 1)[0]
    cleaned = cleaned.replace("\\\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip()
    cleaned = cleaned.strip("{}").strip("/")
    if not cleaned or "#" in cleaned:
        return cleaned
    return os.path.basename(cleaned)


def normalize_latex_content(content: str, attachments_folder: str, resources_folder: str) -> str:
    """重写 LaTeX 内容中的图片路径，确保指向落地资源。"""

    if not isinstance(content, str) or not content:
        return content

    def _rewrite(match: re.Match[str]) -> str:
        # 针对包含路径的命令替换为清洗后的文件名
        prefix, opens, path, closes = match.groups()
        original = match.group(0)
        if "#" in path or not path.strip():
            return original
        normalized = _clean_latex_path(path)
        if not normalized:
            return original
        if len(opens) > 1 or len(closes) > 1:
            return f"{prefix}{opens}{normalized}{closes}"
        return f"{prefix}{{{normalized}}}"

    content = _INCLUDEGRAPHICS_RE.sub(_rewrite, content)
    content = _CUSTOM_IMG_RE.sub(_rewrite, content)
    return content


def _extract_graphics_paths(tex: str) -> set[str]:
    """扫描 LaTeX 字符串，提取所有图片文件名。"""

    if not tex:
        return set()
    paths: set[str] = set()
    for pattern in (_INCLUDEGRAPHICS_RE, _CUSTOM_IMG_RE):
        for match in pattern.finditer(tex):
            path = match.group(3)
            if "#" in path:
                continue
            cleaned = _clean_latex_path(path)
            if cleaned:
                paths.add(cleaned)
    return paths


def _build_asset_index(attachments_folder: str) -> dict[str, str]:
    """构建附件文件名与其绝对路径的索引表。"""

    index: dict[str, str] = {}
    if os.path.exists(attachments_folder):
        for root, _, files in os.walk(attachments_folder):
            for fname in files:
                index.setdefault(fname, os.path.join(root, fname))
    return index


def _find_resource_file(resources_folder: str, name: str) -> str | None:
    """在资源目录（含子目录）中查找指定文件。"""

    if not name:
        return None
    candidate = os.path.join(resources_folder, name)
    if os.path.exists(candidate):
        return candidate
    if os.path.exists(resources_folder):
        for root, _, files in os.walk(resources_folder):
            if name in files:
                return os.path.join(root, name)
    return None


def prepare_latex_assets(chunks: Iterable[str], attachments_folder: str, resources_folder: str, *dest_dirs: str) -> None:
    """根据 LaTeX 文本复制引用到目标目录，确保编译所需资源齐备。"""

    needed: set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, str) and chunk:
            needed.update(_extract_graphics_paths(chunk))
    if not needed or not dest_dirs:
        return

    assets = _build_asset_index(attachments_folder)
    for dest in dest_dirs:
        os.makedirs(dest, exist_ok=True)

    for name in needed:
        if not name or "#" in name:
            continue
        src = assets.get(name) or _find_resource_file(resources_folder, name)
        if not src or not os.path.exists(src):
            continue
        for dest in dest_dirs:
            dst = os.path.join(dest, name)
            try:
                if os.path.exists(dst):
                    try:
                        if os.path.samefile(src, dst):
                            continue
                    except OSError:
                        pass
                shutil.copy2(src, dst)
            except Exception as exc:  # pragma: no cover - best effort logging
                print(f"复制资源失败 {name}: {exc}")


__all__ = [
    "normalize_latex_content",
    "prepare_latex_assets",
    "_find_resource_file",
]
