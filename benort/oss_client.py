"""Aliyun OSS helper utilities for attachment sync."""

from __future__ import annotations

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


def list_files(project_name: str, category: Optional[str] = None) -> Dict[str, str]:
    """Return a map of filename to public URL for OSS objects under the project."""

    settings = get_settings()
    if not settings:
        return {}
    bucket = _get_bucket(settings)
    prefix = _object_prefix(settings, project_name, category)
    results: Dict[str, str] = {}
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        if not obj.key or obj.key.endswith("/"):
            continue
        rel = obj.key[len(prefix) :]
        if not rel:
            continue
        results[rel] = build_public_url(settings, obj.key)
    return results


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
