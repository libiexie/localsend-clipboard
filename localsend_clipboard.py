"""LocalSend → Windows 剪贴板 命令行入口。

子命令（推荐运行方式）：
  python localsend_clipboard.py start            后台启动守护进程（pythonw，无窗口）
  python localsend_clipboard.py stop             停止守护进程
  python localsend_clipboard.py status           查看运行状态与最后一条接收记录
  python localsend_clipboard.py once             只扫描一次（调试/agent 调用）
  python localsend_clipboard.py run              前台运行（调试）
  python localsend_clipboard.py config           打印并确保配置文件存在
  python localsend_clipboard.py install-autostart    安装用户级开机自启（当前用户 Startup 文件夹）
  python localsend_clipboard.py uninstall-autostart  移除开机自启
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge as bridge_mod  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), bridge_mod.APP_DIR_NAME)


def _state_dir(args: argparse.Namespace) -> str:
    return os.path.abspath(args.state_dir or DEFAULT_STATE_DIR)


def _make_bridge(args: argparse.Namespace) -> bridge_mod.Bridge:
    return bridge_mod.Bridge(
        settings_path=getattr(args, "settings", None),
        state_dir=_state_dir(args),
    )


def cmd_run(args: argparse.Namespace) -> int:
    b = _make_bridge(args)
    os.makedirs(b.state_dir, exist_ok=True)
    stop_flag = os.path.join(b.state_dir, "stop.flag")
    if os.path.exists(stop_flag):
        b.clear_stop_flag()
    lock_path = os.path.join(b.state_dir, "daemon.lock")
    lock_file = open(lock_path, "a+b")  # noqa: SIM115 - 保持打开以持锁
    try:
        import msvcrt
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(json.dumps({"error": "already running", "state_dir": b.state_dir}, ensure_ascii=False))
        return 0
    try:
        b.log(f"daemon start pid={os.getpid()} settings={b.settings_path}")
        b.write_status(running=True)
        poll = float(b.config.get("poll_interval", 0.5))
        last_heartbeat = 0.0
        while True:
            if b.is_stop_requested():
                b.log("stop flag detected")
                break
            res = b.scan_once()
            if res.get("error"):
                b.write_status(running=True, extra={"last_error": res["error"]})
            now = time.time()
            if now - last_heartbeat > 3:
                b.write_status(running=True)
                last_heartbeat = now
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        b.clear_stop_flag()
        b.write_status(running=False, extra={"stopped_at": bridge_mod.now_iso()})
        b.log("daemon exit")
        try:
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001
            pass
        lock_file.close()
    return 0


def _is_running(state_dir: str) -> bool:
    lock_path = os.path.join(state_dir, "daemon.lock")
    if not os.path.exists(lock_path):
        return False
    try:
        f = open(lock_path, "a+b")  # noqa: SIM115
    except OSError:
        return False
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True
    finally:
        f.close()


def cmd_start(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    os.makedirs(state_dir, exist_ok=True)
    if _is_running(state_dir):
        print(json.dumps({"started": False, "reason": "already running"}, ensure_ascii=False))
        return 0
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    cmd = [exe, os.path.abspath(__file__), "--state-dir", state_dir, "run"]
    if getattr(args, "settings", None):
        cmd += ["--settings", os.path.abspath(args.settings)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=HERE)
    for _ in range(30):
        time.sleep(0.1)
        if _is_running(state_dir):
            st = bridge_mod.status_summary(state_dir)
            print(json.dumps({"started": True, "pid": st.get("pid")}, ensure_ascii=False))
            return 0
    print(json.dumps({"started": False, "reason": "did not acquire lock in time"}, ensure_ascii=False))
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    if not _is_running(state_dir):
        print(json.dumps({"stopped": True, "reason": "not running"}, ensure_ascii=False))
        return 0
    stop_flag = os.path.join(state_dir, "stop.flag")
    open(stop_flag, "w").close()
    for _ in range(50):
        time.sleep(0.1)
        if not _is_running(state_dir):
            print(json.dumps({"stopped": True}, ensure_ascii=False))
            return 0
    st = bridge_mod.status_summary(state_dir)
    pid = st.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    print(json.dumps({"stopped": True, "forced": True, "pid": pid}, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    st = bridge_mod.status_summary(state_dir)
    st["invoked_at"] = bridge_mod.now_iso()
    print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    b = _make_bridge(args)
    res = b.scan_once()
    res["status"] = bridge_mod.status_summary(b.state_dir)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    b = _make_bridge(args)
    print(json.dumps({"state_dir": b.state_dir, "settings_path": b.settings_path,
                      "config": b.config}, ensure_ascii=False, indent=1))
    return 0


def _startup_shortcut_path() -> str:
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "LocalSendClipboard.lnk")


def cmd_install_autostart(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    script = os.path.abspath(__file__)
    lnk = _startup_shortcut_path()
    arguments = f'"{script}" --state-dir "{state_dir}" run'
    ps = f"""
$W = New-Object -ComObject WScript.Shell
$s = $W.CreateShortcut('{lnk}')
$s.TargetPath = '{exe}'
$s.Arguments = '{arguments}'
$s.WorkingDirectory = '{HERE}'
$s.WindowStyle = 7
$s.Description = 'LocalSend -> Clipboard bridge'
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    ok = os.path.exists(lnk)
    print(json.dumps({"autostart_installed": ok, "shortcut": lnk, "state_dir": state_dir},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 1


def cmd_uninstall_autostart(args: argparse.Namespace) -> int:
    lnk = _startup_shortcut_path()
    removed = False
    if os.path.exists(lnk):
        os.remove(lnk)
        removed = True
    print(json.dumps({"autostart_removed": removed, "shortcut": lnk}, ensure_ascii=False, indent=1))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LocalSend -> Windows 剪贴板桥")
    parser.add_argument("--state-dir", default=None, help="状态目录（默认 %LOCALAPPDATA%\\LocalSendClipboard）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "start", "stop", "status", "once", "config",
                 "install-autostart", "uninstall-autostart"):
        p = sub.add_parser(name)
        p.add_argument("--settings", default=None, help="覆盖 LocalSend settings.json 路径")
        p.set_defaults(func=globals()["cmd_" + name.replace("-", "_")])
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
