"""Helpers for consistent API responses."""

from flask import jsonify


def api_success(data: dict | None = None):
    """返回标准成功响应，可附带额外数据。"""
    payload = {"success": True}
    if data:
        payload.update(data)
    return jsonify(payload)


def api_error(message: str, status: int = 400):
    """返回错误响应，并指定 HTTP 状态码。"""
    return jsonify({"success": False, "error": message}), status
