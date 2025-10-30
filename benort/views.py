"""封装 Flask 路由的蓝图，提供前端交互所需的全部接口。"""

import base64
import copy
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import uuid
import zipfile
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urlparse, unquote

import requests
from flask import Blueprint, current_app, jsonify, render_template, request, send_file, send_from_directory
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from werkzeug.utils import secure_filename

from .latex import normalize_latex_content, prepare_latex_assets, _find_resource_file
from .project_store import (
    ProjectLockedError,
    _store_attachment_file,
    clear_project_password,
    create_project,
    delete_project,
    get_project_cookie_name,
    get_project_from_request,
    get_project_metadata,
    get_project_password_hash,
    get_project_paths,
    get_projects_root,
    get_project_learn_path,
    is_safe_project_name,
    issue_project_cookie,
    list_projects,
    load_learning_data,
    load_project,
    rename_project,
    save_learning_data,
    save_project,
    set_project_password,
    verify_project_password,
)
from .oss_client import (
    delete_file as oss_delete_file,
    is_configured as oss_is_configured,
    list_files as oss_list_files,
    sync_directory as oss_sync_directory,
    upload_file as oss_upload_file,
)
from .config import (
    AI_BIB_PROMPT,
    AI_PROMPTS,
    COMPONENT_LIBRARY,
    LEARNING_ASSISTANT_DEFAULT_PROMPTS,
    UI_THEME,
    OPENAI_CHAT_COMPLETIONS_MODEL,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_RESPONSE_FORMAT,
    OPENAI_TTS_SPEED,
    OPENAI_TTS_VOICE,
)
from .template_store import get_default_header, get_default_template, list_templates
from .template_store import get_default_markdown_template
from .responses import api_error, api_success

try:  # pragma: no cover - optional dependency
    from pdf2image import convert_from_path  # type: ignore
except Exception:  # pragma: no cover - graceful degradation
    convert_from_path = None


_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE)

_LATEX_INCLUDE_RE = re.compile(r"\\includegraphics(?:\[[^]]*])?\{([^}]+)\}")
_LATEX_IMG_RE = re.compile(r"\\img(?:\[[^]]*])?\{([^}]+)\}")
_LATEX_HREF_RE = re.compile(r"\\href\{([^}]+)\}")
_LATEX_URL_RE = re.compile(r"\\url\{([^}]+)\}")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_HTML_SRC_RE = re.compile(r'\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_HTML_HREF_RE = re.compile(r'\bhref=["\']([^"\']+)["\']', re.IGNORECASE)

_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.heic', '.heif'}

_MARKDOWN_RENDERER = (
    MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
    .use(tasklists_plugin, enabled=True, label=True)
    .use(footnote_plugin)
)

_DEFAULT_MARKDOWN_EXPORT_STYLE = """
:root {
  color-scheme: light dark;
}
body.markdown-export {
  margin: 0;
  padding: 48px 24px;
  background: #0b1120;
  color: #e2e8f0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
  line-height: 1.7;
}
body.markdown-export.theme-light {
  background: #f8fafc;
  color: #0f172a;
}
body.markdown-export .markdown-export-content {
  max-width: min(960px, 100%);
  margin: 0 auto;
}
body.markdown-export a {
  color: #93c5fd;
  text-decoration: none;
}
body.markdown-export.theme-light a {
  color: #2563eb;
}
body.markdown-export a:hover {
  text-decoration: underline;
}
body.markdown-export pre {
  overflow-x: auto;
}
body.markdown-export img {
  max-width: 100%;
  height: auto;
}
"""

_DEFAULT_MATHJAX_EXPORT_SNIPPET = """<script>
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true
  },
  svg: { fontCache: 'global' }
};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" defer></script>
"""

_DEFAULT_HIGHLIGHT_EXPORT_SNIPPET = """<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
window.addEventListener('DOMContentLoaded', function(){
  if (window.hljs && typeof window.hljs.highlightAll === 'function') {
    window.hljs.highlightAll();
  }
});
</script>
"""


def _safe_join(base: str, relative: str) -> Optional[str]:
    """Safely join a relative path to a base directory."""

    if not relative:
        return None
    normalized_base = os.path.abspath(base)
    candidate = os.path.abspath(os.path.join(base, relative))
    try:
        common = os.path.commonpath([normalized_base, candidate])
    except ValueError:
        return None
    if common != normalized_base:
        return None
    return candidate


def _resolve_local_asset_path(
    src: str,
    project_name: str,
    attachments_folder: str,
    resources_folder: str,
) -> Optional[str]:
    """Resolve a local image URL to an absolute filesystem path."""

    if not src:
        return None
    cleaned = unquote(str(src).strip())
    if not cleaned:
        return None
    cleaned = cleaned.split("#", 1)[0].split("?", 1)[0]
    parsed = urlparse(cleaned)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    path = parsed.path or cleaned
    trimmed = path.lstrip("/")
    project_prefix = f"projects/{project_name}/"
    if trimmed.startswith(project_prefix):
        trimmed = trimmed[len(project_prefix) :]
    trimmed = trimmed.lstrip("/")
    if trimmed.startswith("uploads/"):
        rel = trimmed[len("uploads/") :]
        return _safe_join(attachments_folder, rel)
    if trimmed.startswith("resources/"):
        rel = trimmed[len("resources/") :]
        return _safe_join(resources_folder, rel)
    candidate = _safe_join(attachments_folder, trimmed)
    if candidate and os.path.exists(candidate):
        return candidate
    candidate = _safe_join(resources_folder, trimmed)
    if candidate and os.path.exists(candidate):
        return candidate
    return None


def _load_image_bytes(
    src: str,
    project_name: str,
    attachments_folder: str,
    resources_folder: str,
) -> tuple[Optional[bytes], Optional[str]]:
    """Fetch image content and detect its MIME type."""

    if not src or src.startswith("data:"):
        return None, None
    cleaned = src.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"}:
        try:
            response = requests.get(cleaned, timeout=10)
            response.raise_for_status()
        except Exception:
            return None, None
        mime_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip() or None
        return response.content, mime_type
    local_path = _resolve_local_asset_path(cleaned, project_name, attachments_folder, resources_folder)
    if local_path and os.path.exists(local_path):
        try:
            with open(local_path, "rb") as fh:
                content = fh.read()
        except OSError:
            return None, None
        mime_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        return content, mime_type
    return None, None


def _enhance_markdown_soup(
    html: str,
    project_name: str,
    attachments_folder: str,
    resources_folder: str,
) -> BeautifulSoup:
    """Apply export-specific enhancements to rendered Markdown HTML."""

    soup = BeautifulSoup(html, "html.parser")

    for pre in soup.find_all("pre"):
        code_block = pre.find("code", recursive=False)
        if not code_block:
            continue
        code_classes = list(code_block.get("class") or [])
        if "hljs" not in code_classes:
            code_classes.append("hljs")
        code_block["class"] = code_classes
        pre_classes = list(pre.get("class") or [])
        if "hljs" not in pre_classes:
            pre_classes.append("hljs")
        pre["class"] = pre_classes

    for img in soup.find_all("img"):
        src = img.get("src") or ""
        content, mime_type = _load_image_bytes(src, project_name, attachments_folder, resources_folder)
        if content and mime_type:
            encoded = base64.b64encode(content).decode("ascii")
            img["src"] = f"data:{mime_type};base64,{encoded}"
        classes = set(img.get("class") or [])
        classes.add("markdown-preview-image")
        img["class"] = list(classes)
        if not img.has_attr("loading"):
            img["loading"] = "lazy"
        if not img.has_attr("decoding"):
            img["decoding"] = "async"

    return soup


def _build_markdown_export_html(
    markdown_text: str,
    template: dict,
    project_name: str,
    attachments_folder: str,
    resources_folder: str,
) -> str:
    """Render Markdown text and wrap it with styling suitable for export."""

    rendered_html = _MARKDOWN_RENDERER.render(markdown_text or "")
    soup = _enhance_markdown_soup(rendered_html, project_name, attachments_folder, resources_folder)
    body_html = "".join(str(child) for child in soup.contents)

    wrapper_classes = ["markdown-preview-content", "markdown-export-content"]
    wrapper_extra = str(template.get("wrapperClass") or "").strip()
    if wrapper_extra:
        wrapper_classes.extend(wrapper_extra.split())
    wrapper_class_attr = " ".join(dict.fromkeys(wrapper_classes))

    css = str(template.get("css") or "")
    custom_head = str(template.get("customHead") or "")
    include_mathjax = "mathjax" not in custom_head.lower()
    mathjax_head = _DEFAULT_MATHJAX_EXPORT_SNIPPET if include_mathjax else ""
    include_highlight = "highlight" not in custom_head.lower() and "hljs" not in custom_head.lower()
    highlight_head = _DEFAULT_HIGHLIGHT_EXPORT_SNIPPET if include_highlight else ""

    color_mode = "light"
    if isinstance(UI_THEME, dict):
        color_mode = str(UI_THEME.get("color_mode", "light") or "light").lower()
        if color_mode not in {"light", "dark"}:
            color_mode = "light"

    body_classes = ["markdown-export", f"theme-{color_mode}"]
    body_attr = f'class="{" ".join(body_classes)}" data-theme="{color_mode}"'

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Markdown 预览导出</title>
  <style>
{_DEFAULT_MARKDOWN_EXPORT_STYLE}
  </style>
  <style>
{css}
  </style>
  {mathjax_head if mathjax_head else ""}
  {highlight_head if highlight_head else ""}
  {custom_head}
</head>
<body {body_attr}>
  <div class="{wrapper_class_attr}">
{body_html}
  </div>
</body>
</html>
"""
    return document


def _normalize_link_target(value: object) -> str:
    """Clean up a link or path and return a comparable string."""

    if value is None:
        return ""
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("\\", "/")
    cleaned = cleaned.split("?", 1)[0]
    cleaned = cleaned.split("#", 1)[0]
    return cleaned.strip()


def _normalize_resource_path(value: str) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().replace("\\", "/")
    cleaned = re.sub(r"/+", "/", cleaned)
    cleaned = cleaned.lstrip("/")
    if cleaned in {"", ".", ".."}:
        return ""
    parts: list[str] = []
    for segment in cleaned.split("/"):
        if segment in {"", ".", ".."}:
            continue
        sanitized = secure_filename(segment)
        if not sanitized:
            continue
        parts.append(sanitized)
    return "/".join(parts)


def _collect_resource_usage(project: dict) -> dict[str, dict[str, object]]:
    """Map resource filenames to page/global references."""


    usage: dict[str, dict[str, object]] = {}
    pages = project.get("pages", [])
    if isinstance(pages, list):
        for idx, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            for res_name in page.get("resources", []) or []:
                if not isinstance(res_name, str):
                    continue
                normalized = _normalize_resource_path(res_name)
                if not normalized:
                    continue
                entry = usage.setdefault(normalized, {"pages": set(), "global": False})
                entry["pages"].add(idx)
    global_resources = project.get("resources", [])
    if isinstance(global_resources, list):
        for res_name in global_resources:
            if not isinstance(res_name, str):
                continue
            normalized = _normalize_resource_path(res_name)
            if not normalized:
                continue
            entry = usage.setdefault(normalized, {"pages": set(), "global": False})
            entry["global"] = True
    return usage


def _collect_attachment_references(project: dict) -> dict[str, list[str]]:
    """Scan project content and gather attachment usage contexts."""

    usage: dict[str, set[str]] = {}

    def _register(raw: object, context: str):
        cleaned = _normalize_link_target(raw)
        if not cleaned:
            return
        base = os.path.basename(cleaned)
        if not base:
            return
        usage.setdefault(base, set()).add(context)

    def _scan_text(text: object, context: str):
        if text is None:
            return
        content = str(text)
        if not content:
            return
        for pattern in (_LATEX_INCLUDE_RE, _LATEX_IMG_RE, _LATEX_HREF_RE, _LATEX_URL_RE):
            for match in pattern.finditer(content):
                _register(match.group(1), context)
        for match in _MARKDOWN_LINK_RE.finditer(content):
            _register(match.group(1), context)
        for match in _HTML_SRC_RE.finditer(content):
            _register(match.group(1), context)
        for match in _HTML_HREF_RE.finditer(content):
            _register(match.group(1), context)

    pages = project.get("pages", [])
    if isinstance(pages, list):
        for idx, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            _scan_text(page.get("content", ""), f"第{idx + 1}页内容")
            _scan_text(page.get("notes", ""), f"第{idx + 1}页笔记")
            _scan_text(page.get("script", ""), f"第{idx + 1}页讲稿")
            for entry in page.get("bib", []) or []:
                if isinstance(entry, dict):
                    _scan_text(entry.get("entry", ""), f"第{idx + 1}页参考文献")
                else:
                    _scan_text(entry, f"第{idx + 1}页参考文献")

    template = project.get("template", {})
    if isinstance(template, dict):
        _scan_text(template.get("header"), "模板 header")
        _scan_text(template.get("beforePages"), "模板 beforePages")
        _scan_text(template.get("footer"), "模板 footer")

    md_template = project.get("markdownTemplate", {})
    if isinstance(md_template, dict):
        _scan_text(md_template.get("css"), "Markdown 模板 CSS")
        _scan_text(md_template.get("wrapperClass"), "Markdown 模板 wrapperClass")
        _scan_text(md_template.get("customHead"), "Markdown 模板自定义头部")

    global_bib = project.get("bib", [])
    if isinstance(global_bib, list):
        for idx, entry in enumerate(global_bib):
            if isinstance(entry, dict):
                _scan_text(entry.get("entry", ""), f"全局参考文献 {idx + 1}")
            else:
                _scan_text(entry, f"全局参考文献 {idx + 1}")

    return {name: sorted(contexts) for name, contexts in usage.items()}


_DEFAULT_LEARNING_SYSTEM_MESSAGE = (
    "You are a knowledgeable bilingual tutor. "
    "Provide thorough, structured explanations in Markdown. "
    "Use LaTeX math when appropriate, and include actionable study advice."
)


def _truncate_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    snippet = text[:limit].rstrip()
    return f"{snippet}\n\n…（内容较长，已截断）"


def _merge_learning_prompts(project_name: str) -> tuple[list[dict], dict]:
    data = load_learning_data(project_name)
    prompts_meta = data.get("prompts", {})
    overrides = {item["id"]: item for item in prompts_meta.get("overrides", [])}
    removed = set(prompts_meta.get("removed", []))

    combined: list[dict] = []
    for default_prompt in LEARNING_ASSISTANT_DEFAULT_PROMPTS:
        prompt_id = default_prompt["id"]
        if prompt_id in removed:
            continue
        prompt = copy.deepcopy(default_prompt)
        override = overrides.get(prompt_id)
        if override:
            for key in ("name", "description", "template", "system"):
                if override.get(key):
                    prompt[key] = override[key]
        prompt["source"] = "default"
        prompt["allowDelete"] = True
        combined.append(prompt)

    for custom_prompt in prompts_meta.get("custom", []):
        prompt = copy.deepcopy(custom_prompt)
        prompt["source"] = "custom"
        prompt["allowDelete"] = True
        combined.append(prompt)

    combined.sort(key=lambda item: (0 if item.get("source") == "default" else 1, item.get("name", "")))
    return combined, data


def _find_learning_prompt(prompts: list[dict], prompt_id: str) -> Optional[dict]:
    for prompt in prompts:
        if prompt.get("id") == prompt_id:
            return prompt
    return None


def _format_learning_user_message(template: str, content: str, context: str) -> str:
    base_template = template or "{content}\n\n上下文：\n{context}"
    safe_context = context or "（无额外上下文）"
    try:
        return base_template.format(content=content, context=safe_context)
    except KeyError:
        return f"{base_template}\n\n---\n{content}\n\n上下文：\n{safe_context}"


def _extract_json_object(text: str) -> dict:
    """尽量从模型输出中解析出 JSON 对象。"""

    if not text:
        return {}

    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        snippet = match.group(0)
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_reference_link(ref: str, link: Optional[str], doi: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """返回规范化后的链接和 DOI。"""

    found_doi = doi
    if not found_doi:
        match = _DOI_PATTERN.search(ref)
        if match:
            found_doi = match.group(0).strip().rstrip('.')

    normalized_link = link.strip() if link else ""
    if not normalized_link and _DOI_PATTERN.match(ref):
        normalized_link = f"https://doi.org/{ref.strip()}"
    if not normalized_link and ref.lower().startswith("http"):
        normalized_link = ref
    if not normalized_link and found_doi:
        normalized_link = f"https://doi.org/{found_doi}"

    return normalized_link or None, found_doi


_SEARCH_FIELD_LABELS = {
    "content": "LaTeX 内容",
    "notes": "Markdown 笔记",
    "script": "讲稿",
}


# 定义蓝图以便在应用工厂中一次性注册所有路由
bp = Blueprint("benort", __name__)

_LOCKED_ERROR = '项目已加密，请先解锁'


def _clean_text_for_excerpt(text: str) -> str:
    """粗略去除 LaTeX/Markdown 标记，生成更易读的摘要。"""

    if not text:
        return ""
    cleaned = re.sub(r"\\(begin|end)\{[^}]+\}", " ", text)
    cleaned = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?(?:\{[^}]*\})?", " ", cleaned)
    cleaned = re.sub(r"\$[^$]*\$", " ", cleaned)
    cleaned = re.sub(r"`{1,3}[^`]*`{1,3}", " ", cleaned)
    cleaned = re.sub(r"[*_#>-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _build_excerpt(text: str, start: int, match_len: int, radius: int = 60) -> str:
    """根据命中位置构建简短摘要。"""

    if not text:
        return ""
    start = max(start, 0)
    if match_len <= 0:
        match_len = 1
    end = min(len(text), start + match_len)
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = text[left:right].replace('\n', ' ')
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if left > 0:
        snippet = '…' + snippet
    if right < len(text):
        snippet += '…'
    return snippet


def _extract_page_label(idx: int, page: dict) -> str:
    """尽量提取页面标题用于搜索结果展示。"""

    if not isinstance(page, dict):
        return f"第 {idx + 1} 页"

    content = page.get("content") or ""
    notes = page.get("notes") or ""
    script = page.get("script") or ""

    for pattern in (r"\\frametitle\{([^}]*)\}", r"\\section\{([^}]*)\}"):
        match = re.search(pattern, content)
        if match and match.group(1).strip():
            return _clean_text_for_excerpt(match.group(1).strip()) or f"第 {idx + 1} 页"

    note_title = re.search(r"^\s*#+\s+(.+)$", notes, flags=re.MULTILINE)
    if note_title and note_title.group(1).strip():
        return _clean_text_for_excerpt(note_title.group(1).strip()) or f"第 {idx + 1} 页"

    for raw in (content, notes, script):
        cleaned = _clean_text_for_excerpt(raw)
        if cleaned:
            return cleaned[:40]

    return f"第 {idx + 1} 页"


def _export_tts_audio_file(text: str, audio_folder: str, base_name: str, download_name: str, empty_error: str):
    """将讲稿文本转换为语音文件并返回下载响应。"""

    normalized = str(text or "")
    if not normalized.strip():
        return jsonify({"success": False, "error": empty_error}), 400

    os.makedirs(audio_folder, exist_ok=True)
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    audio_path = os.path.join(audio_folder, f"{base_name}.mp3")
    hash_path = os.path.join(audio_folder, f"{base_name}.hash")

    if os.path.exists(audio_path) and os.path.exists(hash_path):
        try:
            with open(hash_path, "r", encoding="utf-8") as hf:
                if hf.read().strip() == content_hash:
                    return send_file(
                        audio_path,
                        mimetype="audio/mpeg",
                        as_attachment=True,
                        download_name=download_name,
                    )
        except Exception:
            pass

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False, "error": "未设置OPENAI_API_KEY环境变量"}), 500

    try:
        resp = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_TTS_MODEL,
                "input": normalized,
                "voice": OPENAI_TTS_VOICE,
                "response_format": OPENAI_TTS_RESPONSE_FORMAT,
                "speed": OPENAI_TTS_SPEED,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            audio_bytes = resp.content
            try:
                with open(audio_path, "wb") as af:
                    af.write(audio_bytes)
                with open(hash_path, "w", encoding="utf-8") as hf:
                    hf.write(content_hash)
            except Exception as exc:
                print(f'写入音频失败: {exc}')
            return send_file(
                audio_path,
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name=download_name,
            )
        return jsonify({"success": False, "error": f"OpenAI TTS错误: {resp.text}"}), 500
    except Exception as exc:  # pragma: no cover - network errors
        return jsonify({"success": False, "error": str(exc)}), 500


def _collect_search_matches(pages, query: str, limit: int = 50):
    """在项目页内检索关键词，返回按命中次数排序的结果。"""

    matches = []
    if not query:
        return matches

    lowered_query = query.lower()
    tokens = [token.lower() for token in re.split(r"\s+", query) if token.strip()]

    for idx, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        for field, label in _SEARCH_FIELD_LABELS.items():
            raw = page.get(field)
            if not raw:
                continue
            text = str(raw)
            lowered_text = text.lower()

            positions = []
            if lowered_query:
                start = 0
                while True:
                    found = lowered_text.find(lowered_query, start)
                    if found == -1:
                        break
                    positions.append(found)
                    increment = len(lowered_query) or 1
                    start = found + increment

            if not positions and tokens:
                if all(token in lowered_text for token in tokens):
                    first_token = tokens[0]
                    pos = lowered_text.find(first_token)
                    if pos != -1:
                        positions.append(pos)

            if not positions:
                continue

            page_label = _extract_page_label(idx, page)
            excerpt_source = _build_excerpt(text, positions[0], len(query))
            excerpt = _clean_text_for_excerpt(excerpt_source) or excerpt_source

            matches.append({
                "pageIndex": idx,
                "pageLabel": page_label,
                "field": field,
                "fieldLabel": label,
                "matchCount": len(positions),
                "excerpt": excerpt,
                "position": positions[0],
                "matchLength": max(len(query), 1),
            })

    matches.sort(key=lambda item: (-item["matchCount"], item["pageIndex"]))
    return matches[:limit]


@bp.route("/")
def index():
    """渲染主编辑器页面，提供初始 UI。"""

    return render_template("editor.html", component_library=COMPONENT_LIBRARY, ui_theme=UI_THEME)


@bp.route("/export_audio", methods=["GET"])
def export_audio():
    """合并所有讲稿并调用 OpenAI TTS 生成整段音频。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    pages = project.get("pages", []) if isinstance(project, dict) else []
    scripts = [str(p.get("script", "")) for p in pages if isinstance(p, dict)]
    merged = "\n\n".join([n.strip() for n in scripts if n and n.strip()])

    project_name = get_project_from_request()
    _, _, _, _, build_folder = get_project_paths(project_name)
    audio_folder = os.path.join(build_folder, 'audio')

    return _export_tts_audio_file(merged, audio_folder, 'all_notes', 'all_notes.mp3', '没有可用的笔记内容')


@bp.route("/export_page_audio", methods=["GET"])
def export_page_audio():
    """为当前页讲稿生成语音并返回音频文件。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    page_number = request.args.get("page", type=int)
    if page_number is None:
        return jsonify({"success": False, "error": "缺少页码参数"}), 400

    pages = project.get("pages", []) if isinstance(project, dict) else []
    if not pages:
        return jsonify({"success": False, "error": "项目内没有幻灯片页"}), 404

    page_idx = page_number - 1
    if page_idx < 0 or page_idx >= len(pages):
        return jsonify({"success": False, "error": "指定页不存在"}), 404

    page = pages[page_idx]
    script = ""
    if isinstance(page, dict):
        script = str(page.get("script", ""))
    else:
        script = str(page)

    project_name = get_project_from_request()
    _, _, _, _, build_folder = get_project_paths(project_name)
    audio_folder = os.path.join(build_folder, 'audio')
    base_name = f'page_{page_idx + 1}_script'
    download_name = f'{base_name}.mp3'

    return _export_tts_audio_file(script, audio_folder, base_name, download_name, '当前页没有讲稿内容')


@bp.route("/add_page", methods=["POST"])
def add_page():
    """在指定位置插入一张默认幻灯片。"""

    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    if "pages" not in project or not isinstance(project["pages"], list):
        project["pages"] = []
    idx = int(request.json.get("idx", len(project["pages"])))
    project["pages"].insert(
        idx,
        {
            "content": "\\begin{frame}\n...\n\\end{frame}",
            "script": "",
            "notes": "",
            "bib": [],
        },
    )
    save_project(project)
    return jsonify({"success": True, "pages": project["pages"]})


def _compile_single_page_pdf(page_idx: int) -> tuple[int, dict]:
    """编译指定页为 PDF，并返回状态码与信息。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return 401, {"error": _LOCKED_ERROR}

    template = project.get("template") if isinstance(project, dict) else {}
    pages = project.get("pages", []) if isinstance(project, dict) else []

    if not isinstance(page_idx, int) or page_idx < 0:
        return 400, {"error": "页索引无效"}

    if not pages or page_idx >= len(pages):
        return 404, {"error": "指定页不存在"}

    project_name = get_project_from_request()
    attachments_folder, pdf_folder, _, resources_folder, build_folder = get_project_paths(project_name)

    default_template = get_default_template()
    default_header = default_template.get("header", get_default_header())
    default_before = default_template.get("beforePages", "\\begin{document}")
    default_footer = default_template.get("footer", "\\end{document}")

    if isinstance(template, dict):
        header = normalize_latex_content(template.get("header", default_header), attachments_folder, resources_folder)
        before = normalize_latex_content(template.get("beforePages", default_before), attachments_folder, resources_folder)
        footer = normalize_latex_content(template.get("footer", default_footer), attachments_folder, resources_folder)
    else:
        header = normalize_latex_content(str(template) if template else default_header, attachments_folder, resources_folder)
        before = normalize_latex_content(default_before, attachments_folder, resources_folder)
        footer = normalize_latex_content(default_footer, attachments_folder, resources_folder)

    page = pages[page_idx]
    raw = page["content"] if isinstance(page, dict) else str(page)
    page_tex = normalize_latex_content(raw or "", attachments_folder, resources_folder)

    tex = f"{header}\n{before}\n{page_tex}\n{footer}\n"
    filename = f"slide_page_{page_idx + 1}.tex"
    tex_path = os.path.join(build_folder, filename)
    pdf_name = f"slide_page_{page_idx + 1}.pdf"
    pdf_path = os.path.join(pdf_folder, pdf_name)
    prepare_latex_assets([header, before, page_tex, footer], attachments_folder, resources_folder, build_folder, pdf_folder)

    try:
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write(tex)
    except OSError as exc:
        return 500, {"error": f"写入临时 TeX 文件失败: {exc}"}

    try:
        result = subprocess.run(
            ["xelatex", "-output-directory", pdf_folder, filename],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=build_folder,
        )
    except Exception as exc:
        return 500, {"error": str(exc)}

    if result.returncode != 0:
        return 500, {"error": result.stderr or "xelatex 编译失败"}

    return 200, {"pdf": pdf_name, "path": pdf_path}


@bp.route("/compile_page", methods=["POST"])
def compile_page():
    """将单页 LaTeX 组合模板后用 xelatex 编译为 PDF。"""

    data = request.json or {}
    page_idx = int(data.get("page", 0))
    status, payload = _compile_single_page_pdf(page_idx)
    if status != 200:
        return jsonify({"success": False, "error": payload.get("error", "编译失败")}), status
    return jsonify({"success": True, "pdf": payload["pdf"]})


@bp.route("/export_page_pdf", methods=["GET"])
def export_page_pdf():
    """导出当前页的 PDF 文件。"""

    page_param = request.args.get("page", type=int)
    page_idx = max((page_param or 1) - 1, 0)
    status, payload = _compile_single_page_pdf(page_idx)
    if status != 200:
        return jsonify({"success": False, "error": payload.get("error", "导出失败")}), status

    pdf_path = payload.get("path")
    if not pdf_path or not os.path.exists(pdf_path):
        return jsonify({"success": False, "error": "PDF 文件不存在"}), 500

    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=payload["pdf"],
    )


@bp.route("/export_page_notes", methods=["GET"])
def export_page_notes():
    """导出当前页的 Markdown 笔记。"""

    page_param = request.args.get("page", type=int)
    page_idx = max((page_param or 1) - 1, 0)

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    pages = project.get("pages", []) if isinstance(project, dict) else []
    if not pages or page_idx >= len(pages):
        return jsonify({"success": False, "error": "指定页不存在"}), 404

    page = pages[page_idx]
    notes = ""
    if isinstance(page, dict):
        notes = page.get("notes", "") or ""
    else:  # pragma: no cover - 容错兜底
        notes = str(page or "")

    if not notes.strip():
        return jsonify({"success": False, "error": "当前页没有笔记可导出"}), 400

    buffer = io.BytesIO(notes.encode("utf-8"))
    download_name = f"page_{page_idx + 1}_notes.md"
    return send_file(
        buffer,
        mimetype="text/markdown",
        as_attachment=True,
        download_name=download_name,
    )


@bp.route("/export_page_markdown_html", methods=["GET"])
def export_page_markdown_html():
    """导出当前页 Markdown 渲染后的 HTML（内联图片）。"""

    page_param = request.args.get("page", type=int)
    page_idx = max((page_param or 1) - 1, 0)

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    pages = project.get("pages", []) if isinstance(project, dict) else []
    if not pages or page_idx >= len(pages):
        return jsonify({"success": False, "error": "指定页不存在"}), 404

    page = pages[page_idx]
    notes = ""
    if isinstance(page, dict):
        notes = page.get("notes", "") or ""
    else:  # pragma: no cover - 容错兜底
        notes = str(page or "")

    if not notes.strip():
        return jsonify({"success": False, "error": "当前页没有笔记可导出"}), 400

    project_name = get_project_from_request()
    attachments_folder, _, _, resources_folder, _ = get_project_paths(project_name)

    default_template = get_default_markdown_template()
    template: dict = {
        "css": default_template.get("css", ""),
        "wrapperClass": default_template.get("wrapperClass", ""),
    }
    if default_template.get("customHead"):
        template["customHead"] = default_template["customHead"]

    raw_template = project.get("markdownTemplate") if isinstance(project, dict) else None
    if isinstance(raw_template, dict):
        css_value = raw_template.get("css")
        if isinstance(css_value, str):
            template["css"] = css_value
        wrapper_value = raw_template.get("wrapperClass")
        if isinstance(wrapper_value, str):
            template["wrapperClass"] = wrapper_value
        head_value = raw_template.get("customHead")
        if isinstance(head_value, str) and head_value.strip():
            template["customHead"] = head_value
        elif "customHead" in template and not template["customHead"]:
            template.pop("customHead")

    html_output = _build_markdown_export_html(
        notes,
        template,
        project_name,
        attachments_folder,
        resources_folder,
    )

    buffer = io.BytesIO(html_output.encode("utf-8"))
    download_name = f"page_{page_idx + 1}_notes.html"
    return send_file(
        buffer,
        mimetype="text/html",
        as_attachment=True,
        download_name=download_name,
    )


@bp.route("/page_pdf/<int:page>")
def get_page_pdf(page: int):
    """返回单页 PDF 结果，用于前端预览。"""

    project_name = get_project_from_request()
    _, pdf_folder, _, _, _ = get_project_paths(project_name)
    pdf_name = f"slide_page_{page}.pdf"
    pdf_path = os.path.join(pdf_folder, pdf_name)
    if not os.path.exists(pdf_path):
        return "", 404
    return send_from_directory(pdf_folder, pdf_name)


@bp.route("/export_tex")
def export_tex():
    """导出全量幻灯片的 LaTeX 文本。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    template = project.get("template")
    pages = project.get("pages", [])
    project_name = get_project_from_request()
    attachments_folder, _, _, resources_folder, build_folder = get_project_paths(project_name)

    default_template = get_default_template()
    default_header = default_template.get("header", get_default_header())
    default_before = default_template.get("beforePages", "\\begin{document}")
    default_footer = default_template.get("footer", "\\end{document}")

    if isinstance(template, dict):
        header = normalize_latex_content(template.get("header", default_header), attachments_folder, resources_folder)
        before = normalize_latex_content(template.get("beforePages", default_before), attachments_folder, resources_folder)
        footer = normalize_latex_content(template.get("footer", default_footer), attachments_folder, resources_folder)
    else:
        header = normalize_latex_content(str(template) if template else default_header, attachments_folder, resources_folder)
        before = normalize_latex_content(default_before, attachments_folder, resources_folder)
        footer = normalize_latex_content(default_footer, attachments_folder, resources_folder)

    # 拼接所有页面内容
    tex_body = "\n".join([
        normalize_latex_content(p["content"] if isinstance(p, dict) else str(p), attachments_folder, resources_folder)
        for p in pages
    ])
    tex = f"{header}\n{before}\n{tex_body}\n{footer}\n"
    out_path = os.path.join(build_folder, "exported_full.tex")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(tex)
    return send_from_directory(build_folder, "exported_full.tex", as_attachment=True)


@bp.route("/export_notes", methods=["GET"])
def export_notes():
    """导出分页备注为 Markdown 文档。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    pages = project.get("pages", [])
    notes_list = []
    for idx, page in enumerate(pages, start=1):
        if isinstance(page, dict):
            note = page.get("notes", "") or ""
            notes_list.append(f"{note}\n")

    markdown = "\n\n".join([note for note in notes_list if note.strip()])
    if not markdown:
        return jsonify({"success": False, "error": "没有笔记可导出"}), 400
    return send_file(
        io.BytesIO(markdown.encode("utf-8")),
        mimetype="text/markdown",
        as_attachment=True,
        download_name="notes.md",
    )


@bp.route("/project", methods=["GET"])
def get_project_api():
    """返回当前项目的完整 JSON 描述。"""

    try:
        data = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    return jsonify(data)


@bp.route("/search", methods=["POST"])
def search_project_content():
    """检索当前项目的 LaTeX、笔记与讲稿内容。"""

    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or payload.get("q") or "").strip()
    if not query:
        return jsonify({"success": True, "query": "", "total": 0, "matches": []})

    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    pages = project.get("pages", []) if isinstance(project, dict) else []
    try:
        limit = int(payload.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    matches = _collect_search_matches(pages, query, limit)
    return jsonify({
        "success": True,
        "query": query,
        "total": len(matches),
        "matches": matches,
        "pages": len(pages),
    })


@bp.route("/upload_image", methods=["POST"])
def upload_image():
    """上传图片至附件目录并返回可访问链接。"""

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part"})
    file_storage = request.files["file"]
    project_name = get_project_from_request()
    try:
        result = _store_attachment_file(file_storage, project_name)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    return jsonify(
        {
            "success": True,
            "url": result.preferred_url,
            "filename": result.filename,
            "localUrl": result.local_url,
            "ossUrl": result.oss_url,
        }
    )


@bp.route("/attachments/upload", methods=["POST"])
def upload_attachment():
    """上传任意附件文件并返回文件信息。"""

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part"})
    file_storage = request.files["file"]
    project_name = get_project_from_request()
    try:
        result = _store_attachment_file(file_storage, project_name)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    return jsonify(
        {
            "success": True,
            "filename": result.filename,
            "url": result.preferred_url,
            "localUrl": result.local_url,
            "ossUrl": result.oss_url,
        }
    )


@bp.route("/mobile/attachments/upload", methods=["POST"])
def mobile_upload_attachment():
    """移动端附件上传，支持 PDF 转图片并返回插入片段。"""

    if "file" not in request.files:
        return api_error("No file part", 400)
    file_storage = request.files["file"]
    if not file_storage or not file_storage.filename:
        return api_error("No selected file", 400)

    project_name = get_project_from_request()
    try:
        project_data = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    os.makedirs(attachments_folder, exist_ok=True)

    try:
        stored = _store_attachment_file(file_storage, project_name)
    except ValueError as exc:
        return api_error(str(exc), 400)

    filename = stored.filename
    full_path = os.path.join(attachments_folder, filename)
    _, ext = os.path.splitext(filename)
    ext_lower = ext.lower()

    attachments_info = [
        {
            "name": filename,
            "localUrl": stored.local_url,
            "ossUrl": stored.oss_url,
            "preferredUrl": stored.preferred_url,
        }
    ]
    snippet = ""
    message = ""
    message_level = "success"

    if ext_lower == ".pdf":
        base_name = os.path.splitext(filename)[0]
        converted_files, error = _convert_pdf_to_images(full_path, attachments_folder, base_name)
        if converted_files:
            generated_infos = []
            for generated in converted_files:
                generated_path = os.path.join(attachments_folder, generated)
                generated_infos.append(
                    _prepare_attachment_payload(project_data, project_name, generated, generated_path)
                )
            attachments_info.extend(generated_infos)
            snippet_lines = []
            for info in generated_infos:
                url = info.get("preferredUrl") or info.get("localUrl") or info.get("ossUrl")
                if url:
                    snippet_lines.append(f"![{info.get('name', '')}]({url})")
            if snippet_lines:
                snippet = "\n".join(snippet_lines) + "\n"
            message = f"PDF 已转换为 {len(generated_infos)} 张图片并插入引用"
        else:
            url = stored.preferred_url or stored.local_url or stored.oss_url
            if url:
                snippet = f"[{filename}]({url})\n"
            if error:
                try:
                    current_app.logger.warning('PDF 转图片失败 %s: %s', filename, error)
                except Exception:
                    pass
                message = f"PDF 转图片失败：{error}"
                message_level = "warning"
            else:
                message = "PDF 转图片失败"
                message_level = "warning"
    else:
        url = stored.preferred_url or stored.local_url or stored.oss_url
        if url:
            if ext_lower in _IMAGE_EXTENSIONS:
                snippet = f"![{filename}]({url})\n"
            else:
                snippet = f"[{filename}]({url})\n"
        message = "附件已上传并插入引用"

    payload = {
        "attachments": attachments_info,
        "snippet": snippet,
    }
    if message:
        payload["message"] = message
        payload["level"] = message_level
    return api_success(payload)


@bp.route("/project", methods=["POST"])
def save_project_api():
    """持久化前端传入的项目数据。"""

    data = request.json or {}
    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    for key, value in data.items():
        project[key] = value
    if data.get("project"):
        pass
    save_project(project)
    return jsonify({"success": True})


@bp.route("/compile", methods=["POST"])
def compile_tex():
    """将全部页面合成模板后编译为整册 PDF。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    template = project.get("template")
    pages = project.get("pages", [])
    project_name = get_project_from_request()
    attachments_folder, pdf_folder, _, resources_folder, build_folder = get_project_paths(project_name)

    page_texts = []
    for page in pages:
        raw = page["content"] if isinstance(page, dict) else str(page)
        page_texts.append(normalize_latex_content(raw, attachments_folder, resources_folder))

    default_template = get_default_template()
    default_header = default_template.get("header", get_default_header())
    default_before = default_template.get("beforePages", "\\begin{document}")
    default_footer = default_template.get("footer", "\\end{document}")

    if isinstance(template, dict):
        header = normalize_latex_content(template.get("header", default_header), attachments_folder, resources_folder)
        before = normalize_latex_content(template.get("beforePages", default_before), attachments_folder, resources_folder)
        footer = normalize_latex_content(template.get("footer", default_footer), attachments_folder, resources_folder)
    else:
        header = normalize_latex_content(str(template) if template else default_header, attachments_folder, resources_folder)
        before = normalize_latex_content(default_before, attachments_folder, resources_folder)
        footer = normalize_latex_content(default_footer, attachments_folder, resources_folder)

    tex_body = "\n".join(page_texts)
    tex = header + "\n" + before + "\n" + tex_body + "\n" + footer + "\n"
    filename = secure_filename("slide.tex")
    tex_path = os.path.join(build_folder, filename)
    pdf_name = "slide.pdf"
    prepare_latex_assets([header, before, tex_body, footer], attachments_folder, resources_folder, build_folder, pdf_folder)

    with open(tex_path, "w", encoding="utf-8") as fh:
        fh.write(tex)
    try:
        result = subprocess.run(
            ["xelatex", "-output-directory", pdf_folder, filename],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=build_folder,
        )
        if result.returncode != 0:
            return jsonify({"success": False, "error": result.stderr})
        return jsonify({"success": True, "pdf": pdf_name})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@bp.route("/tts", methods=["POST"])
def tts_api():
    """文本转语音 API，返回 base64 编码的 MP3。"""

    data = request.json or {}
    text = data.get("text", "")
    if not str(text).strip():
        return jsonify({"success": False, "error": "文本为空"}), 400

    project_name = get_project_from_request()
    _, _, _, _, build_folder = get_project_paths(project_name)
    audio_folder = os.path.join(build_folder, 'audio')
    os.makedirs(audio_folder, exist_ok=True)
    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    audio_path = os.path.join(audio_folder, f'tts_{text_hash}.mp3')

    if os.path.exists(audio_path):
        with open(audio_path, 'rb') as af:
            encoded = base64.b64encode(af.read()).decode('utf-8')
        return jsonify({"success": True, "audio": encoded, "cached": True})

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False, "error": "未设置OPENAI_API_KEY环境变量"}), 500

    try:
        resp = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_TTS_MODEL,
                "input": text,
                "voice": OPENAI_TTS_VOICE,
                "response_format": OPENAI_TTS_RESPONSE_FORMAT,
                "speed": OPENAI_TTS_SPEED,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            audio_bytes = resp.content
            try:
                with open(audio_path, 'wb') as af:
                    af.write(audio_bytes)
            except Exception as exc:  # pragma: no cover
                print(f'写入单段音频失败: {exc}')
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            return jsonify({"success": True, "audio": encoded})
        return jsonify({"success": False, "error": f"OpenAI TTS错误: {resp.text}"}), 500
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route("/ai_optimize", methods=["POST"])
def ai_optimize():
    """调用 OpenAI ChatCompletion 优化讲稿、笔记或 LaTeX 内容。"""

    data = request.json or {}
    script_text = data.get("script", "")
    opt_type = data.get("type", "latex")
    latex_text = data.get("latex", "")
    markdown_text = data.get("markdown", "")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return api_error("未设置OPENAI_API_KEY环境变量", 500)

    default_header = get_default_header()

    if opt_type == "script":
        prompt = AI_PROMPTS["script"]["template"].format(
            latex=latex_text,
            markdown=markdown_text or "（无笔记内容）",
            script=script_text,
        )
        system_prompt = AI_PROMPTS["script"]["system"]
    elif opt_type == "note":
        prompt = AI_PROMPTS["note"]["template"].format(
            latex=latex_text,
            markdown=markdown_text,
        )
        system_prompt = AI_PROMPTS["note"]["system"]
    else:
        project = load_project()
        template = project.get("template") if isinstance(project, dict) else {}
        if isinstance(template, dict):
            header_tex = template.get("header", default_header) or default_header
        elif isinstance(template, str):
            header_tex = template or default_header
        else:
            header_tex = default_header

        package_matches = re.findall(r"\\usepackage(?:\[[^]]*\])?\{([^}]*)\}", header_tex)
        allowed_packages: list[str] = []
        for match in package_matches:
            for pkg in match.split(','):
                pkg = pkg.strip()
                if pkg and pkg not in allowed_packages:
                    allowed_packages.append(pkg)
        if 'beamer' not in allowed_packages:
            allowed_packages.insert(0, 'beamer')

        custom_macros = re.findall(r"\\newcommand\{(\\[^}]+)\}", header_tex)
        custom_macro_list = ', '.join(custom_macros) if custom_macros else '无自定义命令'

        allowed_str = ", ".join(allowed_packages) if allowed_packages else "无可用宏包"
        prompt = AI_PROMPTS["latex"]["template"].format(
            latex=latex_text,
            allowed_packages=allowed_str,
            custom_macros=custom_macro_list,
            markdown=markdown_text or "（无笔记内容）",
        )
        system_prompt = AI_PROMPTS["latex"]["system"]

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_CHAT_COMPLETIONS_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"]
            return api_success({"result": result})
        return api_error(f"OpenAI API错误: {resp.text}", 500)
    except Exception as exc:  # pragma: no cover
        return api_error(str(exc), 500)


@bp.route("/ai_bib", methods=["POST"])
def ai_bib():
    """调用 OpenAI 获取文献的记忆标签与备注摘要。"""

    data = request.json or {}
    ref = str(data.get("ref", "")).strip()
    if not ref:
        return jsonify({"success": False, "error": "参考文献输入为空"}), 400

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False, "error": "未设置OPENAI_API_KEY环境变量"}), 500

    system_prompt = AI_BIB_PROMPT["system"]
    user_prompt = AI_BIB_PROMPT["user"].format(ref=ref)

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_CHAT_COMPLETIONS_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            timeout=60,
        )
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "error": str(exc)}), 500

    if resp.status_code != 200:
        return jsonify({"success": False, "error": f"OpenAI API错误: {resp.text}"}), 500

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover
        return jsonify({"success": False, "error": f"解析OpenAI响应失败: {exc}"}), 500

    parsed = _extract_json_object(content)
    if not parsed:
        parsed = {"note": content.strip() or ""}

    metadata = parsed.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    label = str(parsed.get("label") or "").strip()
    note = str(parsed.get("note") or parsed.get("summary") or "").strip()
    suggested_id = str(parsed.get("id") or "").strip()
    link = parsed.get("link") or parsed.get("url") or ""
    doi = parsed.get("doi") or metadata.get("doi") or ""

    link, doi = _normalize_reference_link(ref, str(link or "") or None, str(doi or "") or None)
    if doi:
        metadata.setdefault("doi", doi)

    bibtex = parsed.get("bibtex") or parsed.get("entry") or ""

    authors = parsed.get("authors")
    if authors and isinstance(authors, list):
        metadata.setdefault("authors", authors)
    venue = parsed.get("venue") or parsed.get("journal")
    if venue:
        metadata.setdefault("venue", venue)
    year = parsed.get("year") or parsed.get("date")
    if year:
        metadata.setdefault("year", year)
    resource_type = parsed.get("type")
    if resource_type:
        metadata.setdefault("type", resource_type)
    title = parsed.get("title")
    if title and "title" not in metadata:
        metadata["title"] = title

    if not label:
        if metadata.get("title"):
            label = str(metadata["title"]).strip()
        elif metadata.get("venue") and metadata.get("year"):
            label = f"{metadata['venue']} {metadata['year']}".strip()
        elif link:
            label = link.split("//")[-1][:50]
    if not label:
        label = suggested_id or "参考资料"

    label = label[:60]

    if not note:
        note = metadata.get("summary") or metadata.get("abstract") or ""
    note = note[:200]

    cite_id = suggested_id
    if not cite_id and doi:
        cite_id = re.sub(r"[^a-zA-Z0-9]+", "_", doi).strip("_")[:50]
    if not cite_id and label:
        cite_id = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_")[:50]
    if not cite_id:
        cite_id = f"ref{hashlib.md5((ref or label).encode('utf-8')).hexdigest()[:8]}"

    entry_payload = {
        "id": cite_id,
        "label": label,
        "note": note,
        "link": link,
        "entry": str(bibtex or ""),
        "metadata": metadata,
    }

    return jsonify({"success": True, "entry": entry_payload})


@bp.route("/learn/config", methods=["GET"])
def learn_config():
    project_name = get_project_from_request()
    prompts, _ = _merge_learning_prompts(project_name)
    return api_success({"prompts": prompts})


@bp.route("/learn/prompts", methods=["POST"])
def learn_create_prompt():
    data = request.json or {}
    name = str(data.get("name") or "").strip()
    template = str(data.get("template") or "").strip()
    if not name or not template:
        return api_error("name 和 template 必填", 400)
    description = str(data.get("description") or "").strip()
    system_text = str(data.get("system") or "").strip()

    project_name = get_project_from_request()
    learn_data = load_learning_data(project_name)
    prompts_meta = learn_data.setdefault("prompts", {"custom": [], "overrides": [], "removed": []})
    prompts_meta.setdefault("custom", [])
    prompts_meta.setdefault("overrides", [])
    prompts_meta.setdefault("removed", [])

    prompt_id = f"custom_{uuid.uuid4().hex[:12]}"
    entry = {"id": prompt_id, "name": name, "template": template}
    if description:
        entry["description"] = description
    if system_text:
        entry["system"] = system_text
    prompts_meta["custom"].append(entry)

    save_learning_data(project_name, learn_data)
    prompts, _ = _merge_learning_prompts(project_name)
    return api_success({"prompts": prompts, "createdId": prompt_id})


@bp.route("/learn/prompts/<prompt_id>", methods=["PUT"])
def learn_update_prompt(prompt_id: str):
    data = request.json or {}
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return api_error("prompt_id 无效", 400)

    name = data.get("name")
    template = data.get("template")
    description = data.get("description")
    system_text = data.get("system")

    if template is not None and not str(template).strip():
        return api_error("template 不能为空", 400)

    project_name = get_project_from_request()
    learn_data = load_learning_data(project_name)
    prompts_meta = learn_data.setdefault("prompts", {"custom": [], "overrides": [], "removed": []})
    custom_list = prompts_meta.setdefault("custom", [])
    overrides_list = prompts_meta.setdefault("overrides", [])
    removed_list = prompts_meta.setdefault("removed", [])

    target = None
    for item in custom_list:
        if item.get("id") == prompt_id:
            target = item
            break

    if target:
        if name is not None and str(name).strip():
            target["name"] = str(name).strip()
        if description is not None:
            desc_val = str(description).strip()
            if desc_val:
                target["description"] = desc_val
            else:
                target.pop("description", None)
        if system_text is not None:
            sys_val = str(system_text).strip()
            if sys_val:
                target["system"] = sys_val
            else:
                target.pop("system", None)
        if template is not None and str(template).strip():
            target["template"] = str(template)
    else:
        override = None
        for item in overrides_list:
            if item.get("id") == prompt_id:
                override = item
                break
        if override is None:
            override = {"id": prompt_id}
            overrides_list.append(override)
        if name is not None and str(name).strip():
            override["name"] = str(name).strip()
        if description is not None:
            desc_val = str(description).strip()
            if desc_val:
                override["description"] = desc_val
            else:
                override.pop("description", None)
        if system_text is not None:
            sys_val = str(system_text).strip()
            if sys_val:
                override["system"] = sys_val
            else:
                override.pop("system", None)
        if template is not None and str(template).strip():
            override["template"] = str(template)
        if prompt_id in removed_list:
            removed_list.remove(prompt_id)

    save_learning_data(project_name, learn_data)
    prompts, _ = _merge_learning_prompts(project_name)
    return api_success({"prompts": prompts})


@bp.route("/learn/prompts/<prompt_id>", methods=["DELETE"])
def learn_delete_prompt(prompt_id: str):
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return api_error("prompt_id 无效", 400)

    project_name = get_project_from_request()
    learn_data = load_learning_data(project_name)
    prompts_meta = learn_data.setdefault("prompts", {"custom": [], "overrides": [], "removed": []})
    custom_list = prompts_meta.setdefault("custom", [])
    overrides_list = prompts_meta.setdefault("overrides", [])
    removed_list = prompts_meta.setdefault("removed", [])

    removed_flag = False
    filtered = []
    for item in custom_list:
        if item.get("id") == prompt_id:
            removed_flag = True
            continue
        filtered.append(item)
    prompts_meta["custom"] = filtered

    if not removed_flag:
        overrides_list[:] = [item for item in overrides_list if item.get("id") != prompt_id]
        if prompt_id not in removed_list:
            removed_list.append(prompt_id)

    save_learning_data(project_name, learn_data)
    prompts, _ = _merge_learning_prompts(project_name)
    return api_success({"prompts": prompts})


@bp.route("/learn/query", methods=["POST"])
def learn_query():
    data = request.json or {}
    content = str(data.get("content") or "").strip()
    if not content:
        return api_error("学习内容不能为空", 400)
    context = str(data.get("context") or "").strip()
    prompt_id = str(data.get("promptId") or "").strip()
    prompt_name = str(data.get("promptName") or "").strip()

    project_name = get_project_from_request()
    prompts, _ = _merge_learning_prompts(project_name)

    if prompt_id and prompt_id != "__raw__":
        prompt = _find_learning_prompt(prompts, prompt_id)
        if not prompt:
            return api_error("提示词不存在", 404)
        prompt_name = prompt.get("name", prompt_id)
        system_text = prompt.get("system") or _DEFAULT_LEARNING_SYSTEM_MESSAGE
        template = prompt.get("template") or "{content}\n\n上下文：\n{context}"
    else:
        prompt_id = "__raw__"
        if not prompt_name:
            prompt_name = "无提示词"
        system_text = _DEFAULT_LEARNING_SYSTEM_MESSAGE
        template = (
            "以下是需要学习或解析的内容，请用结构化 Markdown 给出详细讲解与建议：\n"
            "{content}\n\n上下文信息：\n{context}"
        )

    truncated_content = _truncate_text(content, 4000)
    truncated_context = _truncate_text(context, 3000) or "（无额外上下文）"
    user_prompt = _format_learning_user_message(template, truncated_content, truncated_context)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return api_error("未设置OPENAI_API_KEY环境变量", 500)

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_CHAT_COMPLETIONS_MODEL,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
            },
            timeout=60,
        )
    except Exception as exc:  # pragma: no cover
        return api_error(str(exc), 500)

    if resp.status_code != 200:
        return api_error(f"OpenAI API错误: {resp.text}", 500)

    try:
        result = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover
        return api_error(f"解析 OpenAI 响应失败: {exc}", 500)

    return api_success({
        "result": result,
        "promptId": prompt_id,
        "promptName": prompt_name,
    })


@bp.route("/learn/record", methods=["POST"])
def learn_record():
    data = request.json or {}
    content = str(data.get("content") or "").strip()
    output = str(data.get("output") or "").strip()
    if not content or not output:
        return api_error("content 和 output 必填", 400)

    prompt_name = str(data.get("promptName") or "").strip() or "无提示词"
    prompt_id = str(data.get("promptId") or "").strip()
    context = str(data.get("context") or "").strip()

    project_name = get_project_from_request()
    learn_data = load_learning_data(project_name)
    prompts_meta = learn_data.setdefault("prompts", {"custom": [], "overrides": [], "removed": []})
    prompts_meta.setdefault("custom", [])
    prompts_meta.setdefault("overrides", [])
    prompts_meta.setdefault("removed", [])

    records = learn_data.setdefault("records", [])
    target = None
    for record in records:
        if record.get("input") == content:
            target = record
            break
    if target is None:
        target = {"input": content, "entries": []}
        records.append(target)
    if context:
        target["context"] = context

    entries = target.setdefault("entries", [])
    existing = None
    for entry in entries:
        entry_prompt_id = str(entry.get("promptId") or "").strip()
        if prompt_id and entry_prompt_id == prompt_id:
            existing = entry
            break
        if not prompt_id and not entry_prompt_id and entry.get("promptName") == prompt_name:
            existing = entry
            break

    timestamp = datetime.utcnow().isoformat() + "Z"
    if existing:
        existing["output"] = output
        existing["promptName"] = prompt_name
        existing["savedAt"] = timestamp
    else:
        entries.append({
            "id": uuid.uuid4().hex,
            "promptId": prompt_id,
            "promptName": prompt_name,
            "output": output,
            "savedAt": timestamp,
        })

    save_learning_data(project_name, learn_data)
    return api_success({"savedAt": timestamp})


@bp.route("/export_learn_project", methods=["GET"])
def export_learn_project():
    """导出学习助手的记录/配置文件。"""

    project_name = get_project_from_request()
    try:
        load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    learn_data = load_learning_data(project_name)
    save_learning_data(project_name, learn_data)
    learn_path = get_project_learn_path(project_name)
    if not os.path.exists(learn_path):
        return api_error("暂无学习记录", 404)

    download_name = f"{project_name}_learn_project.yaml"
    return send_file(
        learn_path,
        mimetype="application/x-yaml",
        as_attachment=True,
        download_name=download_name,
    )


@bp.route("/static/<path:filename>")
def static_files(filename: str):
    """提供默认项目生成的静态 PDF 文件。"""

    project_name = get_project_from_request()
    _, pdf_folder, _, _, _ = get_project_paths(project_name)
    return send_from_directory(pdf_folder, filename)


@bp.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    """返回当前项目的附件文件。"""

    project_name = get_project_from_request()
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    return send_from_directory(attachments_folder, filename)


@bp.route("/export_attachments", methods=["GET"])
def export_attachments():
    """将附件、资源及 YAML 打包成 ZIP 供下载。"""

    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)
    mem_zip = io.BytesIO()
    added_any = False

    with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        if os.path.exists(attachments_folder):
            for root, _, filenames in os.walk(attachments_folder):
                for fname in filenames:
                    if fname.lower().endswith('.tex'):
                        continue
                    file_path = os.path.join(root, fname)
                    rel = os.path.relpath(file_path, attachments_folder)
                    archive.write(file_path, arcname=os.path.join("attachments", rel))
                    added_any = True
        if os.path.exists(resources_folder):
            for root, _, filenames in os.walk(resources_folder):
                for fname in filenames:
                    file_path = os.path.join(root, fname)
                    rel = os.path.relpath(file_path, resources_folder)
                    archive.write(file_path, arcname=os.path.join("resources", rel))
                    added_any = True
        if os.path.exists(yaml_path):
            archive.write(yaml_path, arcname="project.yaml")
            added_any = True

    if not added_any:
        return jsonify({"success": False, "error": "无附件、资源或项目配置可导出"}), 400

    mem_zip.seek(0)
    archive_name = secure_filename(project_name) or "project"
    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{archive_name}.zip",
    )


@bp.route("/export_project_bundle", methods=["GET"])
def export_project_bundle():
    """导出当前项目的完整数据，并可选包含应用源代码。"""

    project_name = get_project_from_request()
    include_param = request.args.get("mode") or request.args.get("include_code") or request.args.get("includeCode") or ""
    include_code = str(include_param).strip().lower() in {"1", "true", "yes", "on", "code", "with_code", "full"}

    try:
        load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        return api_error(str(exc), 500)

    attachments_folder, _, _, resources_folder, _ = get_project_paths(project_name)
    project_root = os.path.join(get_projects_root(), project_name)

    mem_zip = io.BytesIO()
    added_any = False

    with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as archive:

        def _add_directory(source_path: str, arc_prefix: str, skip_dirs: set[str] | None = None) -> None:
            nonlocal added_any
            if not os.path.exists(source_path):
                return
            skip = set(skip_dirs or ())
            for root, dirs, files in os.walk(source_path):
                dirs[:] = [d for d in sorted(dirs) if d not in skip and not d.startswith('.') and d != '__pycache__']
                for fname in sorted(files):
                    if fname.startswith('.') or fname in {'.DS_Store', 'Thumbs.db'}:
                        continue
                    if os.path.splitext(fname)[1] in {'.pyc', '.pyo', '.pyd'}:
                        continue
                    file_path = os.path.join(root, fname)
                    rel_dir = os.path.relpath(root, source_path)
                    rel_path = fname if rel_dir == '.' else os.path.join(rel_dir, fname)
                    archive.write(file_path, os.path.join(arc_prefix, rel_path))
                    added_any = True

        def _add_file(file_path: str, arcname: str) -> None:
            nonlocal added_any
            if os.path.isfile(file_path):
                archive.write(file_path, arcname)
                added_any = True

        def _add_project_data() -> None:
            project_prefix = os.path.join("project_data", secure_filename(project_name) or project_name)
            _add_directory(project_root, project_prefix)
            _add_directory(attachments_folder, os.path.join("project_data", "attachments"))
            _add_directory(resources_folder, os.path.join("project_data", "resources"))

        def _add_repo_tree() -> None:
            nonlocal added_any
            app_root = current_app.root_path
            repo_root = os.path.abspath(os.path.join(app_root, os.pardir))
            repo_name = os.path.basename(repo_root.rstrip(os.sep)) or 'project'
            skip_dirs = {'.git', '.pytest_cache', '.mypy_cache', '.idea', 'venv', 'build', 'temps', '__pycache__', '.sass-cache', 'benort.egg-info'}
            allowed_root_files = {'README.md', 'pyproject.toml'}

            for root, dirs, files in os.walk(repo_root):
                rel_dir = os.path.relpath(root, repo_root)
                parts = [] if rel_dir == '.' else rel_dir.split(os.sep)

                if parts and parts[0] in skip_dirs:
                    dirs[:] = []
                    continue

                if len(parts) >= 2 and parts[0] == 'benort' and parts[1] == 'projects':
                    if len(parts) == 2:
                        dirs[:] = [d for d in dirs if d == project_name]
                        files[:] = []
                        continue
                    if len(parts) >= 3 and parts[2] != project_name:
                        dirs[:] = []
                        continue

                if len(parts) >= 2 and parts[0] == 'benort' and parts[1] in {'attachments_store', 'resources_store'}:
                    if len(parts) == 2:
                        dirs[:] = [d for d in dirs if d == project_name]
                        files[:] = []
                        continue
                    if len(parts) >= 3 and parts[2] != project_name:
                        dirs[:] = []
                        continue

                dirs[:] = [d for d in sorted(dirs) if d not in skip_dirs and not d.startswith('.') and d != '__pycache__']
                selected_files = []
                for fname in sorted(files):
                    if fname.startswith('.') or fname in {'.DS_Store', 'Thumbs.db'}:
                        continue
                    if os.path.splitext(fname)[1] in {'.pyc', '.pyo', '.pyd'}:
                        continue
                    if rel_dir == '.':
                        if fname.lower().endswith('.zip'):
                            continue
                        if fname not in allowed_root_files:
                            continue
                    if fname.lower() in {'flask.log', 'server.info'} and rel_dir == '.':
                        continue
                    selected_files.append(fname)

                for fname in selected_files:
                    file_path = os.path.join(root, fname)
                    rel_path = fname if rel_dir == '.' else os.path.join(rel_dir, fname)
                    archive.write(file_path, os.path.join(repo_name, rel_path))
                    added_any = True

            setup_script = """#!/usr/bin/env bash
# chmod +x setup_project.sh
# ./setup_project.sh
# bash setup_project.sh

set -e

if [ ! -f "pyproject.toml" ]; then
  echo "请在项目根目录运行此脚本"
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

if [ -f ".env.example" ] && [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "安装完成。可以运行以下命令启动项目："
echo "source .venv/bin/activate"
echo "flask --app benort run"
"""
            archive.writestr(os.path.join(repo_name, 'setup_project.sh'), setup_script)
            added_any = True

        if include_code:
            _add_repo_tree()
        else:
            _add_project_data()

    if not added_any:
        return api_error("没有可导出的项目内容", 404)

    mem_zip.seek(0)
    safe = secure_filename(project_name) or project_name
    suffix = "with_code" if include_code else "data_only"
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    download_name = f"{safe}_{suffix}_{timestamp}.zip"
    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )


@bp.route("/projects", methods=["GET"])
def api_list_projects():
    """返回所有项目名称以及当前项目。"""

    projects = list_projects()
    metadata = {name: get_project_metadata(name) for name in projects}
    return jsonify({
        "projects": projects,
        "default": get_project_from_request(),
        "metadata": metadata,
    })


@bp.route("/projects/create", methods=["POST"])
def api_create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return api_error('项目名不能为空', 400)
    if not is_safe_project_name(name):
        return api_error('非法的项目名', 400)
    if name in list_projects():
        return api_error('同名项目已存在', 409)
    try:
        create_project(name)
    except Exception as exc:
        return api_error(str(exc), 500)
    return api_success({'project': name})


@bp.route("/projects/rename", methods=["POST"])
def api_rename_project():
    data = request.get_json(silent=True) or {}
    old = (data.get('oldName') or '').strip()
    new = (data.get('newName') or '').strip()
    if not old or not new:
        return api_error('oldName 和 newName 必填', 400)
    if not is_safe_project_name(old) or not is_safe_project_name(new):
        return api_error('非法的项目名', 400)
    if old == new:
        return api_success({'project': new})
    if new in list_projects():
        return api_error('目标项目已存在', 409)
    try:
        rename_project(old, new)
    except FileNotFoundError:
        return api_error('项目不存在', 404)
    except Exception as exc:
        return api_error(str(exc), 500)

    resp = api_success({'project': new})
    # 清除旧项目的 cookie
    old_cookie = get_project_cookie_name(old)
    resp.set_cookie(old_cookie, '', max_age=0, expires=0, httponly=True, samesite='Lax')
    return resp


@bp.route("/projects/delete", methods=["POST"])
def api_delete_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return api_error('name 必填', 400)
    if not is_safe_project_name(name):
        return api_error('非法的项目名', 400)
    if name not in list_projects():
        return api_error('项目不存在', 404)
    try:
        delete_project(name)
    except Exception as exc:
        return api_error(str(exc), 500)
    resp = api_success({'project': name})
    cookie = get_project_cookie_name(name)
    resp.set_cookie(cookie, '', max_age=0, expires=0, httponly=True, samesite='Lax')
    return resp


@bp.route("/projects/password", methods=["POST"])
def api_project_password():
    data = request.get_json(silent=True) or {}
    project_name = (data.get('project') or '').strip()
    if not project_name:
        return api_error('project 必填', 400)
    if not is_safe_project_name(project_name):
        return api_error('非法的项目名', 400)
    action = data.get('action') or 'set'
    current_password = data.get('currentPassword') or data.get('oldPassword')
    if data.get('remove') or action == 'clear':
        try:
            clear_project_password(project_name, current_password)
        except PermissionError as exc:
            return api_error(str(exc), 403)
        except Exception as exc:
            return api_error(str(exc), 500)
        resp = api_success({'project': project_name, 'passwordCleared': True})
        cookie = get_project_cookie_name(project_name)
        resp.set_cookie(cookie, '', max_age=0, expires=0, httponly=True, samesite='Lax')
        return resp

    new_password = data.get('newPassword') or data.get('password')
    if not new_password:
        return api_error('新密码不能为空', 400)
    try:
        set_project_password(project_name, new_password, current_password)
    except PermissionError as exc:
        return api_error(str(exc), 403)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        return api_error(str(exc), 500)

    password_hash = get_project_password_hash(project_name)
    resp = api_success({'project': project_name, 'passwordSet': True})
    if password_hash:
        cookie_name, token = issue_project_cookie(project_name, password_hash)
        resp.set_cookie(cookie_name, token, httponly=True, samesite='Lax')
    return resp


@bp.route("/projects/unlock", methods=["POST"])
def api_unlock_project():
    data = request.get_json(silent=True) or {}
    project_name = (data.get('project') or '').strip()
    password = data.get('password') or ''
    if not project_name or not password:
        return api_error('project 和 password 必填', 400)
    if not is_safe_project_name(project_name):
        return api_error('非法的项目名', 400)
    if not verify_project_password(project_name, password):
        return api_error('密码不正确', 403)
    password_hash = get_project_password_hash(project_name)
    if not password_hash:
        return api_error('项目未设置密码', 400)
    cookie_name, token = issue_project_cookie(project_name, password_hash)
    resp = api_success({'project': project_name, 'unlocked': True})
    resp.set_cookie(cookie_name, token, httponly=True, samesite='Lax')
    return resp


@bp.route("/projects/lock", methods=["POST"])
def api_lock_project():
    data = request.get_json(silent=True) or {}
    project_name = (data.get('project') or '').strip()
    if not project_name:
        return api_error('project 必填', 400)
    if not is_safe_project_name(project_name):
        return api_error('非法的项目名', 400)
    cookie_name = get_project_cookie_name(project_name)
    resp = api_success({'project': project_name, 'locked': True})
    resp.set_cookie(cookie_name, '', max_age=0, expires=0, httponly=True, samesite='Lax')
    return resp

@bp.route("/projects/<project_name>/static/<path:filename>")
def project_static(project_name: str, filename: str):
    """提供指定项目的静态文件。"""

    if not ensure_safe_project(project_name):
        return "", 404
    _, pdf_folder, _, _, _ = get_project_paths(project_name)
    return send_from_directory(pdf_folder, filename)


@bp.route("/projects/<project_name>/uploads/<path:filename>")
def project_uploads(project_name: str, filename: str):
    """提供指定项目的附件资源。"""

    if not ensure_safe_project(project_name):
        return "", 404
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    return send_from_directory(attachments_folder, filename)


def ensure_safe_project(project_name: str) -> bool:
    """在返回资源前确认项目合法存在。"""
    if not is_safe_project_name(project_name):
        return False
    return project_name in list_projects()


def _prepare_attachment_payload(
    project_data: dict | None,
    project_name: str,
    filename: str,
    full_path: str,
) -> dict[str, object]:
    local_url = f"/projects/{project_name}/uploads/{filename}"
    preferred_url = local_url
    oss_url: str | None = None
    sync_enabled = bool(project_data.get('ossSyncEnabled')) if isinstance(project_data, dict) else False
    if sync_enabled and oss_is_configured():
        try:
            oss_url = oss_upload_file(project_name, filename, full_path)
            if oss_url:
                preferred_url = oss_url
        except Exception as exc:  # pragma: no cover - logging only
            try:
                current_app.logger.warning('OSS 上传附件失败 %s: %s', filename, exc)
            except Exception:
                pass
    return {
        'name': filename,
        'localUrl': local_url,
        'ossUrl': oss_url,
        'preferredUrl': preferred_url,
    }


def _convert_pdf_to_images(pdf_path: str, output_dir: str, base_name: str) -> tuple[list[str], str | None]:
    if convert_from_path is None:  # pragma: no cover - requires optional dependency
        return [], '未安装 pdf2image，无法转换 PDF'
    try:
        images = convert_from_path(pdf_path, fmt='png', dpi=200)
    except Exception as exc:  # pragma: no cover - conversion environment specific
        return [], str(exc)
    if not images:
        return [], 'PDF 不包含可转换的页面'
    sanitized_base = secure_filename(base_name) or base_name or 'pdf_image'
    saved: list[str] = []
    for idx, image in enumerate(images, start=1):
        candidate_name = f"{sanitized_base}-p{idx}.png"
        candidate_path = os.path.join(output_dir, candidate_name)
        counter = 1
        while os.path.exists(candidate_path):
            candidate_name = f"{sanitized_base}-p{idx}-{counter}.png"
            candidate_path = os.path.join(output_dir, candidate_name)
            counter += 1
        try:
            image.save(candidate_path, "PNG")
        except Exception as exc:  # pragma: no cover - filesystem dependent
            return saved, f'保存图片失败: {exc}'
        saved.append(candidate_name)
    return saved, None


def _run_full_oss_sync(project_name: str, attachments_folder: str, resources_folder: str, yaml_path: str) -> dict[str, object]:
    summary: dict[str, object] = {}
    try:
        summary["attachments"] = oss_sync_directory(project_name, attachments_folder)
    except Exception as exc:
        summary["attachments"] = {"error": str(exc)}
        try:
            current_app.logger.warning('OSS 同步附件失败: %s', exc)
        except Exception:
            pass

    try:
        summary["resources"] = oss_sync_directory(project_name, resources_folder, category='resources')
    except Exception as exc:
        summary["resources"] = {"error": str(exc)}
        try:
            current_app.logger.warning('OSS 同步资源失败: %s', exc)
        except Exception:
            pass

    if os.path.exists(yaml_path):
        try:
            url = oss_upload_file(project_name, 'project.yaml', yaml_path, category='yaml')
            summary["yaml"] = {"uploaded": bool(url), "url": url}
        except Exception as exc:
            summary["yaml"] = {"error": str(exc)}
            try:
                current_app.logger.warning('OSS 上传项目 YAML 失败: %s', exc)
            except Exception:
                pass
    else:
        summary["yaml"] = {"uploaded": False, "url": None, "error": '项目 YAML 不存在'}
    return summary


@bp.route("/attachments/list")
def list_attachments():
    """返回项目附件清单及访问链接。"""

    project_name = get_project_from_request()
    try:
        project_data = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    os.makedirs(attachments_folder, exist_ok=True)
    attachment_refs = _collect_attachment_references(project_data)

    local_files: dict[str, str] = {}
    for fname in os.listdir(attachments_folder):
        full_path = os.path.join(attachments_folder, fname)
        if not os.path.isfile(full_path) or fname.lower().endswith('.tex'):
            continue
        local_files[fname] = f"/projects/{project_name}/uploads/{fname}"

    remote_files: dict[str, str] = {}
    oss_error: str | None = None
    configured = oss_is_configured()
    if configured:
        try:
            remote_files = oss_list_files(project_name)
        except Exception as exc:
            oss_error = str(exc)
            try:
                current_app.logger.warning('OSS 列出附件失败: %s', exc)
            except Exception:
                pass

    all_names = sorted(set(local_files) | set(remote_files))
    files = []
    for name in all_names:
        local_url = local_files.get(name)
        oss_url = remote_files.get(name)
        preferred = local_url or oss_url or ''
        location = 'synced'
        if local_url and not oss_url:
            location = 'local'
        elif oss_url and not local_url:
            location = 'oss'
        elif not local_url and not oss_url:
            location = 'missing'
        files.append(
            {
                "name": name,
                "path": name,
                "preferredUrl": preferred,
                "localUrl": local_url,
                "ossUrl": oss_url,
                "local": bool(local_url),
                "remote": bool(oss_url),
                "location": location,
                "refCount": len(attachment_refs.get(name, [])),
                "references": attachment_refs.get(name, []),
            }
        )

    payload = {
        "files": files,
        "syncEnabled": bool(project_data.get("ossSyncEnabled")),
        "ossConfigured": configured,
        "localPath": attachments_folder,
        "unused": [item["name"] for item in files if item.get("refCount", 0) == 0],
    }
    if oss_error:
        payload["ossError"] = oss_error
    return api_success(payload)


@bp.route("/attachments/delete", methods=["POST"])
def delete_attachment():
    """验证路径后删除附件文件。"""

    data = request.get_json(silent=True) or request.form or {}
    rel = data.get('path') or data.get('name') or request.args.get('path')
    if not rel:
        return api_error('path required', 400)
    if '..' in rel or rel.startswith('/') or rel.startswith('\\'):
        return api_error('invalid path', 400)

    project_name = get_project_from_request()
    try:
        project_data = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    target = os.path.normpath(os.path.join(attachments_folder, rel))
    if not target.startswith(os.path.normpath(attachments_folder)):
        return api_error('invalid path', 400)
    attachment_name = os.path.basename(rel)
    refs_map = _collect_attachment_references(project_data)
    contexts = refs_map.get(attachment_name, [])
    if contexts:
        preview = contexts[:5]
        detail = '，'.join(preview)
        if len(contexts) > len(preview):
            detail += '，等'
        return api_error(f'附件仍被引用，涉及：{detail}', 409)

    local_removed = False
    if os.path.exists(target):
        try:
            os.remove(target)
            local_removed = True
        except Exception as exc:
            return api_error(str(exc), 500)

    remote_removed = False
    remote_error: str | None = None
    if bool(project_data.get('ossSyncEnabled')) and oss_is_configured():
        try:
            oss_delete_file(project_name, rel)
            remote_removed = True
        except Exception as exc:
            remote_error = str(exc)
            try:
                current_app.logger.warning('OSS 删除附件失败 %s: %s', rel, exc)
            except Exception:
                pass

    if not local_removed and not remote_removed:
        return api_error('file not found', 404)

    payload = {"localRemoved": local_removed, "remoteRemoved": remote_removed}
    if remote_error:
        payload["remoteError"] = remote_error
    return api_success(payload)


@bp.route("/attachments/rename", methods=["POST"])
def rename_attachment():
    """Rename an attachment locally and propagate to OSS if enabled."""

    data = request.get_json(silent=True) or request.form or {}
    old_name = data.get('oldName') or data.get('from') or data.get('old')
    new_name = data.get('newName') or data.get('to') or data.get('name')
    if not old_name or not new_name:
        return api_error('oldName and newName required', 400)
    if any(sep in old_name for sep in ('..', '/', '\\')):
        return api_error('invalid oldName', 400)

    new_name_sanitized = secure_filename(new_name)
    if not new_name_sanitized:
        return api_error('invalid newName', 400)

    project_name = get_project_from_request()
    try:
        project_data = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    attachments_folder, _, _, _, _ = get_project_paths(project_name)

    old_path = os.path.join(attachments_folder, old_name)
    if not os.path.exists(old_path):
        return api_error('file not found', 404)

    if new_name_sanitized == old_name:
        return api_success(
            {
                "name": new_name_sanitized,
                "localUrl": f"/projects/{project_name}/uploads/{new_name_sanitized}",
                "ossStatus": None,
            }
        )

    new_path = os.path.join(attachments_folder, new_name_sanitized)
    if os.path.exists(new_path):
        return api_error('target filename already exists', 409)

    try:
        os.rename(old_path, new_path)
    except OSError as exc:
        return api_error(str(exc), 500)

    oss_status: dict[str, object] | None = None
    if bool(project_data.get('ossSyncEnabled')) and oss_is_configured():
        oss_status = {"uploaded": False, "deleted": False}
        try:
            oss_upload_file(project_name, new_name_sanitized, new_path)
            oss_status["uploaded"] = True
        except Exception as exc:
            oss_status["error"] = str(exc)
            try:
                current_app.logger.warning('OSS 重命名上传失败 %s→%s: %s', old_name, new_name_sanitized, exc)
            except Exception:
                pass
        try:
            oss_delete_file(project_name, old_name)
            oss_status["deleted"] = True
        except Exception as exc:
            oss_status = oss_status or {}
            oss_status["delete_error"] = str(exc)
            try:
                current_app.logger.warning('OSS 删除旧附件失败 %s: %s', old_name, exc)
            except Exception:
                pass

    return api_success(
        {
            "name": new_name_sanitized,
            "localUrl": f"/projects/{project_name}/uploads/{new_name_sanitized}",
            "ossStatus": oss_status,
        }
    )


@bp.route("/attachments/sync", methods=["POST"])
def update_attachment_sync():
    """开启或关闭 OSS 同步，并在开启时执行一次同步。"""

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    configured = oss_is_configured()
    if enabled and not configured:
        return api_error("OSS 未配置，无法开启同步", 400)

    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    project["ossSyncEnabled"] = enabled
    save_project(project)

    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)

    sync_payload: dict[str, object] = {"syncEnabled": enabled}
    if enabled:
        sync_payload["synced"] = _run_full_oss_sync(project_name, attachments_folder, resources_folder, yaml_path)
    return api_success(sync_payload)


@bp.route("/oss/status", methods=["GET"])
def oss_status():
    """返回当前项目的 OSS 同步状态。"""

    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)
    payload = {
        "syncEnabled": bool(project.get("ossSyncEnabled")),
        "ossConfigured": oss_is_configured(),
        "attachmentsPath": attachments_folder,
        "resourcesPath": resources_folder,
        "yamlPath": yaml_path if os.path.exists(yaml_path) else None,
        "project": project_name,
    }
    return api_success(payload)


@bp.route("/oss/sync_now", methods=["POST"])
def oss_sync_now():
    """立即将附件、资源与项目 YAML 同步至 OSS。"""

    if not oss_is_configured():
        return api_error("OSS 未配置，无法同步", 400)
    try:
        project = load_project() or {}
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)
    summary = _run_full_oss_sync(project_name, attachments_folder, resources_folder, yaml_path)
    return api_success({
        "syncEnabled": bool(project.get("ossSyncEnabled")),
        "synced": summary,
    })


@bp.route("/upload_resource", methods=["POST"])
def upload_resource():
    """上传资源文件，可按需挂载到指定页面或全局。"""

    files = request.files.getlist('files[]') or request.files.getlist('files')
    if not files and 'file' in request.files:
        files = [request.files['file']]
    if not files:
        return jsonify({'success': False, 'error': 'No file part'}), 400

    scope_param = (request.form.get('scope') or request.args.get('scope') or '').strip().lower()
    page_raw = request.form.get('page') or request.args.get('page')
    try:
        requested_page_idx = int(page_raw) if page_raw is not None else None
    except Exception:
        requested_page_idx = None
    effective_scope = 'page' if scope_param == 'page' and requested_page_idx is not None else 'global'

    paths_hint = request.form.getlist('paths[]') or request.form.getlist('paths') or []

    project_name = get_project_from_request()
    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    _, _, _, resources_folder, _ = get_project_paths(project_name)
    os.makedirs(resources_folder, exist_ok=True)

    uploads: list[dict[str, object]] = []
    pages = project.get('pages') if isinstance(project.get('pages'), list) else []

    def _ensure_page_resources(index: int) -> list[str]:
        if not isinstance(pages, list) or not (0 <= index < len(pages)) or not isinstance(pages[index], dict):
            return []
        res_list = pages[index].setdefault('resources', [])
        if not isinstance(res_list, list):
            res_list = pages[index]['resources'] = []
        return res_list

    project.setdefault('resources', [])
    if not isinstance(project['resources'], list):
        project['resources'] = []

    for idx, file_storage in enumerate(files):
        if not file_storage or file_storage.filename == '':
            continue
        raw_path = paths_hint[idx] if idx < len(paths_hint) else file_storage.filename
        normalized_hint = _normalize_resource_path(raw_path or file_storage.filename)
        if not normalized_hint:
            fallback_name = secure_filename(file_storage.filename or f'file_{idx}')
            normalized_hint = fallback_name or f'file_{idx}'
        parts = [secure_filename(part) for part in normalized_hint.split('/') if secure_filename(part)]
        if not parts:
            fallback = secure_filename(file_storage.filename or f'file_{idx}')
            if not fallback:
                continue
            parts = [fallback]

        rel_path = '/'.join(parts)
        dest_path = os.path.join(resources_folder, *parts)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(parts[-1])
            counter = 1
            original_base = base or 'file'
            while True:
                candidate = secure_filename(f"{original_base}_{counter}{ext}") or f"{original_base}_{counter}{ext}"
                parts[-1] = candidate
                rel_path = '/'.join(parts)
                dest_path = os.path.join(resources_folder, *parts)
                if not os.path.exists(dest_path):
                    break
                counter += 1

        file_storage.save(dest_path)
        name = parts[-1]

        scope_for_file = effective_scope
        page_idx = requested_page_idx if scope_for_file == 'page' else None
        if scope_for_file == 'page' and page_idx is not None:
            res_list = _ensure_page_resources(page_idx)
            if rel_path not in res_list:
                res_list.append(rel_path)
        else:
            scope_for_file = 'global'
            if rel_path not in project['resources']:
                project['resources'].append(rel_path)

        local_url = f'/projects/{project_name}/resources/{rel_path}'
        oss_url = None
        if bool(project.get('ossSyncEnabled')) and oss_is_configured():
            try:
                oss_url = oss_upload_file(project_name, rel_path, dest_path, category='resources')
            except Exception as exc:
                try:
                    current_app.logger.warning('OSS 上传资源失败 %s: %s', rel_path, exc)
                except Exception:
                    pass

        uploads.append({
            'path': rel_path,
            'name': name,
            'scope': scope_for_file,
            'page': page_idx,
            'localUrl': local_url,
            'ossUrl': oss_url,
            'url': oss_url or local_url,
        })

    if not uploads:
        return jsonify({'success': False, 'error': 'No valid files'}), 400

    save_project(project)
    return api_success({'files': uploads, 'scope': effective_scope, 'page': requested_page_idx})


def _serve_resource_file(project_name: str, filename: str):
    if not ensure_safe_project(project_name):
        return '', 404
    _, _, _, resources_folder, _ = get_project_paths(project_name)
    if not filename:
        return '', 404
    sanitized = filename.lstrip('/')
    if '..' in sanitized:
        return '', 404
    direct = os.path.join(resources_folder, sanitized)
    if os.path.exists(direct):
        rel = os.path.relpath(direct, resources_folder)
        return send_from_directory(resources_folder, rel)
    fallback = _find_resource_file(resources_folder, os.path.basename(sanitized))
    if fallback and os.path.exists(fallback):
        try:
            common = os.path.commonpath([os.path.abspath(fallback), os.path.abspath(resources_folder)])
        except ValueError:
            return '', 404
        if common == os.path.abspath(resources_folder):
            return send_file(fallback)
    return '', 404


@bp.route('/projects/<project_name>/resources/<path:filename>')
def project_resources(project_name: str, filename: str):
    """RESTful 访问指定项目资源文件。"""

    return _serve_resource_file(project_name, filename)


@bp.route('/resources/<path:filename>')
def serve_resource(filename):
    """兼容旧接口，通过查询参数解析项目名。"""

    project_name = get_project_from_request()
    return _serve_resource_file(project_name, filename)


@bp.route('/resources/rename', methods=['POST'])
def rename_resource():
    """重命名资源文件并同步更新引用及 OSS。"""

    data = request.get_json(silent=True) or request.form or {}
    raw_old = (
        data.get('oldPath')
        or data.get('oldName')
        or data.get('from')
        or data.get('old')
        or ''
    )
    raw_new = (
        data.get('newPath')
        or data.get('newName')
        or data.get('to')
        or data.get('name')
        or ''
    )
    old_value = str(raw_old).strip()
    new_value = str(raw_new).strip()
    if not old_value or not new_value:
        return api_error('oldPath and newPath required', 400)

    project_name = get_project_from_request()
    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    _, _, _, resources_folder, _ = get_project_paths(project_name)
    os.makedirs(resources_folder, exist_ok=True)

    normalized_old = _normalize_resource_path(old_value)
    if not normalized_old:
        sanitized = secure_filename(old_value)
        if not sanitized:
            return api_error('invalid oldPath', 400)
        normalized_old = sanitized

    old_abs = os.path.join(resources_folder, *normalized_old.split('/'))
    if not os.path.exists(old_abs):
        fallback = _find_resource_file(resources_folder, os.path.basename(normalized_old))
        if fallback and os.path.exists(fallback):
            old_abs = fallback
            rel = os.path.relpath(fallback, resources_folder)
            normalized_old = _normalize_resource_path(rel)
        else:
            return api_error('file not found', 404)

    try:
        common = os.path.commonpath([os.path.abspath(old_abs), os.path.abspath(resources_folder)])
    except ValueError:
        return api_error('invalid path', 400)
    if common != os.path.abspath(resources_folder):
        return api_error('invalid path', 400)

    parent_rel = os.path.dirname(normalized_old)

    normalized_new = _normalize_resource_path(new_value)
    if not normalized_new:
        sanitized_new = secure_filename(new_value)
        if not sanitized_new:
            return api_error('invalid newPath', 400)
        normalized_new = '/'.join(filter(None, [parent_rel, sanitized_new]))
    new_abs = os.path.join(resources_folder, *normalized_new.split('/'))

    if os.path.abspath(new_abs) == os.path.abspath(old_abs):
        return api_success({
            'name': os.path.basename(normalized_new),
            'path': normalized_new,
            'localUrl': f"/projects/{project_name}/resources/{normalized_new}",
        })

    if os.path.exists(new_abs):
        return api_error('target filename already exists', 409)

    os.makedirs(os.path.dirname(new_abs), exist_ok=True)

    try:
        os.rename(old_abs, new_abs)
    except OSError as exc:
        return api_error(str(exc), 500)

    old_leaf = os.path.basename(normalized_old)
    new_leaf = os.path.basename(normalized_new)

    def _rewrite_entries(container: list[str] | None) -> list[str] | None:
        if not isinstance(container, list):
            return container
        updated: list[str] = []
        for entry in container:
            if not isinstance(entry, str):
                updated.append(entry)
                continue
            normalized_entry = _normalize_resource_path(entry)
            if not normalized_entry:
                updated.append(entry)
                continue
            if normalized_entry in {normalized_old, old_leaf}:
                updated.append(normalized_new)
            else:
                updated.append(entry)
        # 去重，保持顺序
        seen: set[str] = set()
        deduped: list[str] = []
        for item in updated:
            if isinstance(item, str):
                marker = _normalize_resource_path(item) or item
                if marker in seen:
                    continue
                seen.add(marker)
            deduped.append(item)
        return deduped

    if isinstance(project.get('resources'), list):
        project['resources'] = _rewrite_entries(project['resources'])  # type: ignore[assignment]

    if isinstance(project.get('pages'), list):
        for page in project['pages']:
            if isinstance(page, dict) and isinstance(page.get('resources'), list):
                page['resources'] = _rewrite_entries(page['resources'])  # type: ignore[assignment]

    save_project(project)

    oss_status: dict[str, object] | None = None
    if bool(project.get('ossSyncEnabled')) and oss_is_configured():
        oss_status = {'uploaded': False, 'deleted': False}
        try:
            oss_upload_file(project_name, normalized_new, new_abs, category='resources')
            oss_status['uploaded'] = True
        except Exception as exc:
            oss_status['error'] = str(exc)
            try:
                current_app.logger.warning('OSS 上传资源失败 %s→%s: %s', normalized_old, normalized_new, exc)
            except Exception:
                pass
        try:
            oss_delete_file(project_name, normalized_old, category='resources')
            oss_status['deleted'] = True
        except Exception as exc:
            oss_status['delete_error'] = str(exc)
            try:
                current_app.logger.warning('OSS 删除旧资源失败 %s: %s', normalized_old, exc)
            except Exception:
                pass

    payload = {
        'name': new_leaf,
        'path': normalized_new,
        'localUrl': f"/projects/{project_name}/resources/{normalized_new}",
        'ossStatus': oss_status,
    }
    return api_success(payload)


@bp.route('/resources/delete', methods=['POST'])
def delete_resource():
    """删除资源文件并清理项目中的引用。"""

    data = request.get_json(silent=True) or request.form or {}
    raw = data.get('name') or data.get('path') or request.args.get('name') or request.args.get('path')
    if not raw:
        return api_error('name required', 400)
    raw = str(raw).strip()
    if not raw:
        return api_error('name required', 400)
    raw = raw.split('?', 1)[0]
    raw = raw.split('#', 1)[0]
    relative = raw.lstrip('/')
    if '..' in relative:
        return api_error('invalid name', 400)
    name = os.path.basename(relative)
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        return api_error('invalid name', 400)

    scope = str(data.get('scope') or '').strip().lower()
    page_idx_raw = data.get('page')
    try:
        page_idx = int(page_idx_raw) if page_idx_raw is not None else None
    except (TypeError, ValueError):
        page_idx = None

    project_name = get_project_from_request()
    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    _, _, _, resources_folder, _ = get_project_paths(project_name)

    pages_removed: list[int] = []
    global_removed = False
    updated = False

    if scope == 'page':
        if page_idx is None:
            return api_error('page required for scope=page', 400)
        pages = project.get('pages', [])
        if not isinstance(pages, list) or not (0 <= page_idx < len(pages)):
            return api_error('page out of range', 404)
        page_obj = pages[page_idx]
        if not isinstance(page_obj, dict):
            return api_error('page data invalid', 500)
        resources = page_obj.get('resources', [])
        if not isinstance(resources, list) or name not in resources:
            return api_error('resource not associated with page', 404)
        page_obj['resources'] = [r for r in resources if r != name]
        pages_removed.append(page_idx)
        updated = True
    elif scope == 'global':
        resources = project.get('resources', [])
        if not isinstance(resources, list) or name not in resources:
            return api_error('resource not in global scope', 404)
        project['resources'] = [r for r in resources if r != name]
        global_removed = True
        updated = True
    else:
        scope = 'all'
        resources = project.get('resources', [])
        if isinstance(resources, list) and name in resources:
            project['resources'] = [r for r in resources if r != name]
            global_removed = True
            updated = True
        pages = project.get('pages', [])
        if isinstance(pages, list):
            for idx, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                res_list = page.get('resources', [])
                if not isinstance(res_list, list) or name not in res_list:
                    continue
                page['resources'] = [r for r in res_list if r != name]
                pages_removed.append(idx)
                updated = True

    if updated:
        save_project(project)

    usage_after = _collect_resource_usage(project)
    remaining = usage_after.get(name)

    payload = {
        'name': name,
        'scope': scope,
        'globalRemoved': global_removed,
        'pagesRemoved': [idx + 1 for idx in pages_removed],
    }

    if remaining:
        payload['fileRemoved'] = False
        payload['stillReferenced'] = {
            'pages': sorted(idx + 1 for idx in remaining['pages']),
            'global': bool(remaining['global']),
            'refCount': len(remaining['pages']) + (1 if remaining['global'] else 0),
        }
        return api_success(payload)

    direct_target = os.path.join(resources_folder, relative)
    target = direct_target if os.path.exists(direct_target) else _find_resource_file(resources_folder, name)

    local_removed = False
    if target and os.path.exists(target):
        try:
            os.remove(target)
            local_removed = True
        except Exception as exc:
            return api_error(str(exc), 500)

    remote_removed = False
    remote_error: Optional[str] = None
    if bool(project.get('ossSyncEnabled')) and oss_is_configured():
        remote_candidates: list[str] = []
        try:
            existing = oss_list_files(project_name, category='resources')
            remote_candidates = [key for key in existing if os.path.basename(key) == name]
        except Exception as exc:
            remote_error = str(exc)
            try:
                current_app.logger.warning('OSS 列出资源失败 %s: %s', name, exc)
            except Exception:
                pass
        targets = remote_candidates or [name]
        for key in targets:
            try:
                oss_delete_file(project_name, key, category='resources')
                remote_removed = True
            except Exception as exc:
                remote_error = str(exc)
                try:
                    current_app.logger.warning('OSS 删除资源失败 %s: %s', key, exc)
                except Exception:
                    pass

    if not local_removed and not remote_removed:
        return api_error('file not found', 404)

    payload['fileRemoved'] = True
    payload['localRemoved'] = local_removed
    payload['remoteRemoved'] = remote_removed
    if remote_error:
        payload['remoteError'] = remote_error
    return api_success(payload)


@bp.route('/resources/list')
def list_resources():
    """列出全局或指定页面的资源，并标记文件是否存在。"""

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    project_name = get_project_from_request()
    _, _, _, resources_folder, _ = get_project_paths(project_name)
    page = request.args.get('page')
    try:
        page_idx = int(page) if page is not None else None
    except Exception:
        page_idx = None

    names: list[str] = []
    page_id = None
    if page_idx is not None:
        pages = project.get('pages', [])
        if 0 <= page_idx < len(pages) and isinstance(pages[page_idx], dict):
            res_list = pages[page_idx].get('resources', [])
            if isinstance(res_list, list):
                names = [str(name) for name in res_list if isinstance(name, str)]
            page_id = pages[page_idx].get('pageId')
    else:
        res_list = project.get('resources', [])
        if isinstance(res_list, list):
            names = [str(name) for name in res_list if isinstance(name, str)]

    usage_map = _collect_resource_usage(project)
    local_map: dict[str, str] = {}
    for root, _, file_names in os.walk(resources_folder):
        for fname in file_names:
            rel_root = os.path.relpath(root, resources_folder)
            rel_path = fname if rel_root in ('.', '') else os.path.join(rel_root, fname)
            norm_rel = _normalize_resource_path(rel_path)
            if not norm_rel:
                continue
            local_url = f'/projects/{project_name}/resources/{norm_rel}'
            local_map.setdefault(norm_rel, local_url)
            base_name = os.path.basename(norm_rel)
            local_map.setdefault(base_name, local_url)

    remote_map: dict[str, str] = {}
    oss_error: str | None = None
    configured = oss_is_configured()
    if configured:
        try:
            raw_remote = oss_list_files(project_name, category='resources')
            for rel_name, url in raw_remote.items():
                norm_rel = _normalize_resource_path(rel_name)
                if not norm_rel:
                    continue
                remote_map.setdefault(norm_rel, url)
                remote_map.setdefault(os.path.basename(norm_rel), url)
        except Exception as exc:
            oss_error = str(exc)
            try:
                current_app.logger.warning('OSS 列出资源失败: %s', exc)
            except Exception:
                pass

    files = []
    for name in names:
        normalized = _normalize_resource_path(name)
        if not normalized:
            sanitized = secure_filename(str(name))
            normalized = sanitized
        else:
            sanitized = os.path.basename(normalized) or normalized
        local_url = local_map.get(normalized) or local_map.get(sanitized) or ''
        oss_url = remote_map.get(normalized) or remote_map.get(sanitized) or ''
        preferred = local_url or oss_url or ''
        if normalized in usage_map:
            usage_entry = usage_map[normalized]
        else:
            usage_entry = usage_map.get(sanitized, {'pages': set(), 'global': False})
        if not sanitized:
            continue
        if local_url and oss_url:
            location = 'synced'
        elif local_url and not oss_url:
            location = 'local'
        elif not local_url and oss_url:
            location = 'oss'
        else:
            location = 'missing'
        files.append(
            {
                'name': sanitized,
                'path': normalized,
                'url': preferred,
                'preferredUrl': preferred,
                'localUrl': local_url,
                'ossUrl': oss_url,
                'exists': bool(local_url),
                'remote': bool(oss_url),
                'local': bool(local_url),
                'location': location,
                'refCount': len(usage_entry['pages']) + (1 if usage_entry['global'] else 0),
                'usedOnPages': sorted(idx + 1 for idx in usage_entry['pages']),
                'usedGlobally': bool(usage_entry['global']),
                'otherPages': sorted(idx + 1 for idx in usage_entry['pages'] if page_idx is not None and idx != page_idx),
            }
        )

    payload = {'files': files, 'ossConfigured': configured}
    if page_id:
        payload['pageId'] = str(page_id)
    if oss_error:
        payload['ossError'] = oss_error
    return api_success(payload)


@bp.route('/templates/list', methods=['GET'])
def templates_list():
    """返回可用模板列表，供前端选择。"""

    try:
        return api_success({'templates': list_templates()})
    except Exception as exc:
        return api_error(str(exc), 500)
