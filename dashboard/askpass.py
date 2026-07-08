#!/usr/bin/env python3
"""SSH askpass helper + DPAPI credential store (stdlib ctypes only).

ssh runs this (via askpass.cmd) when SSH_ASKPASS_REQUIRE=force is set:
  - host-key confirmation prompts get "yes"
  - password prompts get the credential for $MAESTRO_SSH_KEY from
    state/ssh-creds.json (DPAPI-encrypted: only this Windows account
    can decrypt it), or $MAESTRO_SSH_PW for a use-once password.

serve.py imports protect()/unprotect()/store()/fetch() to save credentials.
"""
import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS = os.path.join(ROOT, "state", "ssh-creds.json")


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(fn, data):
    blob_in = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                           ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(text):
    return base64.b64encode(_crypt(ctypes.windll.crypt32.CryptProtectData,
                                   text.encode("utf-8"))).decode("ascii")


def unprotect(b64):
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData,
                  base64.b64decode(b64)).decode("utf-8")


def _load():
    try:
        with open(CREDS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def store(key, password):
    doc = _load()
    doc[key] = protect(password)
    with open(CREDS, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def fetch(key):
    enc = _load().get(key)
    try:
        return unprotect(enc) if enc else None
    except OSError:
        return None


def keys():
    return sorted(_load().keys())


def forget(key):
    doc = _load()
    if doc.pop(key, None) is not None:
        with open(CREDS, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        return True
    return False


def main():
    prompt = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if "yes/no" in prompt or "authenticity" in prompt or "fingerprint" in prompt:
        print("yes")
        return 0
    pw = os.environ.get("MAESTRO_SSH_PW") or fetch(os.environ.get("MAESTRO_SSH_KEY", ""))
    if pw is None:
        return 1  # ssh falls back to failing this auth; user relaunches and types it
    print(pw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
