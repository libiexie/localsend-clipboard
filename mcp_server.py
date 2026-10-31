#!/usr/bin/env python3
"""LocalSend → 剪贴板 brid 的 MCP（Model Context Protocol）stdio 服务器。

任何支持 MCP 的 agent 客户端都可以接入（Claude Desktop / Cursor / DSH 等）：
  command: <python.exe>
  args:    ["<此文件绝对路径>"]
可选环境变量：
  LOCALSEND_CLIPBOARD_STATE_DIR  自定义状态目录（默认 %LOCALAPPDATA%\\LocalSendClipboard）
  LOCALSEND_CLIPBOARD_SETTINGS   自定义 LocalSend settings.json 路径（默认 %APPDATA%\\LocalSend\\settings.json）

协议：JSON-RPC 2.0 over stdio，按行分隔（同时兼容 Content-Length 帧）。
所有日志写到 stderr，stdout 只输出协议消息。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bridge as bridge_mod  # noqa: E402

CLI = os.path.join(HERE, "localsend_clipboard.py")
RECEIVER_CLI = os.path.join(HERE, "clipboard_receiver.py")
STATE_DIR = os.environ.get(
    "LOCALSEND_CLIPBOARD_STATE_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), bridge_mod.APP_DIR_NAME),
)
RECEIVER_STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                  "LocalSendClipboard", "receiver")
SETTINGS_OVERRIDE = os.environ.get("LOCALSEND_CLIPBOARD_SETTINGS")

SERVER_INFO = {"name": "localsend-clipboard", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _summary() -> dict[str, Any]:
    st = bridge_mod.status_summary(STATE_DIR)
    return {
        "running": st.get("running", False),
        "pid": st.get("pid"),
        "state_dir": STATE_DIR,
        "last_item": (st.get("status") or {}).get("last_item"),
        "history_count": (st.get("status") or {}).get("history_count"),
        "seen_count": (st.get("status") or {}).get("seen_count"),
        "updated_at": (st.get("status") or {}).get("updated_at"),
    }


def _run_cli(args: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, CLI, "--state-dir", STATE_DIR] + args
    if SETTINGS_OVERRIDE:
        cmd += ["--settings", SETTINGS_OVERRIDE]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (p.stdout or "").strip()
        try:
            return {"ok": p.returncode == 0, "result": json.loads(out) if out else None,
                    "stderr": (p.stderr or "")[-800:]}
        except json.JSONDecodeError:
            return {"ok": p.returncode == 0, "stdout": out[-2000:], "stderr": (p.stderr or "")[-800:]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _run_receiver(args: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, RECEIVER_CLI, "--state-dir", RECEIVER_STATE_DIR] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (p.stdout or "").strip()
        try:
            return {"ok": p.returncode == 0, "result": json.loads(out) if out else None,
                    "stderr": (p.stderr or "")[-800:]}
        except json.JSONDecodeError:
            return {"ok": p.returncode == 0, "stdout": out[-2000:], "stderr": (p.stderr or "")[-800:]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


TOOLS = [
    {
        "name": "clipboard_bridge_status",
        "description": "查看 LocalSend→剪贴板 桥接守护进程状态、最后一条接收记录和统计。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_last_received",
        "description": "返回最近一次从手机经 LocalSend 接收并写入剪贴板的内容（文字预览/文件名/类型/时间）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_wait_for_received",
        "description": "等待下一次手机 → 电脑的 LocalSend 接收事件（最长 timeout_seconds 秒），返回该条内容摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {"timeout_seconds": {"type": "number", "minimum": 1, "maximum": 1800, "default": 120}},
            "additionalProperties": False,
        },
    },
    {
        "name": "clipboard_bridge_start",
        "description": "启动桥接守护进程（后台、无窗口）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_bridge_stop",
        "description": "停止桥接守护进程。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_bridge_scan_once",
        "description": "立即扫描一次 LocalSend 接收历史并把新内容写入剪贴板（调试/一次性使用）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_receiver_status",
        "description": "查看「零交互」独立接收器（虚拟设备 电脑剪贴板，端口 53318）的状态、证书指纹和最近接收记录。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_receiver_start",
        "description": "启动零交互接收器（手机设备列表里的 电脑剪贴板 设备）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clipboard_receiver_stop",
        "description": "停止零交互接收器。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def call_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "clipboard_bridge_status":
        return _summary()
    if name == "clipboard_last_received":
        return {"last_item": _summary().get("last_item")}
    if name == "clipboard_bridge_start":
        return _run_cli(["start"])
    if name == "clipboard_bridge_stop":
        return _run_cli(["stop"])
    if name == "clipboard_bridge_scan_once":
        b = bridge_mod.Bridge(settings_path=SETTINGS_OVERRIDE, state_dir=STATE_DIR)
        res = b.scan_once()
        res["status"] = bridge_mod.status_summary(STATE_DIR)
        return res
    if name == "clipboard_receiver_status":
        return _run_receiver(["status"])
    if name == "clipboard_receiver_start":
        return _run_receiver(["start"])
    if name == "clipboard_receiver_stop":
        return _run_receiver(["stop"])
    if name == "clipboard_wait_for_received":
        timeout = float(args.get("timeout_seconds", 120))
        baseline = _summary().get("last_item") or {}
        base_key = (baseline.get("time"), baseline.get("entries"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = _summary().get("last_item") or {}
            key = (item.get("time"), item.get("entries"))
            if item and key != base_key:
                return {"new_item": item}
            time.sleep(0.4)
        return {"new_item": None, "timeout": True, "waited_seconds": timeout}
    raise ValueError(f"unknown tool: {name}")


def handle(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:
        return None
    if msg.get("id") is None and method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            result = {"content": [{"type": "text",
                                   "text": json.dumps(call_tool(name, params.get("arguments") or {}),
                                                      ensure_ascii=False, indent=1)}]}
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # noqa: BLE001
        log(f"tool error: {e}")
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    log(f"localsend-clipboard MCP server started; state_dir={STATE_DIR}")
    buf = sys.stdin.buffer
    while True:
        line = buf.readline()
        if not line:
            break
        raw = line.decode("utf-8", errors="replace").strip()
        if not raw:
            continue
        # 兼容 LSP 风格的 Content-Length 帧
        if raw.lower().startswith("content-length:"):
            try:
                length = int(raw.split(":", 1)[1].strip())
                while True:
                    h = buf.readline()
                    if not h or h in (b"\r\n", b"\n"):
                        break
                body = buf.read(length)
                raw = body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        resp = handle(msg)
        if resp is not None:
            send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
