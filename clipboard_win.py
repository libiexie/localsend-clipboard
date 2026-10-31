"""Windows 剪贴板读写（纯 ctypes，无第三方依赖；图片处理可选 Pillow）。

支持的写入格式：
- 文本          : CF_UNICODETEXT
- 文件拖放列表  : CF_HDROP（粘贴到资源管理器/微信/QQ 等 = 文件）
- 图片          : CF_DIB（BMP DIB）+ 注册格式 "PNG"（保留 alpha，现代应用优先用 PNG）

版本：1.1  目标：Windows 10/11 x64
"""

from __future__ import annotations

import ctypes
import io
import os
import struct
import time
import traceback
from ctypes import wintypes
from typing import Iterable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

last_error: Optional[str] = None
_heif_registered = False

CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t


class ClipboardError(RuntimeError):
    pass


def _open_clipboard(retries: int = 40, delay: float = 0.05) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def _alloc_global(data: bytes) -> int:
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise ClipboardError("GlobalAlloc failed")
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        raise ClipboardError("GlobalLock failed")
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def _set_data(fmt: int, data: bytes) -> None:
    h = _alloc_global(data)
    if not user32.SetClipboardData(fmt, h):
        err = ctypes.get_last_error()
        kernel32.GlobalFree(h)
        raise ClipboardError(f"SetClipboardData({fmt}) failed, error={err}")


def _droppables(paths: Iterable[str]) -> bytes:
    files = [os.path.abspath(p) for p in paths]
    listing = "".join(p + "\x00" for p in files) + "\x00"
    data = listing.encode("utf-16-le")
    # DROPFILES: DWORD pFiles; POINT pt; BOOL fNC; BOOL fWide  -> 20 bytes
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + data


def _ensure_image_codecs() -> None:
    """按需注册 HEIC/HEIF 解码器（手机照片常见格式）。未安装 pillow-heif 时静默降级。"""
    global _heif_registered
    if _heif_registered:
        return
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass
    _heif_registered = True


def _open_image(path: str):
    _ensure_image_codecs()
    from PIL import Image, ImageOps
    im = Image.open(path)
    try:
        im = ImageOps.exif_transpose(im)  # 尊重手机照片的旋转方向
    except Exception:  # noqa: BLE001
        pass
    return im


def _image_to_dib(path: str) -> bytes:
    with _open_image(path) as im:
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="BMP")
        return buf.getvalue()[14:]  # 去掉 BITMAPFILEHEADER(14B)，剩 BITMAPINFOHEADER+像素


def _image_to_png(path: str) -> bytes:
    with _open_image(path) as im:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


def set_bundle(*, text: Optional[str] = None, files: Optional[Iterable[str]] = None,
               image_path: Optional[str] = None) -> dict:
    """一次性写入多种格式（同一 clipboard 会话），返回实际写入的格式列表。"""
    global last_error
    files = [p for p in (files or []) if p]
    wrote: list[str] = []
    # 先做耗时的图片转换，避免持有剪贴板期间调用 Pillow
    dib_bytes = png_bytes = None
    if image_path and os.path.exists(image_path):
        try:
            dib_bytes = _image_to_dib(image_path)
            png_bytes = _image_to_png(image_path)
        except Exception:  # noqa: BLE001
            last_error = traceback.format_exc()
    if not _open_clipboard():
        raise ClipboardError("无法打开剪贴板（被其它程序占用）")
    try:
        user32.EmptyClipboard()
        if text is not None:
            _set_data(CF_UNICODETEXT, (text + "\x00").encode("utf-16-le"))
            wrote.append("text")
        if files:
            _set_data(CF_HDROP, _droppables(files))
            wrote.append("files")
        if dib_bytes is not None:
            try:
                _set_data(CF_DIB, dib_bytes)
                wrote.append("dib")
                png_fmt = user32.RegisterClipboardFormatW("PNG")
                if png_fmt and png_bytes is not None:
                    _set_data(png_fmt, png_bytes)
                    wrote.append("png")
            except Exception:  # noqa: BLE001
                last_error = traceback.format_exc()
                # 图片写入失败不影响文件列表（若已写入）
                if "files" not in wrote:
                    _set_data(CF_HDROP, _droppables([image_path]))
                    wrote.append("files")
    finally:
        user32.CloseClipboard()
    return {"formats": wrote, "text_len": len(text) if text is not None else 0,
            "files": files, "image_path": image_path}


def set_text(text: str) -> dict:
    return set_bundle(text=text)


def set_files(paths: Iterable[str]) -> dict:
    return set_bundle(files=paths)


def set_image(path: str, also_file: bool = True) -> dict:
    return set_bundle(image_path=path, files=[path] if also_file else None)


# ---------------------------------------------------------------------------
# 读取（用于自测/诊断）
# ---------------------------------------------------------------------------

def _global_bytes(h) -> bytes:
    p = kernel32.GlobalLock(h)
    if not p:
        return b""
    try:
        size = kernel32.GlobalSize(h)
        return ctypes.string_at(p, size)
    finally:
        kernel32.GlobalUnlock(h)


def read_snapshot() -> dict:
    snap: dict = {"text": None, "files": [], "image": None, "formats": []}
    if not _open_clipboard():
        snap["error"] = "open failed"
        return snap
    try:
        for name, fmt in (("text", CF_UNICODETEXT), ("files", CF_HDROP), ("image", CF_DIB)):
            if not user32.IsClipboardFormatAvailable(fmt):
                continue
            snap["formats"].append(name)
            h = user32.GetClipboardData(fmt)
            if not h:
                continue
            if name == "text":
                raw = _global_bytes(h)
                snap["text"] = raw.decode("utf-16-le", errors="replace").split("\x00")[0]
            elif name == "files":
                count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
                out = []
                for i in range(count):
                    n = shell32.DragQueryFileW(h, i, None, 0)
                    buf = ctypes.create_unicode_buffer(n + 1)
                    shell32.DragQueryFileW(h, i, buf, n + 1)
                    out.append(buf.value)
                snap["files"] = out
            elif name == "image":
                raw = _global_bytes(h)
                if len(raw) >= 16:
                    width = struct.unpack_from("<i", raw, 4)[0]
                    height = struct.unpack_from("<i", raw, 8)[0]
                    snap["image"] = {"width": width, "height": abs(height), "dib_bytes": len(raw)}
    finally:
        user32.CloseClipboard()
    return snap


if __name__ == "__main__":
    import json
    print(json.dumps(read_snapshot(), ensure_ascii=False, indent=1))
