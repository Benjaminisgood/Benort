"""Aliyun OSS helper utilities for attachment sync."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Dict, Optional

from flask import current_app

try:  # pragma: no cover - optional dependency guard
    import oss2  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - handled at runtime
    oss2 = None  # type: ignore


@dataclass(slots=True)
class OSSSettings:
    endpoint: str
    access_key_id: str
    access_key_secret: str
    bucket_name: str
    prefix: str
    public_base_url: Optional[str]

DEFAULT_CATEGORY = "attachments"


def _clean_prefix(prefix: str) -> str:
    if not prefix:
        return DEFAULT_CATEGORY
    cleaned = prefix.strip().strip("/")
    return cleaned or DEFAULT_CATEGORY


def _normalize_category(category: Optional[str]) -> str:
    if category is None:
        return DEFAULT_CATEGORY
    normalized = category.strip().strip("/")
    return normalized or DEFAULT_CATEGORY


def _category_segments(category: str) -> list[str]:
    if category == "yaml":
        return [".yaml"]
    if category in {"attachments", "resources"}:
        return [category]
    return [category]


def _base_segments(settings: OSSSettings, project_name: str) -> list[str]:
    prefix = settings.prefix.strip("/")
    project = project_name.strip().strip("/")
    segments: list[str] = []
    if prefix:
        segments.append(prefix)
    if project:
        segments.append(project)
    return segments


def _legacy_object_keys(
    settings: OSSSettings, project_name: str, filename: str, category: Optional[str]
) -> list[str]:
    name = filename.strip().lstrip("/")
    if not name:
        return []
    normalized_category = _normalize_category(category)
    prefix = settings.prefix.strip("/")
    project = project_name.strip().strip("/")

    segments: list[str] = []
    if prefix:
        segments.append(prefix)

    legacy_category = None if normalized_category == "attachments" else normalized_category
    if legacy_category == "yaml":
        legacy_category = "yaml"

    if legacy_category:
        segments.append(legacy_category)
    if project:
        segments.append(project)
    segments.append(name)

    key = "/".join(filter(None, segments))
    return [key] if key else []


def get_settings() -> Optional[OSSSettings]:
    """Read OSS configuration from the Flask app context."""

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    endpoint = app.config.get("ALIYUN_OSS_ENDPOINT") or os.environ.get("ALIYUN_OSS_ENDPOINT")
    access_key_id = app.config.get("ALIYUN_OSS_ACCESS_KEY_ID") or os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID")
    access_key_secret = app.config.get("ALIYUN_OSS_ACCESS_KEY_SECRET") or os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET")
    bucket_name = app.config.get("ALIYUN_OSS_BUCKET") or os.environ.get("ALIYUN_OSS_BUCKET")
    prefix = app.config.get("ALIYUN_OSS_PREFIX") or os.environ.get("ALIYUN_OSS_PREFIX") or "attachments"
    public_base_url = app.config.get("ALIYUN_OSS_PUBLIC_BASE_URL") or os.environ.get("ALIYUN_OSS_PUBLIC_BASE_URL")

    if not all([endpoint, access_key_id, access_key_secret, bucket_name]):
        return None
    if oss2 is None:
        raise RuntimeError("oss2 库未安装，无法使用 OSS 同步功能")

    return OSSSettings(
        endpoint=str(endpoint),
        access_key_id=str(access_key_id),
        access_key_secret=str(access_key_secret),
        bucket_name=str(bucket_name),
        prefix=_clean_prefix(str(prefix)),
        public_base_url=str(public_base_url) if public_base_url else None,
    )


def is_configured() -> bool:
    """Return True when OSS sync can be used."""

    try:
        return get_settings() is not None
    except RuntimeError:
        return False


def _get_bucket(settings: OSSSettings):
    auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
    return oss2.Bucket(auth, settings.endpoint, settings.bucket_name)


def _object_key(
    settings: OSSSettings,
    project_name: str,
    filename: str,
    category: Optional[str] = None,
) -> str:
    name = filename.strip().lstrip("/")
    normalized_category = _normalize_category(category)
    segments = _base_segments(settings, project_name)
    segments.extend(_category_segments(normalized_category))
    if name:
        segments.append(name)
    key = "/".join(filter(None, segments))
    return key


def _object_prefix(settings: OSSSettings, project_name: str, category: Optional[str] = None) -> str:
    key = _object_key(settings, project_name, "", category)
    if key and not key.endswith("/"):
        key = f"{key}/"
    return key


def build_public_url(settings: OSSSettings, key: str) -> str:
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        return f"{base}/{key}"
    endpoint = settings.endpoint.lstrip("http://").lstrip("https://")
    return f"https://{settings.bucket_name}.{endpoint}/{key}"


def upload_file(project_name: str, filename: str, local_path: str, category: Optional[str] = None) -> Optional[str]:
    """Upload a local file to OSS and return its public URL."""

    settings = get_settings()
    if not settings:
        return None
    bucket = _get_bucket(settings)
    key = _object_key(settings, project_name, filename, category)

    with open(local_path, "rb") as fh:
        bucket.put_object(key, fh)

    for legacy_key in _legacy_object_keys(settings, project_name, filename, category):
        if legacy_key == key:
            continue
        try:  # pragma: no cover - best effort cleanup
            bucket.delete_object(legacy_key)
        except Exception:
            pass
    return build_public_url(settings, key)


def delete_file(project_name: str, filename: str, category: Optional[str] = None) -> None:
    """Delete an attachment from OSS."""

    settings = get_settings()
    if not settings:
        return
    bucket = _get_bucket(settings)
    key = _object_key(settings, project_name, filename, category)
    bucket.delete_object(key)
    for legacy_key in _legacy_object_keys(settings, project_name, filename, category):
        if legacy_key == key:
            continue
        try:  # pragma: no cover - best effort cleanup
            bucket.delete_object(legacy_key)
        except Exception:
            pass


def list_files(project_name: str, category: Optional[str] = None, with_meta: bool = False) -> Dict[str, object]:
    """Return a map of filename to public URL for OSS objects under the project."""

    settings = get_settings()
    if not settings:
        return {}
    bucket = _get_bucket(settings)
    prefix = _object_prefix(settings, project_name, category)
    results: Dict[str, object] = {}
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        if not obj.key or obj.key.endswith("/"):
            continue
        rel = obj.key[len(prefix) :]
        if not rel:
            continue
        if with_meta:
            results[rel] = {
                "url": build_public_url(settings, obj.key),
                "etag": getattr(obj, "etag", None),
                "size": getattr(obj, "size", None),
                "last_modified": getattr(obj, "last_modified", None),
            }
        else:
            results[rel] = build_public_url(settings, obj.key)
    return results


def _file_md5(path: str) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        hasher = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def pull_directory(
    project_name: str,
    local_dir: str,
    *,
    category: Optional[str] = None,
    overwrite: bool = True,
    delete_local_extras: bool = False,
) -> dict:
    """Download OSS directory content to local disk."""

    settings = get_settings()
    if not settings:
        return {"error": "OSS 未配置"}
    bucket = _get_bucket(settings)
    prefix = _object_prefix(settings, project_name, category)
    os.makedirs(local_dir, exist_ok=True)

    downloaded: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    removed: list[str] = []
    remote_keys: dict[str, str] = {}

    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        key = getattr(obj, "key", "")
        if not key or key.endswith("/"):
            continue
        rel = key[len(prefix) :].lstrip("/")
        if not rel:
            continue
        remote_keys[rel] = key
        dest_path = os.path.join(local_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if not overwrite and os.path.exists(dest_path):
            skipped.append(rel)
            continue
        try:
            bucket.get_object_to_file(key, dest_path)
            downloaded.append(rel)
        except Exception:  # pragma: no cover - runtime network errors
            failed.append(rel)

    if delete_local_extras:
        for root, _, files in os.walk(local_dir):
            for fname in files:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, local_dir)
                normalized = rel_path.replace(os.sep, "/")
                if normalized not in remote_keys:
                    try:
                        os.remove(abs_path)
                        removed.append(normalized)
                    except Exception:
                        failed.append(normalized)

    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "removed": removed,
        "failed": failed,
    }


def pull_file(
    project_name: str,
    filename: str,
    local_path: str,
    *,
    category: Optional[str] = None,
    overwrite: bool = True,
) -> dict:
    """Download a single file from OSS."""

    settings = get_settings()
    if not settings:
        return {"downloaded": False, "error": "OSS 未配置"}
    bucket = _get_bucket(settings)
    key = _object_key(settings, project_name, filename, category)
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    if not overwrite and os.path.exists(local_path):
        return {"downloaded": False, "skipped": True, "path": local_path}

    try:
        bucket.get_object_to_file(key, local_path)
        return {"downloaded": True, "path": local_path}
    except Exception as exc:  # pragma: no cover - network errors
        if oss2 is not None:
            no_such_key = getattr(getattr(oss2, "exceptions", object), "NoSuchKey", None)
            if no_such_key and isinstance(exc, no_such_key):
                return {"downloaded": False, "error": "OSS 上未找到文件"}
        return {"downloaded": False, "error": str(exc)}


def diff_directory(project_name: str, local_dir: str, category: Optional[str] = None) -> dict:
    """Compare local directory with OSS contents and report differences."""

    settings = get_settings()
    if not settings:
        return {"error": "OSS 未配置"}
    os.makedirs(local_dir, exist_ok=True)

    remote_meta = list_files(project_name, category, with_meta=True)
    remote_map = {key: value for key, value in remote_meta.items() if isinstance(key, str)}

    local_map: dict[str, dict[str, object]] = {}
    for root, _, files in os.walk(local_dir):
        for fname in files:
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, local_dir).replace(os.sep, "/")
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                size = None
            local_map[rel_path] = {
                "size": size,
                "md5": _file_md5(abs_path),
            }

    only_local = sorted([path for path in local_map if path not in remote_map])
    only_remote = sorted([path for path in remote_map if path not in local_map])
    differing: list[dict[str, object]] = []

    shared_keys = sorted(set(local_map.keys()) & set(remote_map.keys()))
    for key in shared_keys:
        local_info = local_map.get(key) or {}
        remote_info = remote_map.get(key) or {}
        local_size = local_info.get("size")
        local_md5 = (local_info.get("md5") or "") if isinstance(local_info.get("md5"), str) else None
        remote_size = remote_info.get("size") if isinstance(remote_info, dict) else None
        remote_etag = remote_info.get("etag") if isinstance(remote_info, dict) else None
        if isinstance(remote_etag, str):
            remote_etag = remote_etag.strip('"')

        size_mismatch = (
            local_size is not None
            and remote_size is not None
            and int(remote_size) != int(local_size)
        )
        hash_mismatch = (
            local_md5
            and remote_etag
            and local_md5.lower() != str(remote_etag).lower()
        )
        if size_mismatch or hash_mismatch:
            differing.append(
                {
                    "path": key,
                    "localSize": local_size,
                    "remoteSize": remote_size,
                    "localMd5": local_md5,
                    "remoteEtag": remote_etag,
                }
            )

    return {
        "onlyLocal": only_local,
        "onlyRemote": only_remote,
        "different": differing,
    }


def diff_file(
    project_name: str,
    local_path: str,
    filename: str,
    *,
    category: Optional[str] = None,
) -> dict:
    """Compare a single file between local storage and OSS."""

    settings = get_settings()
    if not settings:
        return {"error": "OSS 未配置"}

    remote_meta = list_files(project_name, category, with_meta=True)
    remote_info = remote_meta.get(filename) if isinstance(remote_meta, dict) else None

    local_exists = os.path.isfile(local_path)
    remote_exists = isinstance(remote_info, dict)

    result: dict[str, object] = {
        "localExists": local_exists,
        "remoteExists": remote_exists,
    }

    local_size = os.path.getsize(local_path) if local_exists else None
    local_md5 = _file_md5(local_path) if local_exists else None
    remote_size = remote_info.get("size") if isinstance(remote_info, dict) else None
    remote_etag = remote_info.get("etag") if isinstance(remote_info, dict) else None
    if isinstance(remote_etag, str):
        remote_etag = remote_etag.strip('"')

    result.update(
        {
            "localSize": local_size,
            "remoteSize": remote_size,
            "localMd5": local_md5,
            "remoteEtag": remote_etag,
        }
    )

    different = False
    if local_exists != remote_exists:
        different = True
    else:
        if (
            local_size is not None
            and remote_size is not None
            and int(remote_size) != int(local_size)
        ):
            different = True
        elif local_md5 and remote_etag and local_md5.lower() != str(remote_etag).lower():
            different = True

    result["different"] = different
    return result


def sync_directory(project_name: str, local_dir: str, delete_remote_extras: bool = True, category: Optional[str] = None) -> dict:
    """Upload all local files and optionally prune remote extras."""

    settings = get_settings()
    if not settings:
        return {"uploaded": [], "removed": [], "failed": []}

    bucket = _get_bucket(settings)
    prefix = _object_prefix(settings, project_name, category)
    existing_remote = list_files(project_name, category)
    uploaded: list[str] = []
    failed: list[str] = []
    removed: list[str] = []

    local_files: list[str] = []
    for root, _, files in os.walk(local_dir):
        for fname in files:
            rel_root = os.path.relpath(root, local_dir)
            if rel_root in (".", ""):
                rel_path = fname
            else:
                rel_path = os.path.join(rel_root, fname)
            normalized = rel_path.replace(os.sep, "/")
            local_files.append(normalized)

    for rel_path in local_files:
        key = _object_key(settings, project_name, rel_path, category)
        try:
            with open(os.path.join(local_dir, rel_path.replace("/", os.sep)), "rb") as fh:
                bucket.put_object(key, fh)
            uploaded.append(rel_path)
            for legacy_key in _legacy_object_keys(settings, project_name, rel_path, category):
                if legacy_key == key:
                    continue
                try:  # pragma: no cover - best effort cleanup
                    bucket.delete_object(legacy_key)
                except Exception:
                    pass
        except Exception:  # pragma: no cover - best effort logging handled by caller
            failed.append(rel_path)

    if delete_remote_extras:
        for fname in existing_remote:
            if fname in local_files:
                continue
            try:
                bucket.delete_object(f"{prefix}{fname}")
                removed.append(fname)
            except Exception:  # pragma: no cover - ignore
                failed.append(fname)

    return {
        "uploaded": uploaded,
        "removed": removed,
        "failed": failed,
        "public": {
            path: build_public_url(
                settings,
                _object_key(settings, project_name, path.replace(os.sep, "/"), category),
            )
            for path in local_files
        },
    }
