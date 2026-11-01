"""Local persistence for the optional model API configuration.

Non-secret fields are stored in the user's application-data directory.  The
API key is protected with Windows DPAPI before it is written, so it is never
stored as plaintext in the project database or settings file.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

from agent.api import APIConfig


class LocalAPISettingsError(RuntimeError):
    """Raised when local API settings cannot be safely persisted."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _settings_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.getenv("GAMETUNE_API_SETTINGS_FILE", "").strip()
    if override:
        return Path(override)
    app_data = os.getenv("APPDATA", "").strip()
    root = Path(app_data) if app_data else Path.home() / ".config"
    return root / "GameTune Agent" / "api_settings.json"


def _crypt32():
    if os.name != "nt":
        raise LocalAPISettingsError("只有 Windows 系统支持安全保存 API 密钥")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise LocalAPISettingsError("无法加载 Windows 本机加密组件") from error
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def _protect(value: str) -> str:
    crypt32, kernel32 = _crypt32()
    raw = value.encode("utf-8")
    source = ctypes.create_string_buffer(raw)
    source_blob = _DataBlob(
        len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte))
    )
    target_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(target_blob),
    ):
        raise LocalAPISettingsError(
            f"Windows 无法保护 API 密钥（错误码 {ctypes.get_last_error()}）"
        )
    try:
        protected = ctypes.string_at(target_blob.pbData, target_blob.cbData)
    finally:
        kernel32.LocalFree(target_blob.pbData)
    return base64.b64encode(protected).decode("ascii")


def _unprotect(value: str) -> str:
    crypt32, kernel32 = _crypt32()
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise LocalAPISettingsError("本机保存的 API 密钥格式无效") from error
    source = ctypes.create_string_buffer(raw)
    source_blob = _DataBlob(
        len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte))
    )
    target_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(target_blob),
    ):
        raise LocalAPISettingsError("无法用当前 Windows 账户解锁本机 API 密钥")
    try:
        return ctypes.string_at(target_blob.pbData, target_blob.cbData).decode("utf-8")
    except UnicodeDecodeError as error:
        raise LocalAPISettingsError("本机保存的 API 密钥无法解码") from error
    finally:
        kernel32.LocalFree(target_blob.pbData)


def save_saved_api_config(config: APIConfig, path: str | Path | None = None) -> Path:
    """Save an API config, protecting its key with the current Windows user."""
    target = _settings_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "base_url": config.base_url,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "reasoning_effort": config.reasoning_effort,
        "api_key_protected": _protect(config.api_key),
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise LocalAPISettingsError("无法写入本机 API 配置") from error
    return target


def load_saved_api_config(path: str | Path | None = None) -> APIConfig | None:
    """Load a previously saved config; invalid or unavailable data is ignored."""
    target = _settings_path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    protected_key = payload.get("api_key_protected")
    if not isinstance(protected_key, str) or not protected_key.strip():
        return None
    try:
        api_key = _unprotect(protected_key)
        timeout = float(payload.get("timeout_seconds", 30))
        reasoning = payload.get("reasoning_effort")
        reasoning = reasoning if reasoning in {None, "low", "medium", "high"} else None
        return APIConfig(
            str(payload.get("base_url", "")),
            api_key,
            str(payload.get("model", "")),
            timeout,
            reasoning,
        )
    except (LocalAPISettingsError, TypeError, ValueError):
        return None


def clear_saved_api_config(path: str | Path | None = None) -> None:
    """Remove the locally saved API configuration if it exists."""
    try:
        _settings_path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise LocalAPISettingsError("无法清除本机 API 配置") from error
