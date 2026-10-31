"""LocalSend → Windows 剪贴板 桥接核心。

原理（基于对 LocalSend 1.18.2 源码/运行时的分析）：
- 收到的文字消息：App 不落盘，但会立刻写入接收历史
  `%APPDATA%\\LocalSend\\settings.json` 的 `flutter.ls_receive_history`：
  每个条目是 JSON 字符串，`isMessage=true` 时 `fileName` 就是文本内容；
- 收到的文件/图片：保存完成后同样写入接收历史，`path` 是落盘路径，
  `fileType` 为 image/video/text/other 等；
- 本模块轮询该文件，检测到新的历史条目后写入 Windows 剪贴板：
  文字 → 文本；图片 → 图片(CF_DIB+PNG，可也带文件列表)；文件 → 文件拖放列表(CF_HDROP)。

配置文件默认在 `%LOCALAPPDATA%\\LocalSendClipboard\\config.json`。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import clipboard_win as cb

APP_DIR_NAME = "LocalSendClipboard"

DEFAULT_CONFIG: dict[str, Any] = {
    "settings_path": r"%APPDATA%\LocalSend\settings.json",
    "state_dir": r"%LOCALAPPDATA%\\" + APP_DIR_NAME,
    "poll_interval": 0.5,
    "ignore_existing_on_start": True,
    "actions": {
        "message": "text",       # 文字消息 -> 纯文本
        "image": "image",        # 图片 -> 图片（同时可选文件列表）
        "file": "files",         # 普通文件 -> 文件拖放列表
        "text_file": "files",    # 以文件形式发送的 .txt -> 文件列表（可改 "text" 直接贴内容）
    },
    "image_also_file": True,
    "file_batch_gap_seconds": 30.0,
    "log_max_bytes": 512_000,
    "history_scan_limit": 200,
}

KIND_MESSAGE = "message"
KIND_IMAGE = "image"
KIND_FILES = "files"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def read_json_with_retry(path: str, retries: int = 4, delay: float = 0.04) -> Optional[dict]:
    last_err: Optional[Exception] = None
    for _ in range(retries):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            return json.loads(raw.decode("utf-8-sig"))
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 - 文件可能正被 App 重写
            last_err = e
            time.sleep(delay)
    if last_err:
        raise last_err
    return None


@dataclass
class Bridge:
    settings_path: Optional[str] = None
    state_dir: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        explicit_settings = self.settings_path
        explicit_state_dir = self.state_dir
        provided = dict(self.config or {})
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(provided)
        if isinstance(provided.get("actions"), dict):
            acts = dict(DEFAULT_CONFIG["actions"])
            acts.update(provided["actions"])
            self.config["actions"] = acts
        if explicit_state_dir:
            self.config["state_dir"] = explicit_state_dir
        self.state_dir = expand(self.config["state_dir"])
        os.makedirs(self.state_dir, exist_ok=True)
        self.config_path = os.path.join(self.state_dir, "config.json")
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.status_path = os.path.join(self.state_dir, "status.json")
        self.log_path = os.path.join(self.state_dir, "bridge.log")
        self._load_config()
        # 显式参数（命令行/测试）优先级最高，不被已存在的 config.json 覆盖
        if explicit_settings:
            self.config["settings_path"] = explicit_settings
        self.settings_path = expand(self.config["settings_path"])
        self.state = self._load_state()
        self.last_file_hash: Optional[str] = None
        self._status: dict[str, Any] = self._load_status()
        self.on_item = None  # 可选回调：每应用一条接收内容后通知宿主

    # ------------------------------------------------------------------ config/state
    def _load_config(self) -> None:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "rb") as f:
                    user_cfg = json.loads(f.read().decode("utf-8-sig"))
                merged = dict(DEFAULT_CONFIG)
                merged.update(user_cfg)
                if isinstance(user_cfg.get("actions"), dict):
                    acts = dict(DEFAULT_CONFIG["actions"])
                    acts.update(user_cfg["actions"])
                    merged["actions"] = acts
                self.config = merged
                return
            except Exception:
                pass
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=1)

    def _load_state(self) -> dict[str, Any]:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "rb") as f:
                    st = json.loads(f.read().decode("utf-8-sig"))
                st.setdefault("seen", [])
                st.setdefault("initialized", False)
                st.setdefault("last_item", None)
                st.setdefault("file_batch", None)
                return st
            except Exception:
                pass
        return {"seen": [], "initialized": False, "last_item": None, "file_batch": None}

    def _load_status(self) -> dict[str, Any]:
        if os.path.exists(self.status_path):
            try:
                with open(self.status_path, "rb") as f:
                    return json.loads(f.read().decode("utf-8-sig"))
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        st = self.state
        seen = st.get("seen", [])
        # 只保留最近 2000 个 id，防止无限增长
        if len(seen) > 2000:
            st["seen"] = seen[-2000:]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def _load_status(self) -> dict[str, Any]:
        if os.path.exists(self.status_path):
            try:
                return json.loads(open(self.status_path, encoding="utf-8").read())
            except Exception:
                pass
        return {}

    def write_status(self, running: bool = False, extra: Optional[dict] = None) -> dict[str, Any]:
        st = {
            "pid": os.getpid(),
            "running": running,
            "settings_path": self.settings_path,
            "state_dir": self.state_dir,
            "updated_at": now_iso(),
            "last_item": self.state.get("last_item"),
            "history_count": (self._status or {}).get("history_count", 0),
            "seen_count": len(self.state.get("seen", [])),
        }
        if extra:
            st.update(extra)
        self._status = st
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.status_path)
        except Exception:
            pass
        return st

    # ------------------------------------------------------------------ logging
    def log(self, msg: str) -> None:
        line = f"[{now_iso()}] {msg}\n"
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > self.config["log_max_bytes"]:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
        if os.environ.get("LOCALSEND_CLIPBOARD_VERBOSE"):
            print(line, end="", file=sys.stderr)

    # ------------------------------------------------------------------ core scan
    def _read_history(self) -> list[dict[str, Any]]:
        data = read_json_with_retry(self.settings_path)
        if not data:
            return []
        raw = data.get("flutter.ls_receive_history")
        if not raw:
            return []
        if isinstance(raw, str):  # 兼容 single string 形式
            try:
                raw = json.loads(raw)
            except Exception:
                return []
        entries: list[dict[str, Any]] = []
        for item in raw[-int(self.config["history_scan_limit"]):]:
            try:
                e = json.loads(item) if isinstance(item, str) else item
                if isinstance(e, dict) and e.get("id"):
                    entries.append(e)
            except Exception:
                continue
        return entries

    def scan_once(self, apply: bool = True) -> dict[str, Any]:
        """扫描一次接收历史；返回本次处理结果。"""
        result: dict[str, Any] = {"new": [], "applied": None, "initialized": False, "error": None}
        try:
            text = None
            try:
                with open(self.settings_path, "rb") as f:
                    raw = f.read()
                text = raw
                digest = hashlib.sha256(raw).hexdigest()
            except FileNotFoundError:
                return result
            if digest == self.last_file_hash:
                return result
            self.last_file_hash = digest
            data = json.loads(raw.decode("utf-8-sig"))
            hist = data.get("flutter.ls_receive_history") or []
            entries = []
            for item in hist:
                try:
                    e = json.loads(item) if isinstance(item, str) else item
                    if isinstance(e, dict) and e.get("id"):
                        entries.append(e)
                except Exception:
                    continue
            self._status["history_count"] = len(entries)
            seen = set(self.state.get("seen", []))
            new = [e for e in entries if e["id"] not in seen]

            if not self.state.get("initialized"):
                # 首次启动：把已有历史记为已见，避免把旧文件重新塞进剪贴板
                self.state["seen"] = list({e["id"] for e in entries})
                self.state["initialized"] = True
                self._save_state()
                result["initialized"] = True
                self.write_status(running=True)
                if self.config.get("ignore_existing_on_start", True):
                    result["new"] = []
                    return result

            if not new:
                return result
            new.sort(key=lambda e: str(e.get("timestamp") or ""))
            result["new"] = [self._summarize(e) for e in new]
            if apply:
                applied = self.apply_entries(new)
                result["applied"] = applied
            for e in new:
                seen.add(e["id"])
            self.state["seen"] = list(seen)
            self._save_state()
            self.write_status(running=True)
            return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
            self.log("scan error: " + traceback.format_exc())
            return result

    @staticmethod
    def _summarize(e: dict[str, Any]) -> dict[str, Any]:
        msg = bool(e.get("isMessage"))
        return {
            "id": e.get("id"),
            "kind": KIND_MESSAGE if msg else e.get("fileType", "other"),
            "sender": e.get("senderAlias"),
            "timestamp": e.get("timestamp"),
            "preview": (e.get("fileName") or "")[:120] if msg else e.get("fileName"),
            "path": e.get("path"),
            "size": e.get("fileSize"),
        }

    # ------------------------------------------------------------------ apply
    def apply_entries(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        messages = [e for e in entries if e.get("isMessage")]
        files = [e for e in entries if not e.get("isMessage") and e.get("path")]
        applied: dict[str, Any] = {"messages": len(messages), "files": len(files), "clipboard": None}

        if messages:
            text = "\n".join(str(e.get("fileName") or "") for e in messages)
            action = self.config["actions"].get("message", "text")
            if action == "text" and text:
                applied["clipboard"] = cb.set_text(text)
                self._record_last(KIND_MESSAGE, message=text, entries=messages,
                                  clipboard=applied["clipboard"])
                self.log(f"clipboard <- message ({len(text)} chars): {text[:60]}")
            # 文字消息意味着新一轮交互，结束文件累积批次，避免把之前的文件混进来
            self.state["file_batch"] = None

        if files:
            self._apply_files(files, applied)
        return applied

    def _apply_files(self, entries: list[dict[str, Any]], applied: dict[str, Any]) -> None:
        now = time.time()
        gap = float(self.config.get("file_batch_gap_seconds", 30.0))
        batch = self.state.get("file_batch") or {}
        sender = str(entries[0].get("senderAlias") or "")
        last_ts = float(batch.get("last_ts") or 0)
        if (not batch.get("paths") or batch.get("sender") != sender
                or gap <= 0 or (now - last_ts > gap)):
            batch = {"sender": sender, "paths": [], "types": {}, "last_ts": now}
        for e in entries:
            p = e.get("path")
            if not p or not os.path.exists(p):
                self.log(f"skip missing file: {p}")
                continue
            if p not in batch["paths"]:
                batch["paths"].append(p)
                batch["types"][p] = e.get("fileType", "other")
        batch["last_ts"] = now
        self.state["file_batch"] = batch
        self._save_state()

        paths = batch["paths"]
        if not paths:
            return
        types = batch["types"]
        image_path = None
        if len(paths) == 1 and types.get(paths[0]) == "image" and self.config["actions"].get("image", "image") == "image":
            image_path = paths[0]
        if image_path:
            applied["clipboard"] = cb.set_image(
                image_path, also_file=bool(self.config.get("image_also_file", True)))
            self._record_last(KIND_IMAGE, paths=paths, entries=entries,
                              clipboard=applied["clipboard"])
            self.log(f"clipboard <- image {image_path}")
        else:
            text_file = len(paths) == 1 and types.get(paths[0]) == "text" and \
                self.config["actions"].get("text_file") == "text"
            if text_file:
                try:
                    content = open(paths[0], "r", encoding="utf-8", errors="replace").read()
                    applied["clipboard"] = cb.set_text(content)
                    self._record_last(KIND_MESSAGE, message=content, entries=entries,
                                      clipboard=applied["clipboard"])
                    self.log(f"clipboard <- text file content {paths[0]}")
                    return
                except Exception as e:  # noqa: BLE001
                    self.log(f"read text file failed: {e}")
            applied["clipboard"] = cb.set_files(paths)
            self._record_last(KIND_FILES, paths=paths, entries=entries,
                              clipboard=applied["clipboard"])
            self.log(f"clipboard <- files {paths}")

    def _record_last(self, kind: str, *, message: Optional[str] = None,
                     paths: Optional[list[str]] = None,
                     entries: Optional[list[dict[str, Any]]] = None,
                     clipboard: Optional[dict] = None) -> None:
        entries = entries or []
        item = {
            "kind": kind,
            "time": now_iso(),
            "sender": entries[0].get("senderAlias") if entries else None,
            "entries": [e.get("id") for e in entries],
            "clipboard_formats": (clipboard or {}).get("formats"),
        }
        if message is not None:
            item["text_preview"] = message[:500]
            item["text_len"] = len(message)
        if paths:
            item["paths"] = paths
            item["file_names"] = [os.path.basename(p) for p in paths]
        self.state["last_item"] = item
        try:
            if self.on_item is not None:
                self.on_item(item)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ daemon help
    def is_stop_requested(self) -> bool:
        return os.path.exists(os.path.join(self.state_dir, "stop.flag"))

    def clear_stop_flag(self) -> None:
        try:
            os.remove(os.path.join(self.state_dir, "stop.flag"))
        except FileNotFoundError:
            pass


def status_summary(state_dir: str) -> dict[str, Any]:
    status_path = os.path.join(state_dir, "status.json")
    if not os.path.exists(status_path):
        return {"running": False, "reason": "no status file", "state_dir": state_dir}
    try:
        st = json.loads(open(status_path, encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return {"running": False, "reason": f"status unreadable: {e}", "state_dir": state_dir}
    pid = st.get("pid")
    alive = False
    if pid:
        try:
            alive = _pid_alive(int(pid))
        except Exception:
            alive = False
    return {"running": alive, "pid": pid, "status": st, "state_dir": state_dir}


def _pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)
