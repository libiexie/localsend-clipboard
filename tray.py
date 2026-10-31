#!/usr/bin/env python3
"""LocalSend 剪贴板接收器 —— 系统托盘 + 气泡通知（看得见的"软件"外壳）。

职责：
- 启动时确保零交互接收器在运行（不运行则拉起）；
- 托盘图标（通知区域），右键菜单：状态 / 打开接收目录 / 打开日志 / 重启接收器 / 退出；
- 后台跟踪接收器日志，每收到文字/图片/文件就弹一个 Windows 气泡通知（"已复制到剪贴板"）；
- 退出菜单会同时优雅停止接收器，避免"关了托盘但端口还占着"。

运行方式：pythonw.exe tray.py（无控制台）。日志：%LOCALAPPDATA%\\LocalSendClipboard\\tray.log
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RECEIVER_CLI = os.path.join(HERE, "clipboard_receiver.py")
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LocalSendClipboard")
RECEIVER_STATE = os.path.join(APP_DIR, "receiver")
RECEIVER_STATUS = os.path.join(RECEIVER_STATE, "status.json")
RECEIVER_LOG = os.path.join(RECEIVER_STATE, "receiver.log")
TRAY_LOG = os.path.join(APP_DIR, "tray.log")
LOCK_PATH = os.path.join(APP_DIR, "tray.lock")
ICON_PATH = os.path.join(APP_DIR, "tray.png")

PY_DIR = os.path.dirname(sys.executable)
PYTHON_EXE = os.path.join(PY_DIR, "python.exe") if os.path.exists(os.path.join(PY_DIR, "python.exe")) else sys.executable
PYTHONW = os.path.join(PY_DIR, "pythonw.exe") if os.path.exists(os.path.join(PY_DIR, "pythonw.exe")) else sys.executable

LOG_RE = re.compile(r"^\[[^\]]+\] (text|image|files): (.*)$")


def log(msg: str) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(TRAY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def pid_alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        k32.CloseHandle(h)


def read_json(path: str) -> dict:
    for _ in range(3):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    return {}


def receiver_status() -> dict:
    return read_json(RECEIVER_STATUS)


def receiver_running() -> bool:
    st = receiver_status()
    pid = st.get("pid")
    return bool(pid and pid_alive(int(pid)) and st.get("running"))


def run_receiver_cli(args: list[str], timeout: float = 20.0) -> str:
    cmd = [PYTHON_EXE, RECEIVER_CLI, "--state-dir", RECEIVER_STATE] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return (p.stdout or "").strip() or (p.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def ensure_receiver() -> bool:
    if receiver_running():
        return True
    log("receiver not running -> starting")
    run_receiver_cli(["start"])
    for _ in range(50):
        time.sleep(0.2)
        if receiver_running():
            log("receiver started")
            return True
    log("receiver failed to start")
    return False


def stop_receiver() -> None:
    if receiver_running():
        log("stopping receiver")
        run_receiver_cli(["stop"])


def restart_receiver() -> bool:
    stop_receiver()
    time.sleep(0.5)
    return ensure_receiver()


def make_icon():
    from PIL import Image, ImageDraw
    size = 64
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([4, 4, 59, 59], radius=14, fill=(20, 110, 190, 255))
    # 剪贴板外形
    d.rounded_rectangle([17, 15, 47, 50], radius=4, fill=(255, 255, 255, 255))
    d.rounded_rectangle([24, 10, 40, 20], radius=3, fill=(255, 255, 255, 255))
    d.rounded_rectangle([22, 24, 42, 27], radius=1, fill=(20, 110, 190, 255))
    d.rounded_rectangle([22, 31, 42, 34], radius=1, fill=(20, 110, 190, 255))
    # 绿色勾
    d.line([(35, 47), (41, 53), (53, 38)], fill=(40, 180, 90, 255), width=5)
    return im


def describe_item(kind: str, payload: dict) -> str:
    if kind == "text":
        preview = str(payload.get("preview") or "")
        chars = payload.get("chars")
        return f"文字（{chars} 字）：{preview}" if preview else f"文字（{chars} 字）"
    if kind == "image":
        name = payload.get("file_name") or os.path.basename(str(payload.get("path") or ""))
        return f"图片：{name}（已复制到剪贴板）"
    names = payload.get("file_names") or [os.path.basename(p) for p in (payload.get("paths") or [])]
    if len(names) == 1:
        return f"文件：{names[0]}（已复制到剪贴板）"
    return f"{len(names)} 个文件：{', '.join(names[:3])}…（已复制到剪贴板）"


class TrayApp:
    def __init__(self):
        self.icon = None
        self.stop_event = threading.Event()
        self.tail_thread = None
        self.lock_file = None

    # ---------------------------------------------------------------- menu
    def on_status(self, icon, item) -> None:
        st = receiver_status()
        last = (st.get("last_items") or [{}])[-1] if st.get("last_items") else {}
        msg = (f"运行中 PID {st.get('pid')}｜端口 {st.get('port')}｜已通告 {st.get('announces')} 次\n"
               f"设备名：{st.get('alias')}\n最近：{last.get('kind') or '无'}")
        icon.notify(msg, "LocalSend 剪贴板 - 状态")

    def on_open_receive_dir(self, icon, item) -> None:
        st = receiver_status()
        path = st.get("receive_dir") or os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
        try:
            os.startfile(path)  # noqa: S606
        except Exception as e:  # noqa: BLE001
            icon.notify(str(e), "无法打开接收目录")

    def on_open_log(self, icon, item) -> None:
        try:
            os.startfile(RECEIVER_LOG)  # noqa: S606
        except Exception:  # noqa: BLE001
            try:
                os.startfile(APP_DIR)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass

    def on_restart(self, icon, item) -> None:
        ok = restart_receiver()
        icon.notify("接收器已重启" if ok else "接收器重启失败", "LocalSend 剪贴板")

    def on_quit(self, icon, item) -> None:
        log("tray quit requested")
        self.stop_event.set()
        stop_receiver()
        icon.stop()

    # ---------------------------------------------------------------- tail
    def tail_log(self) -> None:
        os.makedirs(os.path.dirname(RECEIVER_LOG), exist_ok=True)
        pos = 0
        try:
            if os.path.exists(RECEIVER_LOG):
                pos = os.path.getsize(RECEIVER_LOG)
        except OSError:
            pos = 0
        while not self.stop_event.wait(0.5):
            try:
                if not os.path.exists(RECEIVER_LOG):
                    continue
                size = os.path.getsize(RECEIVER_LOG)
                if size < pos:  # 日志轮转
                    pos = 0
                if size == pos:
                    continue
                with open(RECEIVER_LOG, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    m = LOG_RE.match(line)
                    if not m:
                        continue
                    kind, raw = m.group(1), m.group(2)
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {}
                    text = describe_item(kind, payload)
                    log(f"notify: {kind} {text[:150]}")
                    try:
                        if self.icon:
                            self.icon.notify(text, "LocalSend → 剪贴板")
                    except Exception:  # noqa: BLE001
                        log("notify failed: " + traceback.format_exc()[-300:])
            except Exception:  # noqa: BLE001
                log("tail error: " + traceback.format_exc()[-400:])

    # ---------------------------------------------------------------- run
    def acquire_lock(self) -> bool:
        os.makedirs(APP_DIR, exist_ok=True)
        try:
            self.lock_file = open(LOCK_PATH, "a+b")  # noqa: SIM115
            import msvcrt
            msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def run(self) -> int:
        if not self.acquire_lock():
            log("another tray instance already running")
            return 0
        import pystray
        self.ensure_ok = ensure_receiver()
        image = make_icon()
        try:
            image.save(ICON_PATH)
        except Exception:  # noqa: BLE001
            pass
        menu = pystray.Menu(
            pystray.MenuItem("状态", self.on_status, default=True),
            pystray.MenuItem("打开接收目录", self.on_open_receive_dir),
            pystray.MenuItem("打开接收日志", self.on_open_log),
            pystray.MenuItem("重启接收器", self.on_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（停止接收器）", self.on_quit),
        )
        title = "LocalSend 剪贴板"
        if not self.ensure_ok:
            title += "（启动异常）"
        self.icon = pystray.Icon("localsend-clipboard", image, title, menu)
        self.tail_thread = threading.Thread(target=self.tail_log, name="tail", daemon=True)
        self.tail_thread.start()
        log(f"tray started pid={os.getpid()} receiver_ok={self.ensure_ok}")
        try:
            self.icon.run()
        finally:
            self.stop_event.set()
            log("tray exit")
        return 0


def main() -> int:
    return TrayApp().run()


if __name__ == "__main__":
    sys.exit(main())
