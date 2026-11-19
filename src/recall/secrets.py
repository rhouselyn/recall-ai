"""Local credential storage and compatibility with existing Windows keys."""
from __future__ import annotations

import ctypes
import os
import tempfile
from pathlib import Path


def _secret_path(name: str) -> Path | None:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return None
    if name not in {"deepseek", "github"}:
        raise ValueError("Unknown credential")
    return Path(os.environ["LOCALAPPDATA"]) / "RecallAI" / f"{name}.dpapi"


def load_secret(name: str) -> str | None:
    path = _secret_path(name)
    return read_credential(path) if path is not None else None


def read_credential(path: Path) -> str | None:
    try:
        encrypted = path.read_bytes()
        if os.name != "nt":
            return encrypted.decode("utf-8")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(encrypted)
        source = Blob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.windll.crypt32
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)):
            return None
        try:
            return ctypes.string_at(output.data, output.size).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(output.data)
    except (OSError, UnicodeError, ValueError):
        return None


def save_secret(name: str, value: str) -> bool:
    path = _secret_path(name)
    if path is None:
        return False
    write_credential(path, value)
    return True


def write_credential(path: Path, value: str) -> None:
    raw = value.encode("utf-8")
    data = _encrypt(raw) if os.name == "nt" else raw
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".recall-credential-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.close()
            os.replace(temporary, path)
        finally:
            file.close()
            temporary.unlink(missing_ok=True)


def _encrypt(raw: bytes) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("Credential encryption failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(output.data)


def deepseek_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY") or load_secret("deepseek")
