from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class CredentialError(RuntimeError):
    pass


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Хранит пароль внешнего приложения в Windows Credential Manager."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, target: str = "RosaMailCollector/MailRu") -> None:
        self.target = target
        if os.name != "nt":
            raise CredentialError("Хранилище паролей поддерживается только в Windows")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    def write(self, username: str, secret: str) -> None:
        if not secret:
            raise CredentialError("Пустой пароль нельзя сохранить")
        blob = secret.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob)
        credential = _CredentialW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.Comment = "Пароль внешнего приложения Mail.ru для Rosa Mail Collector"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = username
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError(f"Не удалось сохранить пароль, код Windows: {ctypes.get_last_error()}")

    def read(self) -> tuple[str, str] | None:
        pointer = ctypes.POINTER(_CredentialW)()
        ok = self._advapi32.CredReadW(
            self.target,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialError(f"Не удалось прочитать пароль, код Windows: {error}")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return credential.UserName or "", raw.decode("utf-16-le")
        finally:
            self._advapi32.CredFree(pointer)

    def delete(self) -> None:
        ok = self._advapi32.CredDeleteW(self.target, self.CRED_TYPE_GENERIC, 0)
        if not ok:
            error = ctypes.get_last_error()
            if error != 1168:
                raise CredentialError(f"Не удалось удалить пароль, код Windows: {error}")

    def has_secret(self) -> bool:
        return self.read() is not None
