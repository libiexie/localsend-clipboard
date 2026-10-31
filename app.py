#!/usr/bin/env python3
"""LocalSend 剪贴板 —— 独立可运行主程序（打包成单文件 exe 用）。

一个进程内同时包含：
- 零交互接收器（伪装成局域网 LocalSend 设备「电脑剪贴板」，HTTPS 53318 + 组播通告）；
- 可选桥接守护（读取官方 LocalSend 电脑版的接收历史，用于"发给电脑版也能自动进剪贴板"）；
- 系统托盘图标 + 气泡通知 + 菜单（状态/打开目录/日志/复制指纹/重启/开机自启/修复防火墙/退出）。

首次运行（非管理员）会弹一次 UAC，自动为当前 exe 添加 Windows 防火墙入站规则
（TCP 53318），之后手机才能连进来。所有状态/证书/日志都在
%LOCALAPPDATA%\\LocalSendClipboard 下，删除 exe 不会残留。

命令行参数：
  --setup-firewall            仅添加防火墙规则（提权子进程使用）
  --port N                    自定义端口（默认 53318）
  --state-dir PATH            自定义状态目录（测试用）
  --no-firewall               跳过防火墙检查（测试用）
  --no-bridge                 不启动桥接模式
  --selftest-image PATH       把指定图片写入剪贴板并输出结果（打包自检用）
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import winreg
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, HERE)

import clipboard_receiver as cr  # noqa: E402
import clipboard_win as cb  # noqa: E402

try:
    import bridge as bridge_mod  # noqa: E402
except Exception:  # noqa: BLE001
    bridge_mod = None

APP_NAME = "LocalSend 剪贴板"
DEFAULT_PORT = 53318
FW_RULE = "LocalSend Clipboard Receiver"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LocalSendClipboard"

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LocalSendClipboard")
RECEIVER_STATE = os.path.join(APP_DIR, "receiver")
BRIDGE_STATE = APP_DIR
APP_LOG = os.path.join(APP_DIR, "app.log")
TRAY_LOG = os.path.join(APP_DIR, "tray.log")
LOCK_PATH = os.path.join(APP_DIR, "app.lock")
FW_MARKER = os.path.join(APP_DIR, "firewall-setup.json")
LOCALSEND_SETTINGS = os.path.join(os.environ.get("APPDATA", ""), "LocalSend", "settings.json")

RECEIVER_RE = re.compile(r"^\[[^\]]+\] (text|image|files): (.*)$")
BRIDGE_RE = re.compile(r"^\[[^\]]+\] clipboard <- (message|image|files)(.*)$")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(APP_LOG) and os.path.getsize(APP_LOG) > 1_000_000:
            os.replace(APP_LOG, APP_LOG + ".1")
        with open(APP_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def read_json(path: str) -> dict:
    for _ in range(3):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    return {}


def pid_alive(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:  # noqa: BLE001
        return False


def run_capture(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def port_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port: int, timeout: float = 10.0) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# 防火墙（首次运行自动提权添加规则）
# ---------------------------------------------------------------------------

def firewall_marker_ok(port: int) -> bool:
    d = read_json(FW_MARKER)
    try:
        same_exe = os.path.normcase(os.path.abspath(str(d.get("exe") or ""))) == \
            os.path.normcase(os.path.abspath(sys.executable))
        return bool(same_exe and int(d.get("port") or 0) == port and d.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def setup_firewall(port: int) -> bool:
    """需要管理员：为当前 exe 添加入站 TCP 规则，并写标记。"""
    exe = os.path.abspath(sys.executable)
    run_capture(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FW_RULE}"])
    code, out = run_capture([
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={FW_RULE}", "dir=in", "action=allow", "protocol=TCP",
        f"localport={port}", f"program={exe}", "profile=any", "enable=yes",
    ])
    ok = code == 0
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(FW_MARKER, "w", encoding="utf-8") as f:
            json.dump({"exe": exe, "port": port, "ok": ok, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    log(f"setup_firewall port={port} ok={ok} out={out[-200:]}")
    return ok


def ensure_firewall(port: int, skip: bool = False) -> bool:
    if skip:
        return True
    if firewall_marker_ok(port):
        return True
    if is_admin():
        return setup_firewall(port)
    # 非管理员：通过 UAC 启动自己，让提权副本去加规则
    try:
        params = f'--setup-firewall --port {port}'
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, HERE, 0)
        log(f"elevation requested ret={ret}")
        if ret is not None and int(ret) > 32:
            for _ in range(60):
                time.sleep(0.5)
                if firewall_marker_ok(port):
                    return True
        else:
            log("UAC declined or ShellExecute failed")
    except Exception:  # noqa: BLE001
        log("elevation failed: " + traceback.format_exc()[-300:])
    return False


# ---------------------------------------------------------------------------
# 托盘图标 / 通知
# ---------------------------------------------------------------------------

def make_icon():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([4, 4, 59, 59], radius=14, fill=(20, 110, 190, 255))
    d.rounded_rectangle([17, 15, 47, 50], radius=4, fill=(255, 255, 255, 255))
    d.rounded_rectangle([24, 10, 40, 20], radius=3, fill=(255, 255, 255, 255))
    d.rounded_rectangle([22, 24, 42, 27], radius=1, fill=(20, 110, 190, 255))
    d.rounded_rectangle([22, 31, 42, 34], radius=1, fill=(20, 110, 190, 255))
    d.line([(35, 47), (41, 53), (53, 38)], fill=(40, 180, 90, 255), width=5)
    return im


def describe_receiver_item(kind: str, payload: dict) -> str:
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
    return f"{len(names)} 个文件（已复制到剪贴板）"


def describe_bridge_line(kind: str, rest: str) -> str:
    rest = rest.strip()
    if kind == "message":
        return f"文字{rest}（已复制到剪贴板）" if rest else "文字（已复制到剪贴板）"
    if kind == "image":
        path = rest.strip().strip("[]'\"")
        return f"图片：{os.path.basename(path)}（已复制到剪贴板）"
    names = [os.path.basename(p) for p in re.findall(r"'([^']*)'", rest)]
    if len(names) == 1:
        return f"文件：{names[0]}（已复制到剪贴板）"
    return f"{len(names)} 个文件（已复制到剪贴板）"


class ClipboardApp:
    def __init__(self, port: int, no_bridge: bool = False):
        self.requested_port = port
        self.port = port
        self.no_bridge = no_bridge
        self.receiver = None
        self.receiver_thread = None
        self.bridge = None
        self.bridge_thread = None
        self.icon = None
        self.tail_thread = None
        self.stop_event = threading.Event()
        self.lock_file = None
        self.firewall_ok = False

    # ------------------------------------------------------------- receiver
    def _pick_port(self) -> int:
        for p in range(self.requested_port, self.requested_port + 20):
            if port_free(p):
                return p
        return self.requested_port

    def start_receiver(self) -> bool:
        # 端口若已被占用则顺延；端口在加防火墙规则之前就必须确定
        if not port_free(self.port):
            self.port = self._pick_port()
        try:
            self.receiver = cr.Receiver(overrides={"state_dir": RECEIVER_STATE, "port": self.port})
        except Exception:  # noqa: BLE001
            log("receiver init failed: " + traceback.format_exc())
            return False
        # 立刻挂回调（此时 pystray 还没导入；通知先只写 tray.log，避免漏掉早到的消息）
        try:
            self.receiver.on_record = self._on_receiver_record
        except Exception:  # noqa: BLE001
            pass

        def _run():
            try:
                self.receiver.start()
            except Exception:  # noqa: BLE001
                log("receiver crashed: " + traceback.format_exc())

        self.receiver_thread = threading.Thread(target=_run, name="receiver", daemon=True)
        self.receiver_thread.start()
        ok = wait_port(self.port, 12)
        log(f"receiver start port={self.port} ok={ok}")
        return ok

    def stop_receiver(self) -> None:
        try:
            if self.receiver:
                self.receiver.stop()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)

    def restart_receiver(self) -> bool:
        self.stop_receiver()
        return self.start_receiver()

    # ------------------------------------------------------------- bridge
    def start_bridge(self) -> None:
        if self.no_bridge or bridge_mod is None or not os.path.exists(LOCALSEND_SETTINGS):
            return
        try:
            self.bridge = bridge_mod.Bridge(settings_path=LOCALSEND_SETTINGS, state_dir=BRIDGE_STATE)
            self.bridge.on_item = self._on_bridge_item
        except Exception:  # noqa: BLE001
            log("bridge init failed: " + traceback.format_exc())
            return

        def _loop():
            while not self.stop_event.is_set():
                try:
                    self.bridge.scan_once()
                except Exception:  # noqa: BLE001
                    log("bridge scan error: " + traceback.format_exc()[-400:])
                self.stop_event.wait(0.6)

        self.bridge_thread = threading.Thread(target=_loop, name="bridge", daemon=True)
        self.bridge_thread.start()
        log("bridge started")

    # ------------------------------------------------------------- notifications
    def _tail(self, path: str, matcher) -> None:
        pos = 0
        try:
            if os.path.exists(path):
                pos = os.path.getsize(path)
        except OSError:
            pos = 0
        while not self.stop_event.wait(0.5):
            try:
                if not os.path.exists(path):
                    continue
                size = os.path.getsize(path)
                if size < pos:
                    pos = 0
                if size == pos:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    text = matcher(line)
                    if not text:
                        continue
                    self.notify(text)
            except Exception:  # noqa: BLE001
                log("tail error: " + traceback.format_exc()[-300:])

    def notify(self, message: str) -> None:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(TRAY_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] notify: {message[:200]}\n")
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.icon:
                self.icon.notify(message, APP_NAME)
        except Exception:  # noqa: BLE001
            log("notify failed: " + traceback.format_exc()[-200:])

    def _on_receiver_record(self, kind: str, fields: dict) -> None:
        try:
            self.notify(describe_receiver_item(kind, fields))
        except Exception:  # noqa: BLE001
            log("notify receiver failed: " + traceback.format_exc()[-200:])

    def _on_bridge_item(self, item: dict) -> None:
        try:
            kind = str(item.get("kind") or "")
            if kind == "message":
                text = item.get("text_preview") or ""
                msg = f"文字：{text}（已复制到剪贴板）" if text else "文字（已复制到剪贴板）"
            elif kind == "image":
                names = item.get("file_names") or []
                msg = f"图片：{names[0] if names else ''}（已复制到剪贴板）"
            else:
                names = item.get("file_names") or []
                msg = (f"文件：{names[0]}（已复制到剪贴板）" if len(names) == 1
                       else f"{len(names)} 个文件（已复制到剪贴板）")
            self.notify(msg)
        except Exception:  # noqa: BLE001
            log("notify bridge failed: " + traceback.format_exc()[-200:])

    def _match_receiver(self, line: str):
        m = RECEIVER_RE.match(line)
        if not m:
            return None
        kind, raw = m.group(1), m.group(2)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        return describe_receiver_item(kind, payload)

    def _match_bridge(self, line: str):
        m = BRIDGE_RE.match(line)
        if not m:
            return None
        return describe_bridge_line(m.group(1), m.group(2))

    # ------------------------------------------------------------- menu handlers
    def on_status(self, icon, item) -> None:
        st = read_json(os.path.join(RECEIVER_STATE, "status.json"))
        bridge_status = read_json(os.path.join(BRIDGE_STATE, "status.json"))
        last = (st.get("last_items") or [{}])[-1] if st.get("last_items") else {}
        fw = "已放行" if self.firewall_ok else "未放行(手机可能连不上)"
        msg = (f"接收器：PID {st.get('pid')}｜端口 {st.get('port')}｜防火墙 {fw}\n"
               f"设备名：{st.get('alias')}｜已通告 {st.get('announces')} 次\n"
               f"最近：{last.get('kind') or '无'}｜桥接：{'运行中' if bridge_status.get('running') else '未运行'}")
        icon.notify(msg, f"{APP_NAME} - 状态")

    def on_copy_fingerprint(self, icon, item) -> None:
        st = read_json(os.path.join(RECEIVER_STATE, "status.json"))
        fp = st.get("fingerprint") or ""
        if fp:
            cb.set_text(fp)
            icon.notify("设备证书指纹已复制到剪贴板", APP_NAME)
        else:
            icon.notify("指纹还没生成（接收器未启动）", APP_NAME)

    def on_open_receive_dir(self, icon, item) -> None:
        st = read_json(os.path.join(RECEIVER_STATE, "status.json"))
        path = st.get("receive_dir") or os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
        try:
            os.startfile(path)  # noqa: S606
        except Exception as e:  # noqa: BLE001
            icon.notify(str(e), APP_NAME)

    def on_open_logs(self, icon, item) -> None:
        try:
            os.startfile(APP_DIR)  # noqa: S606
        except Exception:  # noqa: BLE001
            pass

    def on_restart(self, icon, item) -> None:
        ok = self.restart_receiver()
        icon.notify("接收器已重启" if ok else "接收器重启失败（看 app.log）", APP_NAME)

    def on_repair_firewall(self, icon, item) -> None:
        try:
            os.remove(FW_MARKER)
        except OSError:
            pass
        ok = ensure_firewall(self.port)
        self.firewall_ok = ok
        icon.notify("防火墙规则已更新" if ok else "防火墙修复失败/被取消", APP_NAME)

    def on_quit(self, icon, item) -> None:
        log("quit requested")
        self.stop_event.set()
        self.stop_receiver()
        icon.stop()

    # ------------------------------------------------------------- autostart
    def autostart_command(self) -> str:
        exe = os.path.abspath(sys.executable)
        return f'"{exe}"'

    def autostart_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                value, _ = winreg.QueryValueEx(k, RUN_VALUE)
                return bool(value)
        except OSError:
            return False

    def toggle_autostart(self, icon, item) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if self.autostart_enabled():
                    winreg.DeleteValue(k, RUN_VALUE)
                    icon.notify("已关闭开机自启", APP_NAME)
                else:
                    winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, self.autostart_command())
                    icon.notify("已开启开机自启（当前用户）", APP_NAME)
        except Exception as e:  # noqa: BLE001
            icon.notify(f"设置失败: {e}", APP_NAME)

    # ------------------------------------------------------------- run
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
            log("another instance is running")
            return 0
        log(f"app start pid={os.getpid()} exe={sys.executable} frozen={getattr(sys, 'frozen', False)}")
        # 先确定端口，再按该端口加防火墙规则，最后启动接收器
        self.port = self._pick_port()
        self.firewall_ok = ensure_firewall(self.port, skip=os.environ.get("LS_CLIPBOARD_NO_FW") == "1")
        if not self.firewall_ok:
            log("WARNING: firewall rule not installed; phone may not connect")
        rec_ok = self.start_receiver()
        self.start_bridge()

        from PIL import Image  # noqa: F401
        import pystray
        image = make_icon()
        if self.receiver is not None:
            self.receiver.on_record = self._on_receiver_record
        if self.bridge is not None:
            self.bridge.on_item = self._on_bridge_item

        menu = pystray.Menu(
            pystray.MenuItem("状态", self.on_status, default=True),
            pystray.MenuItem("复制设备指纹（手机核对用）", self.on_copy_fingerprint),
            pystray.MenuItem("打开接收目录", self.on_open_receive_dir),
            pystray.MenuItem("打开日志目录", self.on_open_logs),
            pystray.MenuItem("重启接收器", self.on_restart),
            pystray.MenuItem("修复防火墙（需管理员）", self.on_repair_firewall),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启（当前用户）", self.toggle_autostart, checked=lambda item: self.autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.on_quit),
        )
        title = f"{APP_NAME} · 端口 {self.port}"
        if not rec_ok:
            title += "（启动异常）"
        if not self.firewall_ok:
            title += "（防火墙未放行）"
        self.icon = pystray.Icon("localsend-clipboard", image, title, menu)
        try:
            self.icon.run()
        finally:
            self.stop_event.set()
            self.stop_receiver()
            log("app exit")
        return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def do_selftest_image(path: str) -> int:
    result = {"ok": False}
    try:
        r = cb.set_image(path, also_file=True)
        snap = cb.read_snapshot()
        result = {"ok": True, "formats": r.get("formats"), "image": snap.get("image"),
                  "files": snap.get("files"), "last_error": getattr(cb, "last_error", None)}
    except Exception:  # noqa: BLE001
        result = {"ok": False, "error": traceback.format_exc()}
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "selftest.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    log("selftest-image: " + json.dumps(result, ensure_ascii=False)[:500])
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    global APP_DIR, RECEIVER_STATE, BRIDGE_STATE, APP_LOG, TRAY_LOG, LOCK_PATH, FW_MARKER, LOCALSEND_SETTINGS
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--setup-firewall", action="store_true")
    p.add_argument("--port", type=int, default=int(os.environ.get("LS_CLIPBOARD_PORT", DEFAULT_PORT)))
    p.add_argument("--state-dir", default=None)
    p.add_argument("--no-firewall", action="store_true")
    p.add_argument("--no-bridge", action="store_true")
    p.add_argument("--selftest-image", default=None)
    args = p.parse_args(argv)

    if args.state_dir:
        APP_DIR = os.path.abspath(args.state_dir)
        RECEIVER_STATE = os.path.join(APP_DIR, "receiver")
        BRIDGE_STATE = APP_DIR
        APP_LOG = os.path.join(APP_DIR, "app.log")
        TRAY_LOG = os.path.join(APP_DIR, "tray.log")
        LOCK_PATH = os.path.join(APP_DIR, "app.lock")
        FW_MARKER = os.path.join(APP_DIR, "firewall-setup.json")
        os.makedirs(APP_DIR, exist_ok=True)

    if args.setup_firewall:
        return 0 if setup_firewall(args.port) else 1
    if args.selftest_image:
        return do_selftest_image(args.selftest_image)
    if args.no_firewall:
        os.environ["LS_CLIPBOARD_NO_FW"] = "1"
    log(f"launch args: {sys.argv[1:]}")
    try:
        return ClipboardApp(port=args.port, no_bridge=args.no_bridge).run()
    except Exception:  # noqa: BLE001
        log("fatal: " + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
