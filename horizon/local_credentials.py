"""Windows user-bound encrypted credential storage. Never log plaintext."""
import os
import base64
import ctypes
from ctypes import wintypes


def transform(value, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Use environment credentials outside Windows')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    data = base64.b64decode(value, validate=True) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError('Windows credential encryption failed')
    try:
        raw = ctypes.string_at(result.data, result.size)
        return raw.decode('utf-8') if decrypt else base64.b64encode(raw).decode('ascii')
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel.LocalFree(result.data)
        ctypes.memset(buffer, 0, len(data))


def protect(value):
    return transform(value)


def unprotect(value):
    return transform(value, decrypt=True)
