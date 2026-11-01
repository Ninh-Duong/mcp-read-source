"""Encryption at rest for ai-context. Only `store.py` may import this module.

A random 256-bit key is generated on first use and stored in `ai-context/.key`, wrapped with
Windows DPAPI (CurrentUser scope), so only the Windows account that created it can unwrap it.
Databases are sealed with AES-256-GCM; the plaintext header is authenticated as associated data.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import struct
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEY_MAGIC = b"MRSKEY1\x00"
BLOB_MAGIC = b"MRSAI01\x00"
NONCE_BYTES = 12
LABEL_HASH_BYTES = 16
HEADER = struct.Struct(f">8s{LABEL_HASH_BYTES}sQ{NONCE_BYTES}s")
DPAPI_ENTROPY = b"mcp-read-source/ai-context/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CryptoError(ValueError):
    """Sealed data cannot be opened (wrong key, tampering, or not ours). Never contains secrets."""


class CryptoUnavailable(RuntimeError):
    """The platform cannot protect the key (DPAPI is Windows-only)."""


def label_hash(alias: str, label: str) -> bytes:
    return hashlib.sha256(f"{alias}\x00{label}".encode("utf-8")).digest()[:LABEL_HASH_BYTES]


def read_label_hash(path: Path) -> bytes | None:
    """Return the label hash from a sealed file's header, or None if it is not a sealed file."""
    try:
        with path.open("rb") as handle:
            header = handle.read(HEADER.size)
    except OSError:
        return None
    if len(header) < HEADER.size:
        return None
    magic, stored_hash, _generation, _nonce = HEADER.unpack(header)
    return stored_hash if magic == BLOB_MAGIC else None


def seal(key: bytes, plaintext: bytes, label: bytes, generation: int) -> bytes:
    header = HEADER.pack(BLOB_MAGIC, label, generation, secrets.token_bytes(NONCE_BYTES))
    return header + AESGCM(key).encrypt(header[-NONCE_BYTES:], plaintext, header)


def open_sealed(key: bytes, blob: bytes) -> tuple[bytes, bytes]:
    """Return (label hash, plaintext). Raise CryptoError if the blob is not authentic."""
    if len(blob) < HEADER.size:
        raise CryptoError("ai-context file is truncated")
    header = blob[:HEADER.size]
    magic, stored_hash, _generation, nonce = HEADER.unpack(header)
    if magic != BLOB_MAGIC:
        raise CryptoError("ai-context file has an unknown format")
    try:
        return stored_hash, AESGCM(key).decrypt(nonce, blob[HEADER.size:], header)
    except InvalidTag:
        raise CryptoError("ai-context file failed authentication") from None


def load_or_create_key(path: Path) -> tuple[bytes, bool]:
    """Return (key, created). A key that cannot be unwrapped is replaced; the caller must then
    treat every existing sealed file as unreadable."""
    if path.is_file():
        data = path.read_bytes()
        if data.startswith(KEY_MAGIC):
            try:
                key = dpapi_unprotect(data[len(KEY_MAGIC):])
                if len(key) == 32:
                    return key, False
            except OSError:
                pass
    key = secrets.token_bytes(32)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(KEY_MAGIC + dpapi_protect(key))
    os.replace(temporary, path)
    return key, True


# --- Windows DPAPI ----------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(protect: bool, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise CryptoUnavailable("ai-context encryption requires Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    data_in, keep_in = _blob(data)
    entropy, keep_entropy = _blob(DPAPI_ENTROPY)
    data_out = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(data_in), ctypes.c_wchar_p("mcp-read-source"),
                                      ctypes.byref(entropy), None, None, CRYPTPROTECT_UI_FORBIDDEN,
                                      ctypes.byref(data_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None,
                                        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out))
    del keep_in, keep_entropy
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(True, data)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(False, data)
