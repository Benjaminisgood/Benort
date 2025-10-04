"""封装 Flask 路由的蓝图，提供前端交互所需的全部接口。"""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from typing import Optional, Tuple

import requests
from flask import Blueprint, current_app, jsonify, render_template, request, send_file, send_from_directory
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
    is_safe_project_name,
    issue_project_cookie,
    list_projects,
    load_project,
    rename_project,
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
    UI_THEME,
    OPENAI_CHAT_COMPLETIONS_MODEL,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_RESPONSE_FORMAT,
    OPENAI_TTS_SPEED,
    OPENAI_TTS_VOICE,
)
from .template_store import get_default_header, get_default_template, list_templates
from .responses import api_error, api_success


_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE)


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
    pages = project.get("pages", [])
    # 取出每页讲稿并合并为一段文本
    scripts = [p.get("script", "") for p in pages if isinstance(p, dict)]
    merged = "\n\n".join([n.strip() for n in scripts if n and n.strip()])
    if not merged:
        return jsonify({"success": False, "error": "没有可用的笔记内容"}), 400

    project_name = get_project_from_request()
    _, _, _, _, build_folder = get_project_paths(project_name)
    audio_folder = os.path.join(build_folder, 'audio')
    os.makedirs(audio_folder, exist_ok=True)
    content_hash = hashlib.sha256(merged.encode('utf-8')).hexdigest()
    audio_path = os.path.join(audio_folder, 'all_notes.mp3')
    hash_path = os.path.join(audio_folder, 'all_notes.hash')

    if os.path.exists(audio_path) and os.path.exists(hash_path):
        try:
            with open(hash_path, 'r', encoding='utf-8') as hf:
                if hf.read().strip() == content_hash:
                    return send_file(
                        audio_path,
                        mimetype='audio/mpeg',
                        as_attachment=True,
                        download_name='all_notes.mp3',
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
                "input": merged,
                "voice": OPENAI_TTS_VOICE,
                "response_format": OPENAI_TTS_RESPONSE_FORMAT,
                "speed": OPENAI_TTS_SPEED,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            audio_bytes = resp.content
            try:
                with open(audio_path, 'wb') as af:
                    af.write(audio_bytes)
                with open(hash_path, 'w', encoding='utf-8') as hf:
                    hf.write(content_hash)
            except Exception as exc:
                print(f'写入音频失败: {exc}')
            return send_file(
                audio_path,
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name="all_notes.mp3",
            )
        return jsonify({"success": False, "error": f"OpenAI TTS错误: {resp.text}"}), 500
    except Exception as exc:  # pragma: no cover - network errors
        return jsonify({"success": False, "error": str(exc)}), 500


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
            }
        )

    payload = {
        "files": files,
        "syncEnabled": bool(project_data.get("ossSyncEnabled")),
        "ossConfigured": configured,
        "localPath": attachments_folder,
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

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'}), 400
    file_storage = request.files['file']
    if file_storage.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400

    page = request.form.get('page') or request.args.get('page')
    try:
        page_idx = int(page) if page is not None else None
    except Exception:
        page_idx = None

    filename = secure_filename(file_storage.filename)
    if not filename:
        return jsonify({'success': False, 'error': 'Invalid filename'}), 400

    project_name = get_project_from_request()
    _, _, _, resources_folder, _ = get_project_paths(project_name)
    os.makedirs(resources_folder, exist_ok=True)

    save_path = os.path.join(resources_folder, filename)
    if os.path.exists(save_path):
        base, ext = os.path.splitext(filename)
        counter = 1
        while True:
            candidate = f"{base}_{counter}{ext}"
            candidate_path = os.path.join(resources_folder, candidate)
            if not os.path.exists(candidate_path):
                filename = candidate
                save_path = candidate_path
                break
            counter += 1

    file_storage.save(save_path)

    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    name = filename
    if page_idx is not None:
        pages = project.get('pages', [])
        if 0 <= page_idx < len(pages) and isinstance(pages[page_idx], dict):
            resources = pages[page_idx].setdefault('resources', [])
            if name not in resources:
                resources.append(name)
        else:
            project.setdefault('resources', [])
            if name not in project['resources']:
                project['resources'].append(name)
    else:
        project.setdefault('resources', [])
        if name not in project['resources']:
            project['resources'].append(name)

    save_project(project)
    local_url = f'/projects/{project_name}/resources/{name}'
    oss_url = None
    if bool(project.get('ossSyncEnabled')) and oss_is_configured():
        try:
            oss_url = oss_upload_file(project_name, name, save_path, category='resources')
        except Exception as exc:
            try:
                current_app.logger.warning('OSS 上传资源失败 %s: %s', name, exc)
            except Exception:
                pass

    url = local_url or oss_url or ''
    scope = 'page' if page_idx is not None else 'global'
    return api_success({
        'url': url,
        'preferredUrl': url,
        'name': name,
        'scope': scope,
        'page': page_idx,
        'localUrl': local_url,
        'ossUrl': oss_url,
    })


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
    old_name = (data.get('oldName') or data.get('from') or data.get('old') or '').strip()
    new_name = (data.get('newName') or data.get('to') or data.get('name') or '').strip()
    if not old_name or not new_name:
        return api_error('oldName and newName required', 400)
    if any(sep in old_name for sep in ('..', '/', '\\')):
        return api_error('invalid oldName', 400)

    sanitized_new = secure_filename(new_name)
    if not sanitized_new:
        return api_error('invalid newName', 400)

    project_name = get_project_from_request()
    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)

    _, _, _, resources_folder, _ = get_project_paths(project_name)
    os.makedirs(resources_folder, exist_ok=True)

    original_path = os.path.join(resources_folder, old_name)
    if not os.path.exists(original_path):
        original_path = _find_resource_file(resources_folder, os.path.basename(old_name)) or ''

    if not original_path or not os.path.exists(original_path):
        return api_error('file not found', 404)

    try:
        common = os.path.commonpath([os.path.abspath(original_path), os.path.abspath(resources_folder)])
    except ValueError:
        return api_error('invalid path', 400)
    if common != os.path.abspath(resources_folder):
        return api_error('invalid path', 400)

    old_base = os.path.basename(original_path)

    if old_base == sanitized_new:
        return api_success({
            'name': sanitized_new,
            'localUrl': f"/projects/{project_name}/resources/{os.path.relpath(original_path, resources_folder).replace(os.sep, '/')}"
        })

    target_dir = os.path.dirname(original_path)
    new_path = os.path.join(target_dir, sanitized_new)
    if os.path.exists(new_path):
        return api_error('target filename already exists', 409)

    try:
        os.rename(original_path, new_path)
    except OSError as exc:
        return api_error(str(exc), 500)

    # 更新项目引用的资源名称
    def _update_names(container: list[str] | None) -> list[str] | None:
        if not isinstance(container, list):
            return container
        updated: list[str] = []
        for entry in container:
            entry_str = str(entry)
            base = os.path.basename(entry_str)
            if base == old_base:
                prefix = entry_str[:-len(base)] if entry_str.endswith(base) else ''
                updated.append(prefix + sanitized_new)
            else:
                updated.append(entry)
        return updated

    if isinstance(project.get('resources'), list):
        project['resources'] = _update_names(project['resources'])  # type: ignore[assignment]

    if isinstance(project.get('pages'), list):
        for page in project['pages']:
            if isinstance(page, dict) and isinstance(page.get('resources'), list):
                page['resources'] = _update_names(page['resources'])  # type: ignore[assignment]

    save_project(project)

    oss_status: dict[str, object] | None = None
    if bool(project.get('ossSyncEnabled')) and oss_is_configured():
        oss_status = {'uploaded': False, 'deleted': False}
        try:
            oss_upload_file(project_name, sanitized_new, new_path, category='resources')
            oss_status['uploaded'] = True
        except Exception as exc:
            oss_status['error'] = str(exc)
            try:
                current_app.logger.warning('OSS 上传资源失败 %s→%s: %s', old_name, sanitized_new, exc)
            except Exception:
                pass
        try:
            oss_delete_file(project_name, old_base, category='resources')
            oss_status['deleted'] = True
        except Exception as exc:
            oss_status['delete_error'] = str(exc)
            try:
                current_app.logger.warning('OSS 删除旧资源失败 %s: %s', old_name, exc)
            except Exception:
                pass

    relative_new = os.path.relpath(new_path, resources_folder).replace(os.sep, '/')
    payload = {
        'name': sanitized_new,
        'localUrl': f"/projects/{project_name}/resources/{relative_new}",
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

    project_name = get_project_from_request()
    try:
        project = load_project()
    except ProjectLockedError:
        return api_error(_LOCKED_ERROR, 401)
    _, _, _, resources_folder, _ = get_project_paths(project_name)
    direct_target = os.path.join(resources_folder, relative)
    target = direct_target if os.path.exists(direct_target) else _find_resource_file(resources_folder, name)
    if target and os.path.exists(target):
        try:
            os.remove(target)
        except Exception as exc:
            return api_error(str(exc), 500)

    if bool(project.get('ossSyncEnabled')) and oss_is_configured():
        remote_candidates: list[str] = []
        try:
            existing = oss_list_files(project_name, category='resources')
            remote_candidates = [key for key in existing if os.path.basename(key) == name]
        except Exception as exc:
            try:
                current_app.logger.warning('OSS 列出资源失败 %s: %s', name, exc)
            except Exception:
                pass
        targets = remote_candidates or [name]
        for key in targets:
            try:
                oss_delete_file(project_name, key, category='resources')
            except Exception as exc:
                try:
                    current_app.logger.warning('OSS 删除资源失败 %s: %s', key, exc)
                except Exception:
                    pass

    if isinstance(project.get('resources'), list):
        project['resources'] = [r for r in project['resources'] if os.path.basename(r) != name]
    if isinstance(project.get('pages'), list):
        for page in project['pages']:
            if isinstance(page, dict):
                resources = page.get('resources')
                if isinstance(resources, list):
                    page['resources'] = [r for r in resources if os.path.basename(r) != name]
    save_project(project)
    return api_success()


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

    names = []
    if page_idx is not None:
        pages = project.get('pages', [])
        if 0 <= page_idx < len(pages) and isinstance(pages[page_idx], dict):
            res_list = pages[page_idx].get('resources', [])
            if isinstance(res_list, list):
                names = res_list
    else:
        res_list = project.get('resources', [])
        if isinstance(res_list, list):
            names = res_list

    local_map: dict[str, str] = {}
    for root, _, file_names in os.walk(resources_folder):
        for fname in file_names:
            rel_root = os.path.relpath(root, resources_folder)
            rel_path = fname if rel_root in ('.', '') else os.path.join(rel_root, fname)
            local_map.setdefault(os.path.basename(rel_path), f'/projects/{project_name}/resources/{rel_path.replace(os.sep, "/")}')

    remote_map: dict[str, str] = {}
    oss_error: str | None = None
    configured = oss_is_configured()
    if configured:
        try:
            raw_remote = oss_list_files(project_name, category='resources')
            for rel_name, url in raw_remote.items():
                remote_map.setdefault(os.path.basename(rel_name), url)
        except Exception as exc:
            oss_error = str(exc)
            try:
                current_app.logger.warning('OSS 列出资源失败: %s', exc)
            except Exception:
                pass

    files = []
    for name in names:
        sanitized = os.path.basename(str(name))
        if not sanitized:
            continue
        local_url = local_map.get(sanitized)
        oss_url = remote_map.get(sanitized)
        preferred = local_url or oss_url or ''
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
                'path': sanitized,
                'url': preferred,
                'preferredUrl': preferred,
                'localUrl': local_url,
                'ossUrl': oss_url,
                'exists': bool(local_url),
                'remote': bool(oss_url),
                'local': bool(local_url),
                'location': location,
            }
        )

    payload = {'files': files, 'ossConfigured': configured}
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
