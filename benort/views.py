"""封装 Flask 路由的蓝图，提供前端交互所需的全部接口。"""

import base64
import io
import os
import re
import subprocess
import zipfile

import requests
from flask import Blueprint, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from .latex import normalize_latex_content, prepare_latex_assets, _find_resource_file
from .project_store import (
    _store_attachment_file,
    get_project_from_request,
    get_project_paths,
    is_safe_project_name,
    list_projects,
    load_project,
    save_project,
)
from .config import (
    AI_PROMPTS,
    OPENAI_CHAT_COMPLETIONS_MODEL,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_RESPONSE_FORMAT,
    OPENAI_TTS_SPEED,
    OPENAI_TTS_VOICE,
)
from .template_store import get_default_header, get_default_template, list_templates
from .responses import api_error, api_success


# 定义蓝图以便在应用工厂中一次性注册所有路由
bp = Blueprint("benort", __name__)


@bp.route("/")
def index():
    """渲染主编辑器页面，提供初始 UI。"""

    return render_template("editor.html")


@bp.route("/export_audio", methods=["GET"])
def export_audio():
    """合并所有讲稿并调用 OpenAI TTS 生成整段音频。"""

    project = load_project()
    pages = project.get("pages", [])
    # 取出每页讲稿并合并为一段文本
    scripts = [p.get("script", "") for p in pages if isinstance(p, dict)]
    merged = "\n\n".join([n.strip() for n in scripts if n and n.strip()])
    if not merged:
        return jsonify({"success": False, "error": "没有可用的笔记内容"}), 400

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
            return send_file(
                io.BytesIO(audio_bytes),
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

    project = load_project() or {}
    if "pages" not in project or not isinstance(project["pages"], list):
        project["pages"] = []
    idx = int(request.json.get("idx", len(project["pages"])))
    project["pages"].insert(
        idx,
        {
            "content": "\\begin{frame}\n\\frametitle{Title of the Slide}\n\\framesubtitle{Subtitle}\n内容...\n\\end{frame}",
            "script": "",
            "notes": "",
            "bib": [],
        },
    )
    save_project(project)
    return jsonify({"success": True, "pages": project["pages"]})


@bp.route("/compile_page", methods=["POST"])
def compile_page():
    """将单页 LaTeX 组合模板后用 xelatex 编译为 PDF。"""

    data = request.json
    page_idx = int(data.get("page", 0))
    project = load_project()
    template = project.get("template")
    pages = project.get("pages", [])
    project_name = get_project_from_request()
    attachments_folder, pdf_folder, _, resources_folder, build_folder = get_project_paths(project_name)

    default_template = get_default_template()
    default_header = default_template.get("header", get_default_header())
    default_before = default_template.get("beforePages", "\\begin{document}")
    default_footer = default_template.get("footer", "\\end{document}")

    # 根据模板类型补齐 header/before/footer
    if isinstance(template, dict):
        header = normalize_latex_content(template.get("header", default_header), attachments_folder, resources_folder)
        before = normalize_latex_content(template.get("beforePages", default_before), attachments_folder, resources_folder)
        footer = normalize_latex_content(template.get("footer", default_footer), attachments_folder, resources_folder)
    else:
        header = normalize_latex_content(str(template) if template else default_header, attachments_folder, resources_folder)
        before = normalize_latex_content(default_before, attachments_folder, resources_folder)
        footer = normalize_latex_content(default_footer, attachments_folder, resources_folder)

    if 0 <= page_idx < len(pages):
        raw = pages[page_idx]["content"] if isinstance(pages[page_idx], dict) else str(pages[page_idx])
        page_tex = normalize_latex_content(raw, attachments_folder, resources_folder)
    else:
        page_tex = ""

    tex = f"{header}\n{before}\n{page_tex}\n{footer}\n"
    filename = f"slide_page_{page_idx + 1}.tex"
    tex_path = os.path.join(build_folder, filename)
    pdf_name = f"slide_page_{page_idx + 1}.pdf"
    prepare_latex_assets([header, before, page_tex, footer], attachments_folder, resources_folder, build_folder, pdf_folder)

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

    project = load_project()
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

    project = load_project()
    pages = project.get("pages", [])
    notes_list = []
    for idx, page in enumerate(pages, start=1):
        if isinstance(page, dict):
            note = page.get("notes", "") or ""
            notes_list.append(f"## Page {idx}\n\n{note}\n")

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

    return jsonify(load_project())


@bp.route("/upload_image", methods=["POST"])
def upload_image():
    """上传图片至附件目录并返回可访问链接。"""

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part"})
    file_storage = request.files["file"]
    project_name = get_project_from_request()
    try:
        filename, url = _store_attachment_file(file_storage, project_name)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    return jsonify({"success": True, "url": url, "filename": filename})


@bp.route("/attachments/upload", methods=["POST"])
def upload_attachment():
    """上传任意附件文件并返回文件信息。"""

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part"})
    file_storage = request.files["file"]
    project_name = get_project_from_request()
    try:
        filename, url = _store_attachment_file(file_storage, project_name)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    return jsonify({"success": True, "filename": filename, "url": url})


@bp.route("/project", methods=["POST"])
def save_project_api():
    """持久化前端传入的项目数据。"""

    data = request.json or {}
    project = load_project() or {}
    for key, value in data.items():
        project[key] = value
    if data.get("project"):
        pass
    save_project(project)
    return jsonify({"success": True})


@bp.route("/compile", methods=["POST"])
def compile_tex():
    """将全部页面合成模板后编译为整册 PDF。"""

    project = load_project()
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
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            return jsonify({"success": True, "audio": encoded})
        return jsonify({"success": False, "error": f"OpenAI TTS错误: {resp.text}"}), 500
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "error": str(exc)}), 500


@bp.route("/ai_optimize", methods=["POST"])
def ai_optimize():
    """调用 OpenAI ChatCompletion 优化讲稿、笔记或 LaTeX 内容。"""

    data = request.json or {}
    content = data.get("content", "")
    opt_type = data.get("type", "latex")
    page_tex = data.get("page_tex", "")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return api_error("未设置OPENAI_API_KEY环境变量", 500)

    default_header = get_default_header()

    if opt_type == "script":
        prompt = AI_PROMPTS["script"]["template"].format(page_tex=page_tex, content=content)
        system_prompt = AI_PROMPTS["script"]["system"]
    elif opt_type == "note":
        prompt = AI_PROMPTS["note"]["template"].format(page_tex=page_tex, content=content)
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
            content=content,
            allowed_packages=allowed_str,
            custom_macros=custom_macro_list,
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
    """基于 DOI 或链接调用 OpenAI 生成 bibtex 条目。"""

    data = request.json or {}
    ref = str(data.get("ref", "")).strip()
    if not ref:
        return jsonify({"success": False, "error": "参考文献输入为空"}), 400

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"success": False, "error": "未设置OPENAI_API_KEY环境变量"}), 500

    prompt = (
        "请根据下方输入的DOI或文献链接，自动补全并生成标准的bibtex条目（包含作者、标题、期刊、年份等），"
        "不要出现```bibtex等无关信息，只返回bibtex内容：\n"
        f"{ref}"
    )

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
                    {"role": "system", "content": "你是一个bibtex文献专家，只返回bibtex条目。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            bib = resp.json()["choices"][0]["message"]["content"]
            return jsonify({"success": True, "bib": bib})
        return jsonify({"success": False, "error": f"OpenAI API错误: {resp.text}"}), 500
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "error": str(exc)}), 500


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
    """将附件与资源目录打包成 ZIP 供下载。"""

    project_name = get_project_from_request()
    attachments_folder, _, _, resources_folder, _ = get_project_paths(project_name)
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

    if not added_any:
        return jsonify({"success": False, "error": "无附件或资源可导出"}), 400

    mem_zip.seek(0)
    return send_file(
        mem_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name="attachments_and_resources.zip",
    )


@bp.route("/projects", methods=["GET"])
def api_list_projects():
    """返回所有项目名称以及当前项目。"""

    projects = list_projects()
    return jsonify({"projects": projects, "default": get_project_from_request()})


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


@bp.route("/attachments/list")
def list_attachments():
    """返回项目附件清单及访问链接。"""

    project_name = get_project_from_request()
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    if not os.path.exists(attachments_folder):
        return api_success({"files": []})

    files = []
    for fname in os.listdir(attachments_folder):
        if fname.lower().endswith('.tex'):
            continue
        files.append({"name": fname, "url": f"/uploads/{fname}?project={project_name}"})
    return api_success({"files": files})


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
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    target = os.path.normpath(os.path.join(attachments_folder, rel))
    if not target.startswith(os.path.normpath(attachments_folder)):
        return api_error('invalid path', 400)
    if not os.path.exists(target):
        return api_error('file not found', 404)

    try:
        os.remove(target)
        return api_success()
    except Exception as exc:
        return api_error(str(exc), 500)


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

    project = load_project()
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

    url = f'/resources/{name}?project={project_name}'
    scope = 'page' if page_idx is not None else 'global'
    return api_success({'url': url, 'name': name, 'scope': scope, 'page': page_idx})


@bp.route('/resources/<path:filename>')
def serve_resource(filename):
    """提供资源文件，找不到时按文件名回退检索。"""

    project_name = get_project_from_request()
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
    _, _, _, resources_folder, _ = get_project_paths(project_name)
    direct_target = os.path.join(resources_folder, relative)
    target = direct_target if os.path.exists(direct_target) else _find_resource_file(resources_folder, name)
    if target and os.path.exists(target):
        try:
            os.remove(target)
        except Exception as exc:
            return api_error(str(exc), 500)

    project = load_project()
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

    project = load_project()
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

    files = []
    for name in names:
        sanitized = os.path.basename(str(name))
        if not sanitized:
            continue
        filepath = _find_resource_file(resources_folder, sanitized)
        files.append({
            'name': sanitized,
            'url': f'/resources/{sanitized}?project={project_name}',
            'exists': bool(filepath)
        })
    return api_success({'files': files})


@bp.route('/templates/list', methods=['GET'])
def templates_list():
    """返回可用模板列表，供前端选择。"""

    try:
        return api_success({'templates': list_templates()})
    except Exception as exc:
        return api_error(str(exc), 500)
