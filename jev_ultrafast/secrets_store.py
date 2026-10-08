"""Encrypted storage for local provider credentials."""

import ctypes
import os
import sys
from base64 import b64decode, b64encode
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# The browser encrypts the pasted key with this per-process public key, so it never travels in clear text
# and only this server process can read it. A restart makes the browser fetch the new public key.
_TRANSPORT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)

KEY_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "openrouter-api-key.dpapi"
_ENTROPY = b"Jev Ultrafast OpenRouter key v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_crypto():
    if sys.platform != "win32":
        raise RuntimeError("A chave salva pela interface exige o armazenamento protegido do Windows.")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob), wintypes.LPVOID,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def _input_blob(value):
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _protect(value):
    crypt32, kernel32 = _windows_crypto()
    data, data_buffer = _input_blob(value)
    entropy, entropy_buffer = _input_blob(_ENTROPY)
    encrypted = _DataBlob()
    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(data), "Jev Ultrafast OpenRouter API key", ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(encrypted),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows could not protect the OpenRouter key.")
        return ctypes.string_at(encrypted.pbData, encrypted.cbData)
    finally:
        if encrypted.pbData:
            kernel32.LocalFree(encrypted.pbData)
        del data_buffer, entropy_buffer


def _unprotect(value):
    crypt32, kernel32 = _windows_crypto()
    data, data_buffer = _input_blob(value)
    entropy, entropy_buffer = _input_blob(_ENTROPY)
    decrypted = _DataBlob()
    description = wintypes.LPWSTR()
    try:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(data), ctypes.byref(description), ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(decrypted),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows could not unlock the saved OpenRouter key.")
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        if decrypted.pbData:
            kernel32.LocalFree(decrypted.pbData)
        if description:
            kernel32.LocalFree(description)
        del data_buffer, entropy_buffer


def transport_public_key():
    """Base64 SPKI DER, importable by Web Crypto as RSA-OAEP / SHA-256."""
    return b64encode(_TRANSPORT_KEY.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()


def decrypt_transport_key(value):
    try:
        return _TRANSPORT_KEY.decrypt(b64decode(value, validate=True), _OAEP).decode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Não foi possível ler a chave enviada. Recarregue a página e tente novamente.") from None


def save_openrouter_key(value):
    if not isinstance(value, str):
        raise ValueError("Informe uma chave OpenRouter válida.")
    key = value.strip()
    if not key or len(key) > 1024 or any(ord(char) < 32 for char in key):
        raise ValueError("A chave OpenRouter está vazia ou tem formato inválido.")
    encrypted = _protect(key.encode("utf-8"))
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = KEY_PATH.with_suffix(KEY_PATH.suffix + ".tmp")
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, KEY_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def load_openrouter_key():
    if not KEY_PATH.exists():
        return None
    try:
        return _unprotect(KEY_PATH.read_bytes())
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("Não foi possível descriptografar a chave OpenRouter salva no Windows.") from error


def openrouter_key_source(environment_key):
    if KEY_PATH.exists():
        return "encrypted"
    return "environment" if environment_key.strip() else "missing"
