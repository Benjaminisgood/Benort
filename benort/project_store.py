"""项目数据的读写、清洗与目录管理工具。"""

import io
import os
import re
import shutil
import tempfile
import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional, Tuple

import yaml
from flask import current_app, request
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from .config import DEFAULT_PROJECT_NAME
from .latex import normalize_latex_content
from .template_store import get_default_header, get_default_template
from .oss_client import is_configured as oss_is_configured, upload_file as oss_upload_file


load_yaml = yaml.safe_load

# 统一配置 YAML 输出格式，确保缩进与换行一致
_yaml_writer = YAML()
_yaml_writer.default_flow_style = False
_yaml_writer.allow_unicode = True
_yaml_writer.indent(mapping=2, sequence=4, offset=2)
_yaml_writer.width = 4096

_BIB_URL_RE = re.compile(r'(url\s*=\s*[{\"])\s*([^}\"]+)([}\"])', re.IGNORECASE)


@dataclass(slots=True)
class StoredFileResult:
    filename: str
    preferred_url: str
    local_url: str
    oss_url: Optional[str]


class ProjectLockedError(PermissionError):
    """Raised when attempting to access a password-protected project without unlocking."""


def get_projects_root() -> str:
    """返回项目根目录并确保存在。"""

    root = current_app.config.get("PROJECTS_ROOT")
    if not root:
        raise RuntimeError("PROJECTS_ROOT 未在应用配置中设置")
    os.makedirs(root, exist_ok=True)
    return root


def _project_cookie_name(project_name: str) -> str:
    safe = secure_filename(project_name) or 'default'
    return f'project_token_{safe}'


def _get_secret_key() -> bytes:
    secret = current_app.secret_key
    if not secret:
        secret = os.environ.get('BENORT_SECRET_KEY', 'benort-secret')
    if isinstance(secret, bytes):
        return secret
    return str(secret).encode('utf-8')


def _build_project_token(project_name: str, password_hash: str) -> str:
    payload = f'{project_name}:{password_hash}'.encode('utf-8')
    digest = hmac.new(_get_secret_key(), payload, hashlib.sha256).hexdigest()
    return digest


def _has_valid_project_token(project_name: str, password_hash: str) -> bool:
    if not password_hash:
        return True
    cookie_name = _project_cookie_name(project_name)
    try:
        token = request.cookies.get(cookie_name)
    except RuntimeError:
        token = None
    if not token:
        return False
    expected = _build_project_token(project_name, password_hash)
    try:
        return hmac.compare_digest(token, expected)
    except Exception:
        return False


def get_local_attachments_root() -> str:
    """返回本地附件根目录。"""

    root = current_app.config.get("LOCAL_ATTACHMENTS_ROOT")
    if not root:
        raise RuntimeError("LOCAL_ATTACHMENTS_ROOT 未配置")
    os.makedirs(root, exist_ok=True)
    return root


def get_local_resources_root() -> str:
    """返回本地资源根目录。"""

    root = current_app.config.get("LOCAL_RESOURCES_ROOT")
    if not root:
        raise RuntimeError("LOCAL_RESOURCES_ROOT 未配置")
    os.makedirs(root, exist_ok=True)
    return root


def _migrate_legacy_attachments(legacy_dir: str, target_dir: str) -> None:
    """迁移旧版项目目录中的附件文件。"""

    if not os.path.isdir(legacy_dir):
        return
    for fname in os.listdir(legacy_dir):
        src = os.path.join(legacy_dir, fname)
        dst = os.path.join(target_dir, fname)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
        except Exception:
            pass


def _migrate_legacy_resources(legacy_dir: str, target_dir: str) -> None:
    """迁移旧版项目目录中的资源文件夹。"""

    if not os.path.isdir(legacy_dir):
        return
    for root, dirs, files in os.walk(legacy_dir):
        rel = os.path.relpath(root, legacy_dir)
        dest_root = target_dir if rel in (".", "") else os.path.join(target_dir, rel)
        os.makedirs(dest_root, exist_ok=True)
        for d in dirs:
            os.makedirs(os.path.join(dest_root, d), exist_ok=True)
        for fname in files:
            src = os.path.join(root, fname)
            dst = os.path.join(dest_root, fname)
            if os.path.exists(dst) or not os.path.isfile(src):
                continue
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass


def _read_project_file(yaml_path: str) -> dict:
    if not os.path.exists(yaml_path) or os.path.getsize(yaml_path) == 0:
        return {}
    with open(yaml_path, 'r', encoding='utf-8') as fh:
        data = load_yaml(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_project_file(yaml_path: str, data: dict) -> None:
    dump_ready = _prepare_yaml_for_dump(data)
    buffer = io.StringIO()
    _yaml_writer.dump(dump_ready, buffer)
    yaml_str = buffer.getvalue()
    yaml.safe_load(yaml_str)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix='project_', suffix='.yaml', dir=os.path.dirname(yaml_path))
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp_file:
            tmp_file.write(yaml_str)
        os.replace(tmp_path, yaml_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def _prepare_yaml_for_dump(value):
    """将多行字符串转换为 YAML 的字面量格式，保持换行。"""

    if isinstance(value, dict):
        return {k: _prepare_yaml_for_dump(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_prepare_yaml_for_dump(item) for item in value]
    if isinstance(value, str) and '\n' in value:
        return LiteralScalarString(value)
    return value


def _default_project_data(project_name: str) -> dict:
    return {
        'pages': [
            {
                'content': '\\begin{frame}\n\\frametitle{Title of the Slide}\n\\framesubtitle{Subtitle}\n内容...\n\\end{frame}',
                'script': '',
                'notes': '',
                'bib': [],
            }
        ],
        'template': dict(get_default_template()),
        'resources': [],
        'project': project_name,
        'ossSyncEnabled': False,
    }


def get_project_password_hash(project_name: str) -> Optional[str]:
    projects_root = get_projects_root()
    yaml_path = os.path.join(projects_root, project_name, 'project.yaml')
    data = _read_project_file(yaml_path)
    if not data:
        return None
    password_hash = data.get('passwordHash')
    if isinstance(password_hash, str) and password_hash:
        return password_hash
    return None


def issue_project_cookie(project_name: str, password_hash: str) -> Tuple[str, str]:
    return _project_cookie_name(project_name), _build_project_token(project_name, password_hash)


def get_project_cookie_name(project_name: str) -> str:
    return _project_cookie_name(project_name)


def create_project(name: str) -> None:
    ensure_project(name)
    projects_root = get_projects_root()
    yaml_path = os.path.join(projects_root, name, 'project.yaml')
    data = _default_project_data(name)
    existing = _read_project_file(yaml_path)
    if existing.get('passwordHash'):
        data['passwordHash'] = existing['passwordHash']
    _write_project_file(yaml_path, data)


def rename_project(old_name: str, new_name: str) -> None:
    if not is_safe_project_name(old_name) or not is_safe_project_name(new_name):
        raise ValueError('非法的项目名')
    if old_name == new_name:
        return
    projects_root = get_projects_root()
    old_path = os.path.join(projects_root, old_name)
    new_path = os.path.join(projects_root, new_name)
    if not os.path.isdir(old_path):
        raise FileNotFoundError('项目不存在')
    if os.path.exists(new_path):
        raise FileExistsError('目标项目已存在')

    os.rename(old_path, new_path)

    attachments_root = get_local_attachments_root()
    old_attachments = os.path.join(attachments_root, old_name)
    new_attachments = os.path.join(attachments_root, new_name)
    if os.path.exists(old_attachments):
        os.rename(old_attachments, new_attachments)

    resources_root = get_local_resources_root()
    old_resources = os.path.join(resources_root, old_name)
    new_resources = os.path.join(resources_root, new_name)
    if os.path.exists(old_resources):
        os.rename(old_resources, new_resources)

    yaml_path = os.path.join(new_path, 'project.yaml')
    data = _read_project_file(yaml_path)
    if not data:
        data = _default_project_data(new_name)
    data['project'] = new_name
    _write_project_file(yaml_path, data)


def delete_project(name: str) -> None:
    if not is_safe_project_name(name):
        raise ValueError('非法的项目名')
    projects_root = get_projects_root()
    proj_path = os.path.join(projects_root, name)
    if os.path.isdir(proj_path):
        shutil.rmtree(proj_path, ignore_errors=False)

    attachments_root = get_local_attachments_root()
    attachments_path = os.path.join(attachments_root, name)
    if os.path.isdir(attachments_path):
        shutil.rmtree(attachments_path, ignore_errors=False)

    resources_root = get_local_resources_root()
    resources_path = os.path.join(resources_root, name)
    if os.path.isdir(resources_path):
        shutil.rmtree(resources_path, ignore_errors=False)


def set_project_password(name: str, new_password: str, current_password: Optional[str] = None) -> None:
    if not new_password:
        raise ValueError('密码不能为空')
    projects_root = get_projects_root()
    yaml_path = os.path.join(projects_root, name, 'project.yaml')
    data = _read_project_file(yaml_path)
    if not data:
        data = _default_project_data(name)
    existing_hash = data.get('passwordHash')
    if existing_hash:
        if current_password:
            if not check_password_hash(existing_hash, current_password):
                raise PermissionError('原密码不正确')
        else:
            # allow if current token valid
            if not _has_valid_project_token(name, existing_hash):
                raise PermissionError('需要提供当前密码')
    data['passwordHash'] = generate_password_hash(new_password)
    _write_project_file(yaml_path, data)


def clear_project_password(name: str, current_password: Optional[str] = None) -> None:
    projects_root = get_projects_root()
    yaml_path = os.path.join(projects_root, name, 'project.yaml')
    data = _read_project_file(yaml_path)
    if not data:
        data = _default_project_data(name)
    existing_hash = data.get('passwordHash')
    if not existing_hash:
        return
    if current_password:
        if not check_password_hash(existing_hash, current_password):
            raise PermissionError('原密码不正确')
    else:
        if not _has_valid_project_token(name, existing_hash):
            raise PermissionError('需要提供当前密码')
    data.pop('passwordHash', None)
    _write_project_file(yaml_path, data)


def verify_project_password(name: str, password: str) -> bool:
    password_hash = get_project_password_hash(name)
    if not password_hash:
        return True
    if not password:
        return False
    return check_password_hash(password_hash, password)


def get_project_metadata(name: str) -> dict:
    password_hash = get_project_password_hash(name)
    locked = bool(password_hash)
    unlocked = locked and _has_valid_project_token(name, password_hash or '')
    return {
        'locked': locked,
        'unlocked': unlocked,
    }


def _dedupe_preserve(items):
    """去重但保留原有顺序，用于资源/文献列表。"""

    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _strip_url_query_fragment(url: str) -> str:
    """移除 URL query 与 fragment，便于存储。"""

    if not isinstance(url, str):
        return url
    trimmed = url.strip()
    if not trimmed:
        return trimmed
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(trimmed)
        if parts.scheme and parts.netloc:
            cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
            if cleaned:
                return cleaned
    except ValueError:
        pass
    no_query = trimmed.split('?', 1)[0]
    no_fragment = no_query.split('#', 1)[0]
    return no_fragment


def _sanitize_bib_entry(entry):
    """清洗单条 bib 文本，修复 URL 与大括号匹配问题。"""

    if not isinstance(entry, str):
        return entry

    entry = entry.replace('\r\n', '\n')
    entry = '\n'.join(line.rstrip() for line in entry.split('\n'))

    def _repl(match):
        prefix, url_value, suffix = match.groups()
        cleaned = _strip_url_query_fragment(url_value)
        if not cleaned:
            return match.group(0)
        return f"{prefix}{cleaned}{suffix}"

    sanitized = _BIB_URL_RE.sub(_repl, entry)
    sanitized = sanitized.rstrip()
    opens = sanitized.count('{')
    closes = sanitized.count('}')

    while closes > opens and sanitized.endswith('}'):
        sanitized = sanitized[:-1].rstrip()
        closes -= 1

    if closes < opens:
        missing = opens - closes
        append_str = '\n'.join('}' for _ in range(missing))
        if append_str:
            if sanitized and not sanitized.endswith('\n'):
                sanitized += '\n'
            sanitized += append_str
    return sanitized


def _sanitize_bib_list(items):
    """批量处理 bib 列表，过滤空值并去重。"""

    if not isinstance(items, list):
        return []
    cleaned = []
    for entry in items:
        if not isinstance(entry, str):
            continue
        sanitized = _sanitize_bib_entry(entry.strip())
        if sanitized:
            cleaned.append(sanitized)
    return _dedupe_preserve(cleaned)


def _sanitize_resource_list(resources):
    """仅保留资源文件名并去重。"""

    cleaned = []
    if not isinstance(resources, list):
        return cleaned
    for item in resources:
        if not isinstance(item, str):
            continue
        name = os.path.basename(item.strip())
        if not name:
            continue
        cleaned.append(name)
    return _dedupe_preserve(cleaned)


def _canonicalize_page(page):
    """将单页结构转换为标准字段格式。"""

    if isinstance(page, str):
        return {
            'content': page,
            'script': '',
            'notes': '',
            'bib': [],
        }
    if not isinstance(page, dict):
        page = {}
    content = page.get('content') or ''
    script = page.get('script') or ''
    notes = page.get('notes') or ''
    bib = _sanitize_bib_list(page.get('bib', []))
    resources = _sanitize_resource_list(page.get('resources', []))
    sanitized = {
        'content': content,
        'script': script,
        'notes': notes,
        'bib': bib,
    }
    if resources:
        sanitized['resources'] = resources
    return sanitized


def _clean_template_text(value: str, default: str) -> str:
    """Remove stray backslashes and blank lines from template snippets."""

    if not isinstance(value, str):
        value = ''
    text = value.replace('\r\n', '\n')
    lines = []
    for line in text.split('\n'):
        if line.strip() == '\\':
            continue
        lines.append(line)
    cleaned = '\n'.join(lines).strip()
    return cleaned or default


def _canonicalize_project_structure(data):
    """统一项目结构，补齐模板/资源字段，便于后续处理。"""

    if not isinstance(data, dict):
        data = {}

    default_template = get_default_template()
    default_header = default_template.get('header', get_default_header())
    default_before = default_template.get('beforePages', '\\begin{document}')
    default_footer = default_template.get('footer', '\\end{document}')

    pages = data.get('pages', [])
    if not isinstance(pages, list):
        pages = []
    sanitized_pages = [_canonicalize_page(p) for p in pages]
    if not sanitized_pages:
        sanitized_pages = [{
            'content': '',
            'script': '',
            'notes': '',
            'bib': [],
        }]
    data['pages'] = sanitized_pages

    template = data.get('template')
    if isinstance(template, str):
        template = {'header': template}
    if not isinstance(template, dict):
        template = {}
    template['header'] = _clean_template_text(template.get('header'), default_header)
    template['beforePages'] = _clean_template_text(template.get('beforePages'), default_before)
    template['footer'] = _clean_template_text(template.get('footer'), default_footer)
    data['template'] = template

    global_resources = _sanitize_resource_list(data.get('resources', []))
    if global_resources:
        data['resources'] = global_resources
    elif 'resources' in data:
        data.pop('resources', None)

    global_bib = _sanitize_bib_list(data.get('bib', []))
    if global_bib:
        data['bib'] = global_bib
    elif 'bib' in data:
        data.pop('bib', None)

    if not data.get('project'):
        try:
            data['project'] = get_project_from_request()
        except RuntimeError:
            data['project'] = DEFAULT_PROJECT_NAME

    if 'ossSyncEnabled' in data:
        data['ossSyncEnabled'] = bool(data['ossSyncEnabled'])
    else:
        data['ossSyncEnabled'] = False

    return data


def list_projects():
    """列出当前 projects 目录下的所有项目。"""

    projects_root = get_projects_root()
    try:
        return sorted(
            [
                d
                for d in os.listdir(projects_root)
                if os.path.isdir(os.path.join(projects_root, d)) and not d.startswith('.')
            ]
        )
    except Exception:
        return []


def is_safe_project_name(name: str) -> bool:
    """校验项目名合法性，防止路径穿越。"""

    if not name:
        return False
    if os.path.sep in name or '/' in name or '\\' in name:
        return False
    if name in ('..', '.', ''):
        return False
    return True


def ensure_project(name: str):
    """确保项目目录结构存在，缺失时自动创建。"""

    if not is_safe_project_name(name):
        raise ValueError('非法的项目名')

    projects_root = get_projects_root()
    proj_path = os.path.join(projects_root, name)
    static = os.path.join(proj_path, 'static')
    build = os.path.join(proj_path, 'build')
    os.makedirs(static, exist_ok=True)
    os.makedirs(build, exist_ok=True)

    attachments_root = get_local_attachments_root()
    attachments = os.path.join(attachments_root, name)
    os.makedirs(attachments, exist_ok=True)
    legacy_attachments = os.path.join(proj_path, 'attachments')
    _migrate_legacy_attachments(legacy_attachments, attachments)

    resources_root = get_local_resources_root()
    resources = os.path.join(resources_root, name)
    os.makedirs(resources, exist_ok=True)
    legacy_resources = os.path.join(proj_path, 'resources')
    _migrate_legacy_resources(legacy_resources, resources)

    yaml_path = os.path.join(proj_path, 'project.yaml')
    if not os.path.exists(yaml_path):
        with open(yaml_path, 'w', encoding='utf-8') as f:
            f.write('')
    return proj_path, attachments, static, yaml_path, resources, build


def get_project_paths(project_name: str):
    """返回指定项目下常用目录路径，并确保已创建。"""

    projects_root = get_projects_root()
    proj_path = os.path.join(projects_root, project_name)
    static = os.path.join(proj_path, 'static')
    build = os.path.join(proj_path, 'build')
    yaml_path = os.path.join(proj_path, 'project.yaml')
    os.makedirs(static, exist_ok=True)
    os.makedirs(build, exist_ok=True)

    attachments_root = get_local_attachments_root()
    attachments = os.path.join(attachments_root, project_name)
    os.makedirs(attachments, exist_ok=True)
    legacy_attachments = os.path.join(proj_path, 'attachments')
    _migrate_legacy_attachments(legacy_attachments, attachments)

    resources_root = get_local_resources_root()
    resources = os.path.join(resources_root, project_name)
    os.makedirs(resources, exist_ok=True)
    legacy_resources = os.path.join(proj_path, 'resources')
    _migrate_legacy_resources(legacy_resources, resources)

    return attachments, static, yaml_path, resources, build


def get_project_from_request():
    """综合请求参数、JSON、环境变量获取当前项目名。"""

    name = None
    try:
        name = request.args.get('project')
    except RuntimeError:
        name = None
    if not name:
        try:
            if request.is_json:
                payload = request.get_json(silent=True) or {}
                name = payload.get('project')
        except RuntimeError:
            name = None
    if not name:
        name = os.environ.get('DEFAULT_PROJECT', DEFAULT_PROJECT_NAME)
    if not is_safe_project_name(name):
        name = DEFAULT_PROJECT_NAME
    projects = list_projects()
    if name not in projects:
        name = projects[0] if projects else DEFAULT_PROJECT_NAME
        ensure_project(name)
    return name


def _store_attachment_file(file_storage, project_name: str) -> StoredFileResult:
    """保存上传的附件文件并返回访问信息。"""

    if not file_storage or not file_storage.filename:
        raise ValueError('No selected file')
    filename = secure_filename(file_storage.filename)
    if not filename:
        raise ValueError('Invalid filename')
    attachments_folder, _, _, _, _ = get_project_paths(project_name)
    save_path = os.path.join(attachments_folder, filename)
    file_storage.save(save_path)

    local_url = f'/projects/{project_name}/uploads/{filename}'
    preferred_url = local_url
    oss_url: Optional[str] = None

    project_data: dict | None = None
    try:
        project_data = load_project()
    except Exception:
        project_data = None

    sync_enabled = bool(project_data.get('ossSyncEnabled')) if isinstance(project_data, dict) else False

    if sync_enabled and oss_is_configured():
        try:
            oss_url = oss_upload_file(project_name, filename, save_path)
            if oss_url:
                preferred_url = oss_url
        except Exception as exc:
            try:
                current_app.logger.warning('OSS 上传失败 %s: %s', filename, exc)
            except Exception:
                print(f'OSS 上传失败 {filename}: {exc}')

    return StoredFileResult(
        filename=filename,
        preferred_url=preferred_url,
        local_url=local_url,
        oss_url=oss_url,
    )


def load_project():
    """从 YAML 载入项目数据，并处理兼容迁移逻辑。"""

    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)
    data = _read_project_file(yaml_path)
    if not data:
        data = _default_project_data(project_name)

    password_hash = data.get('passwordHash')
    if password_hash and not _has_valid_project_token(project_name, password_hash):
        raise ProjectLockedError(project_name)

    if data and isinstance(data.get('pages'), list) and (not data['pages'] or isinstance(data['pages'][0], str)):
        # 兼容旧版本仅存储字符串列表的项目结构
        old_notes = data.get('notes', [])
        bib = data.get('page_bib', [])
        new_pages = []
        for i, page in enumerate(data['pages']):
            new_pages.append({
                'content': page,
                'script': old_notes[i] if i < len(old_notes) else '',
                'notes': '',
                'bib': bib[i] if i < len(bib) else [],
            })
        data['pages'] = new_pages
        data.pop('notes', None)
        data.pop('page_bib', None)

    migrated = False
    data = _canonicalize_project_structure(data)

    for idx, page in enumerate(data['pages']):
        page_dir = os.path.join(resources_folder, f'page_{idx+1}')
        if not os.path.isdir(page_dir):
            continue
        existing = set(page.get('resources', []))
        added = []
        for fname in sorted(os.listdir(page_dir)):
            if fname in existing:
                continue
            added.append(fname)
        if added:
            page.setdefault('resources', [])
            page['resources'].extend(added)
            page['resources'] = _sanitize_resource_list(page['resources'])
            migrated = True

    global_dir = os.path.join(resources_folder, 'global')
    if os.path.isdir(global_dir):
        existing = set(data.get('resources', []))
        added = []
        for fname in sorted(os.listdir(global_dir)):
            if fname in existing:
                continue
            added.append(fname)
        if added:
            combined = data.get('resources', []) + added
            data['resources'] = _sanitize_resource_list(combined)
            migrated = True

    data = _canonicalize_project_structure(data)
    if 'passwordHash' in data:
        data.pop('passwordHash', None)
    if migrated:
        save_project(data)
    return data


def save_project(data):
    """规范化后写回项目 YAML。"""

    data = _canonicalize_project_structure(dict(data or {}))
    project_name = get_project_from_request()
    attachments_folder, _, yaml_path, resources_folder, _ = get_project_paths(project_name)
    existing_raw = _read_project_file(yaml_path)
    existing_hash = existing_raw.get('passwordHash') if isinstance(existing_raw, dict) else None
    new_hash = data.pop('passwordHash', None)
    password_hash = new_hash if new_hash is not None else existing_hash

    for page in data['pages']:
        page['content'] = normalize_latex_content(page.get('content', ''), attachments_folder, resources_folder) or ''
        page['script'] = page.get('script', '') or ''
        page['notes'] = page.get('notes', '') or ''
        page['bib'] = _sanitize_bib_list(page.get('bib', []))
        if 'resources' in page:
            sanitized = _sanitize_resource_list(page['resources'])
            if sanitized:
                page['resources'] = sanitized
            else:
                page.pop('resources', None)

    global_resources = _sanitize_resource_list(data.get('resources', []))
    if global_resources:
        data['resources'] = global_resources
    elif 'resources' in data:
        data.pop('resources', None)

    if 'bib' in data:
        global_bib = _sanitize_bib_list(data.get('bib', []))
        if global_bib:
            data['bib'] = global_bib
        else:
            data.pop('bib', None)

    template = data.get('template')
    default_template = get_default_template()
    default_header = default_template.get('header', get_default_header())
    default_before = default_template.get('beforePages', '\\begin{document}')
    default_footer = default_template.get('footer', '\\end{document}')

    if isinstance(template, dict):
        template['header'] = normalize_latex_content(template.get('header', ''), attachments_folder, resources_folder) or default_header
        template['beforePages'] = normalize_latex_content(template.get('beforePages', ''), attachments_folder, resources_folder) or default_before
        template['footer'] = normalize_latex_content(template.get('footer', ''), attachments_folder, resources_folder) or default_footer
    elif isinstance(template, str):
        data['template'] = {
            'header': normalize_latex_content(template, attachments_folder, resources_folder) or default_header,
            'beforePages': default_before,
            'footer': default_footer,
        }
    else:
        data['template'] = {
            'header': default_header,
            'beforePages': default_before,
            'footer': default_footer,
        }

    data = _canonicalize_project_structure(data)

    if password_hash:
        data['passwordHash'] = password_hash

    try:
        _write_project_file(yaml_path, data)
    except yaml.YAMLError as err:
        print('YAML序列化校验失败:', err)
        raise Exception('YAML序列化校验失败，未保存。请检查内容格式。')

    if bool(data.get('ossSyncEnabled')) and oss_is_configured():
        try:
            oss_upload_file(project_name, 'project.yaml', yaml_path, category='yaml')
        except Exception as exc:
            try:
                current_app.logger.warning('OSS 上传项目 YAML 失败: %s', exc)
            except Exception:
                print(f'OSS 上传项目 YAML 失败: {exc}')


__all__ = [
    'DEFAULT_PROJECT_NAME',
    'StoredFileResult',
    'ProjectLockedError',
    'list_projects',
    'is_safe_project_name',
    'ensure_project',
    'get_project_paths',
    'get_local_attachments_root',
    'get_local_resources_root',
    'get_project_from_request',
    'load_project',
    'save_project',
    '_store_attachment_file',
    '_sanitize_resource_list',
    '_sanitize_bib_list',
    'create_project',
    'rename_project',
    'delete_project',
    'set_project_password',
    'clear_project_password',
    'verify_project_password',
    'get_project_password_hash',
    'issue_project_cookie',
    'get_project_cookie_name',
    'get_project_metadata',
]
