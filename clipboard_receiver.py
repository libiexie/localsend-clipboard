#!/usr/bin/env python3
"""LocalSend 兼容的「剪贴板接收器」虚拟设备（完全零交互）。

它把自己伪装成局域网里的一个 LocalSend 设备（HTTPS + 自签证书 + 组播通告）：
手机 LocalSend 的发送页会多出一个设备（默认叫「电脑剪贴板」）。第一次发送时
手机可能让你确认一次证书指纹（TOFU，之后不再询问），随后：
- 文字消息 -> 直接写剪贴板（手机端连"接受"都不用点）
- 图片     -> 写剪贴板图片（同时带文件列表）
- 文件     -> 保存到接收目录，剪贴板变为文件拖放列表
不依赖电脑端 LocalSend 是否运行，也不需要任何弹窗确认。

协议依据：LocalSend v2.2（本机 1.18.2 源码 + 运行时逆向结果）：
  POST /api/localsend/v2/register          -> 返回本设备信息
  POST /api/localsend/v2/prepare-upload    -> 接受全部（文字消息返回 204）
  POST /api/localsend/v2/upload?sessionId&fileId&token -> 收文件字节
  POST /api/localsend/v2/cancel            -> 取消
  GET  /api/localsend/v2/info              -> 设备信息
  UDP 224.0.0.167:53317 通告（announce-only）
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import clipboard_win as cb  # noqa: E402

PROTOCOL_VERSION = "2.2"
DEFAULT_STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                "LocalSendClipboard", "receiver")

DEFAULT_CONFIG: dict[str, Any] = {
    "alias": "电脑剪贴板",
    "device_model": "Windows",
    "port": 53318,
    "receive_dir": r"%USERPROFILE%\Downloads",
    "state_dir": DEFAULT_STATE_DIR,
    "multicast_group": "224.0.0.167",
    "multicast_port": 53317,
    "announce_interval": 5.0,
    "announce_burst": True,
    "verify_checksums": True,
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def find_openssl() -> Optional[str]:
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "usr", "bin", "openssl.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "usr", "bin", "openssl.exe"),
        shutil.which("openssl"),
        shutil.which("openssl.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, fallback: str = "received.bin") -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = _ILLEGAL.sub("_", name).strip().strip(".")
    if not name:
        name = fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) > 120:
        stem = stem[:120]
    return f"{stem}.{ext}" if ext else stem


def unique_path(directory: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return candidate


def local_ipv4_addresses() -> list[str]:
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return [ip for ip in sorted(ips) if not ip.startswith("127.") and not ip.startswith("169.254.")]


def reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 接收器
# ---------------------------------------------------------------------------

class Receiver:
    def __init__(self, config: Optional[dict] = None, overrides: Optional[dict] = None):
        cfg = dict(DEFAULT_CONFIG)
        if config:
            cfg.update(config)
        if overrides:
            cfg.update({k: v for k, v in overrides.items() if v is not None})
        self.config = cfg
        self.state_dir = expand(str(cfg["state_dir"]))
        ensure_dir(self.state_dir)
        self.config_path = os.path.join(self.state_dir, "receiver.json")
        self.status_path = os.path.join(self.state_dir, "status.json")
        self.log_path = os.path.join(self.state_dir, "receiver.log")
        self.stop_flag = os.path.join(self.state_dir, "stop.flag")
        self.receive_dir = expand(str(cfg["receive_dir"]))
        ensure_dir(self.receive_dir)
        self._load_config_file()
        self.port = int(self.config.get("port", 53318))
        self.cert_path = os.path.join(self.state_dir, "cert.pem")
        self.key_path = os.path.join(self.state_dir, "key.pem")
        self.fingerprint = ""
        self._ensure_cert()
        self.lock = threading.Lock()
        self.session: Optional[dict[str, Any]] = None
        self.announces = 0
        self.received = 0
        self.on_record = None  # 可选回调：收到一条内容后通知宿主（托盘气泡）
        self.last_error: Optional[str] = None
        self.last_items: list[dict[str, Any]] = []
        self.started_at = now_iso()
        self._server: Optional[ThreadingHTTPServer] = None
        self._announce_thread: Optional[threading.Thread] = None
        self._clip_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._clip_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------- config
    def _load_config_file(self) -> None:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "rb") as f:
                    user = json.loads(f.read().decode("utf-8-sig"))
                for k, v in user.items():
                    if k in self.config:
                        self.config[k] = v
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, ensure_ascii=False, indent=1)
            except Exception:  # noqa: BLE001
                pass
        self.alias = str(self.config.get("alias") or "电脑剪贴板")
        self.device_model = str(self.config.get("device_model") or "Windows")

    # ------------------------------------------------------------- cert
    def _ensure_cert(self) -> None:
        if os.path.exists(self.cert_path) and os.path.exists(self.key_path):
            self.fingerprint = self._fingerprint_from_pem(self.cert_path)
            return
        # 独立 exe 目标：不依赖系统 openssl，优先用内置 cryptography 生成 RSA-2048 自签证书
        try:
            self._generate_cert_cryptography()
            self.fingerprint = self._fingerprint_from_pem(self.cert_path)
            return
        except Exception as e_crypto:  # noqa: BLE001
            self.log(f"cryptography cert generation failed ({e_crypto}); trying openssl")
        openssl = find_openssl()
        if not openssl:
            raise RuntimeError("无法生成自签证书（cryptography 与 openssl 均不可用）")
        cmd = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", self.key_path,
               "-out", self.cert_path, "-days", "3650", "-nodes", "-subj", "/CN=LocalSend User"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(self.cert_path):
            raise RuntimeError(f"openssl 生成证书失败: {res.stderr[-500:]}")
        self.fingerprint = self._fingerprint_from_pem(self.cert_path)

    def _generate_cert_cryptography(self) -> None:
        """用 cryptography 生成与官方语义一致的自签证书（CN=LocalSend User，RSA-2048，长期有效）。"""
        import datetime as _dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LocalSend User")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc))
            .not_valid_after(_dt.datetime(2099, 12, 31, tzinfo=_dt.timezone.utc))
            .sign(key, hashes.SHA256())
        )
        with open(self.key_path, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        with open(self.cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    @staticmethod
    def _fingerprint_from_pem(path: str) -> str:
        with open(path, encoding="ascii", errors="replace") as f:
            pem = f.read()
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest().upper()

    # ------------------------------------------------------------- logging/status
    def log(self, msg: str) -> None:
        line = f"[{now_iso()}] {msg}\n"
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 1_000_000:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001
            pass
        if os.environ.get("LOCALSEND_RECEIVER_VERBOSE"):
            print(line, end="", file=sys.stderr)

    def write_status(self, running: bool) -> None:
        st = {
            "pid": os.getpid(),
            "running": running,
            "alias": self.alias,
            "port": self.port,
            "fingerprint": self.fingerprint,
            "receive_dir": self.receive_dir,
            "state_dir": self.state_dir,
            "started_at": self.started_at,
            "updated_at": now_iso(),
            "announces": self.announces,
            "received": self.received,
            "session_active": self.session is not None,
            "last_items": self.last_items[-5:],
            "last_error": self.last_error,
        }
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.status_path)
        except Exception:  # noqa: BLE001
            pass

    def _record(self, kind: str, **fields: Any) -> None:
        item = {"kind": kind, "time": now_iso(), **fields}
        self.last_items.append(item)
        self.last_items = self.last_items[-20:]
        self.received += 1
        self.log(f"{kind}: {json.dumps(fields, ensure_ascii=False)[:400]}")
        try:
            if self.on_record is not None:
                self.on_record(kind, fields)
        except Exception:  # noqa: BLE001
            self.log("on_record callback failed: " + traceback.format_exc()[-300:])

    # ------------------------------------------------------------- clipboard
    # 所有剪贴板写入都走同一个专用线程（串行化 + 重试），避免多请求线程同时操作剪贴板
    def copy_text(self, text: str) -> None:
        self._clip_q.put(("text", text))

    def copy_image(self, path: str) -> None:
        self._clip_q.put(("image", path))

    def copy_files(self, paths: list[str]) -> None:
        self._clip_q.put(("files", list(paths)))

    def _clipboard_loop(self) -> None:
        while True:
            try:
                kind, payload = self._clip_q.get(timeout=0.2)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                self._apply_clipboard(kind, payload)
            except Exception:  # noqa: BLE001
                self.last_error = traceback.format_exc()[-500:]
                self.log("clipboard apply failed: " + traceback.format_exc())
            finally:
                self._clip_q.task_done()

    def _apply_clipboard(self, kind: str, payload: Any) -> None:
        res: dict[str, Any] = {}
        for attempt in range(6):
            try:
                if kind == "text":
                    res = cb.set_text(payload)
                    ok = "text" in (res.get("formats") or [])
                elif kind == "image":
                    res = cb.set_image(payload, also_file=True)
                    ok = bool({"dib", "png"} & set(res.get("formats") or []))
                    if not ok and attempt == 0:
                        self.log("image clipboard degraded to file list: " + str(getattr(cb, "last_error", None))[:500])
                else:
                    res = cb.set_files(payload)
                    ok = "files" in (res.get("formats") or [])
                if ok:
                    break
            except Exception:  # noqa: BLE001
                self.last_error = traceback.format_exc()[-500:]
            time.sleep(0.1)
        if kind == "text":
            self._record("text", chars=len(payload), preview=str(payload)[:80],
                         formats=res.get("formats"))
        elif kind == "image":
            self._record("image", path=payload, file_name=os.path.basename(payload),
                         formats=res.get("formats"))
        else:
            self._record("files", paths=list(payload),
                         file_names=[os.path.basename(p) for p in payload],
                         formats=res.get("formats"))
        self.write_status(True)

    # ------------------------------------------------------------- announce
    def announce_payload(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "version": PROTOCOL_VERSION,
            "deviceModel": self.device_model,
            "deviceType": "desktop",
            "fingerprint": self.fingerprint,
            "port": self.port,
            "protocol": "https",
            "download": False,
            "announce": True,
        }

    def send_announce(self) -> None:
        payload = json.dumps(self.announce_payload(), ensure_ascii=False).encode("utf-8")
        group = str(self.config.get("multicast_group", "224.0.0.167"))
        port = int(self.config.get("multicast_port", 53317))
        for ip in local_ipv4_addresses():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                    s.sendto(payload, (group, port))
                    self.announces += 1
                finally:
                    s.close()
            except OSError as e:
                self.last_error = f"announce {ip}: {e}"
        if self.announces % 10 == 1:
            self.write_status(True)

    def _announce_loop(self) -> None:
        self.log("announce loop started")
        last_status = 0.0
        try:
            if bool(self.config.get("announce_burst", True)):
                for delay in (0.1, 0.5, 2.0):
                    if self._stop_event.wait(delay):
                        self.log("announce loop stopped (burst)")
                        return
                    self.send_announce()
                    last_status = time.time()
            interval = float(self.config.get("announce_interval", 2.0))
            if interval <= 0:
                self.log("announce interval <= 0, loop exits")
                return
            while not self._stop_event.wait(interval):
                self.send_announce()
                now = time.time()
                if now - last_status >= 5.0:
                    self.write_status(True)
                    last_status = now
            self.log("announce loop stopped")
        except Exception:  # noqa: BLE001
            self.last_error = "announce loop: " + traceback.format_exc()[-500:]
            self.log("announce loop crashed: " + traceback.format_exc())

    # ------------------------------------------------------------- session helpers
    def _text_message(self, files: dict[str, Any]) -> Optional[str]:
        """官方约定：单个 text/* 文件且带 preview = 文字消息。"""
        if len(files) != 1:
            return None
        dto = next(iter(files.values()))
        ftype = str(dto.get("fileType") or "")
        preview = dto.get("preview")
        if ftype.startswith("text/") and preview is not None:
            return str(preview)
        return None

    def complete_session_if_done(self) -> None:
        with self.lock:
            session = self.session
            if not session:
                return
            if session["pending"]:
                return
            files = list(session["saved"])
            self.session = None
        if not files:
            return
        image_path = None
        if len(files) == 1 and files[0]["file_type"].startswith("image/"):
            image_path = files[0]["path"]
        if image_path:
            self.copy_image(image_path)
        else:
            self.copy_files([f["path"] for f in files])

    def _stop_watcher(self) -> None:
        while not self._stop_event.is_set():
            if os.path.exists(self.stop_flag):
                self._stop_event.set()
                try:
                    if self._server:
                        self._server.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                return
            time.sleep(0.3)

    # ------------------------------------------------------------- server lifecycle
    def start(self) -> None:
        # 清理历史 stop 标记：CLI `stop` 会写 stop.flag，若残留会导致本次启动立即自停
        try:
            if os.path.exists(self.stop_flag):
                os.remove(self.stop_flag)
        except OSError:
            pass
        self._stop_event.clear()
        self.log(f"receiver start pid={os.getpid()} port={self.port} alias={self.alias} fp={self.fingerprint}")
        reconfigure_stdout()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:
            pass
        server = ThreadingHTTPServer(("0.0.0.0", self.port), make_handler(self))
        server.daemon_threads = True
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.write_status(True)
        threading.Thread(target=self._stop_watcher, name="stop-watcher", daemon=True).start()
        self._clip_thread = threading.Thread(target=self._clipboard_loop, name="clipboard", daemon=True)
        self._clip_thread.start()
        self._announce_thread = threading.Thread(target=self._announce_loop, name="announce", daemon=True)
        self._announce_thread.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            self._stop_event.set()
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.write_status(False)
            self.log("receiver exit")

    def stop(self) -> None:
        self._stop_event.set()
        if self._server:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

def make_handler(receiver: Receiver):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LocalSend/2.2 (clipboard-receiver)"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            receiver.log(f"http {self.client_address[0]}: " + fmt % args)

        # ---------- responses ----------
        def _send_json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _send_empty(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()

        # ---------- request body ----------
        def _read_body_bytes(self) -> bytes:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                buf = bytearray()
                while True:
                    line = self.rfile.readline(65536)
                    if not line:
                        break
                    size_str = line.split(b";", 1)[0].strip()
                    try:
                        size = int(size_str, 16)
                    except ValueError:
                        break
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65536)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    buf.extend(self.rfile.read(size))
                    self.rfile.read(2)
                return bytes(buf)
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _read_body_to_file(self, dst_path: str, hasher) -> int:
            total = 0
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            with open(dst_path, "wb") as out:
                if "chunked" in te:
                    while True:
                        line = self.rfile.readline(65536)
                        if not line:
                            break
                        size_str = line.split(b";", 1)[0].strip()
                        if not size_str:
                            continue
                        try:
                            size = int(size_str, 16)
                        except ValueError:
                            break
                        if size == 0:
                            while True:
                                trailer = self.rfile.readline(65536)
                                if trailer in (b"\r\n", b"\n", b""):
                                    break
                            break
                        remaining = size
                        while remaining > 0:
                            chunk = self.rfile.read(min(remaining, 262144))
                            if not chunk:
                                break
                            out.write(chunk)
                            if hasher:
                                hasher.update(chunk)
                            total += len(chunk)
                            remaining -= len(chunk)
                        self.rfile.read(2)  # CRLF
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 262144))
                        if not chunk:
                            break
                        out.write(chunk)
                        if hasher:
                            hasher.update(chunk)
                        total += len(chunk)
                        remaining -= len(chunk)
            return total

        # ---------- info ----------
        def _info(self) -> dict[str, Any]:
            return {
                "alias": receiver.alias,
                "version": PROTOCOL_VERSION,
                "deviceModel": receiver.device_model,
                "deviceType": "desktop",
                "fingerprint": receiver.fingerprint,
                "download": False,
            }

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path in ("/api/localsend/v1/info", "/api/localsend/v2/info"):
                self._send_json(200, self._info())
            elif path == "/":
                body = b"LocalSend clipboard receiver\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"message": "Not found"})

        # ---------- api ----------
        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            path = url.path
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if path == "/api/localsend/v2/register":
                    self._read_body_bytes()
                    self._send_json(200, self._info())
                elif path == "/api/localsend/v2/prepare-upload":
                    self._handle_prepare_upload()
                elif path == "/api/localsend/v2/upload":
                    self._handle_upload(query)
                elif path == "/api/localsend/v2/cancel":
                    self._read_body_bytes()
                    with receiver.lock:
                        receiver.session = None
                    receiver.log("session cancelled by sender")
                    self._send_empty(200)
                else:
                    self._send_json(404, {"message": "Not found"})
            except Exception as e:  # noqa: BLE001
                receiver.last_error = f"{type(e).__name__}: {e}"
                receiver.log("request error: " + traceback.format_exc())
                try:
                    self._send_json(500, {"message": "Internal error"})
                except Exception:  # noqa: BLE001
                    pass

        def _handle_prepare_upload(self) -> None:
            raw = self._read_body_bytes()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"message": "Invalid JSON body"})
                return
            files = payload.get("files") or {}
            info = payload.get("info") or {}
            if not isinstance(files, dict) or not files:
                self._send_json(400, {"message": "No files provided"})
                return
            sender = str(info.get("alias") or "")
            text = receiver._text_message(files)
            if text is not None:
                # 文字消息：官方语义是"内容已在 preview 里，无需传文件"
                receiver.log(f"text message from {sender} ({len(text)} chars)")
                receiver.copy_text(text)
                self._send_empty(204)
                return
            with receiver.lock:
                if receiver.session is not None:
                    self._send_json(409, {"message": "Blocked by another session"})
                    return
                session_id = str(uuid.uuid4())
                tokens: dict[str, str] = {}
                pending = set()
                store: dict[str, Any] = {}
                for fid, dto in files.items():
                    if not isinstance(dto, dict):
                        continue
                    token = str(uuid.uuid4())
                    tokens[fid] = token
                    pending.add(fid)
                    store[fid] = {
                        "dto": dto,
                        "token": token,
                        "file_type": str(dto.get("fileType") or "application/octet-stream"),
                        "attempts": 0,
                    }
                receiver.session = {
                    "session_id": session_id,
                    "sender_alias": sender,
                    "pending": pending,
                    "files": store,
                    "saved": [],
                    "created": now_iso(),
                }
            receiver.log(f"prepare-upload from {sender}: {len(store)} file(s), session {session_id}")
            self._send_json(200, {"sessionId": session_id, "files": tokens})

        def _handle_upload(self, query: dict[str, str]) -> None:
            session_id = query.get("sessionId")
            file_id = query.get("fileId")
            token = query.get("token")
            if not (session_id and file_id and token):
                self._send_json(400, {"message": "Missing parameters"})
                return
            with receiver.lock:
                session = receiver.session
                if not session or session["session_id"] != session_id or file_id not in session["files"]:
                    self._send_json(403, {"message": "Invalid token or IP address"})
                    return
                entry = session["files"][file_id]
                if entry["token"] != token or file_id not in session["pending"]:
                    self._send_json(403, {"message": "Invalid token or IP address"})
                    return
                entry["attempts"] += 1
            dto = entry["dto"]
            name = sanitize_filename(str(dto.get("fileName") or "received.bin"))
            dst = unique_path(receiver.receive_dir, name)
            hasher = hashlib.sha256() if receiver.config.get("verify_checksums", True) else None
            received = 0
            try:
                received = self._read_body_to_file(dst, hasher)
            except Exception as e:  # noqa: BLE001
                receiver.log(f"upload read failed: {e}")
                try:
                    os.remove(dst)
                except OSError:
                    pass
                self._send_json(500, {"message": "Upload failed"})
                return
            expected_sha = (dto.get("sha256") or "").lower()
            actual_sha = hasher.hexdigest() if hasher else ""
            if expected_sha and actual_sha and expected_sha != actual_sha:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                receiver.log(f"checksum mismatch for {name}")
                self._send_json(422, {"message": "Checksum mismatch"})
                return
            with receiver.lock:
                session = receiver.session
                if not session or session["session_id"] != session_id:
                    self._send_json(403, {"message": "Invalid token or IP address"})
                    return
                session["pending"].discard(file_id)
                session["saved"].append({
                    "file_id": file_id,
                    "path": dst,
                    "size": received,
                    "file_type": entry["file_type"],
                    "file_name": name,
                })
            receiver.log(f"uploaded {name} ({received} bytes)")
            self._send_empty(200)
            receiver.complete_session_if_done()

    return Handler


# ---------------------------------------------------------------------------
# CLI / 守护进程
# ---------------------------------------------------------------------------

_PY_DIR = os.path.dirname(sys.executable)
# 注意：Windows 防火墙规则按"程序路径"精确匹配。用户已放行的是 python.exe，
# 因此守护进程必须用 python.exe 启动（隐藏控制台），而不是 pythonw.exe。
PYTHON_EXE = os.path.join(_PY_DIR, "python.exe")
if not os.path.exists(PYTHON_EXE):
    PYTHON_EXE = sys.executable
PYTHONW = os.path.join(_PY_DIR, "pythonw.exe") if os.path.exists(os.path.join(_PY_DIR, "pythonw.exe")) else sys.executable


def _pid_alive(pid: int) -> bool:
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


def is_running(state_dir: str) -> bool:
    lock_path = os.path.join(state_dir, "receiver.lock")
    if not os.path.exists(lock_path):
        return False
    try:
        f = open(lock_path, "a+b")
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


def status_summary(state_dir: str) -> dict[str, Any]:
    p = os.path.join(state_dir, "status.json")
    if not os.path.exists(p):
        return {"running": is_running(state_dir), "reason": "no status file", "state_dir": state_dir}
    try:
        with open(p, "rb") as f:
            st = json.loads(f.read().decode("utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        return {"running": is_running(state_dir), "reason": f"status unreadable: {e}", "state_dir": state_dir}
    return {"running": is_running(state_dir), "status": st, "state_dir": state_dir}


def cmd_run(args: Any) -> int:
    overrides = {
        "alias": getattr(args, "alias", None),
        "port": getattr(args, "port", None),
        "receive_dir": getattr(args, "receive_dir", None),
        "state_dir": getattr(args, "state_dir", None),
        "announce_interval": getattr(args, "announce_interval", None),
    }
    rec = Receiver(overrides=overrides)
    ensure_dir(rec.state_dir)
    if os.path.exists(rec.stop_flag):
        os.remove(rec.stop_flag)
    lock_path = os.path.join(rec.state_dir, "receiver.lock")
    lock_file = open(lock_path, "a+b")  # noqa: SIM115
    try:
        import msvcrt
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(json.dumps({"error": "already running"}, ensure_ascii=False))
        return 0
    try:
        rec.start()
    except KeyboardInterrupt:
        rec.stop()
    finally:
        try:
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001
            pass
        lock_file.close()
    return 0


def _spawn(args: list[str]) -> int:
    # python.exe（而非 pythonw.exe）以匹配已放行的防火墙规则；CREATE_NO_WINDOW 保证无黑窗
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([PYTHON_EXE] + args, creationflags=flags, close_fds=True, cwd=HERE)
    return 0


def _arg(args: Any, name: str):
    return getattr(args, name, None)


def cmd_start(args: Any) -> int:
    state_dir = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    ensure_dir(state_dir)
    if is_running(state_dir):
        print(json.dumps({"started": False, "reason": "already running"}, ensure_ascii=False))
        return 0
    cmd = [os.path.abspath(__file__), "run", "--state-dir", state_dir]
    for flag, val in (("--port", _arg(args, "port")), ("--alias", _arg(args, "alias")),
                      ("--receive-dir", _arg(args, "receive_dir")),
                      ("--announce-interval", _arg(args, "announce_interval"))):
        if val is not None:
            cmd += [flag, str(val)]
    _spawn(cmd)
    for _ in range(40):
        time.sleep(0.1)
        if is_running(state_dir):
            print(json.dumps({"started": True, "state_dir": state_dir}, ensure_ascii=False))
            return 0
    print(json.dumps({"started": False, "reason": "did not start in time"}, ensure_ascii=False))
    return 1


def cmd_stop(args: Any) -> int:
    state_dir = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    if not is_running(state_dir):
        print(json.dumps({"stopped": True, "reason": "not running"}, ensure_ascii=False))
        return 0
    open(os.path.join(state_dir, "stop.flag"), "w").close()
    for _ in range(50):
        time.sleep(0.1)
        if not is_running(state_dir):
            print(json.dumps({"stopped": True}, ensure_ascii=False))
            return 0
    print(json.dumps({"stopped": False, "reason": "timeout; try taskkill"}, ensure_ascii=False))
    return 1


def cmd_status(args: Any) -> int:
    state_dir = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    print(json.dumps(status_summary(state_dir), ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_fingerprint(args: Any) -> int:
    state_dir = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    rec = Receiver(overrides={"state_dir": state_dir})
    print(json.dumps({"fingerprint": rec.fingerprint, "cert": rec.cert_path,
                      "port": rec.port, "alias": rec.alias}, ensure_ascii=False, indent=1))
    return 0


def cmd_announce_once(args: Any) -> int:
    st = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    rec = Receiver(overrides={"state_dir": st, "port": _arg(args, "port"), "alias": _arg(args, "alias")})
    rec.send_announce()
    print(json.dumps({"sent": rec.announces, "payload": rec.announce_payload(),
                      "interfaces": local_ipv4_addresses()}, ensure_ascii=False, indent=1))
    return 0


def _startup_lnk() -> str:
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "LocalSendClipboardReceiver.lnk")


def cmd_install_autostart(args: Any) -> int:
    state_dir = os.path.abspath(_arg(args, "state_dir") or DEFAULT_STATE_DIR)
    script = os.path.abspath(__file__)
    lnk = _startup_lnk()
    arguments = f'"{script}" start --state-dir "{state_dir}"'
    ps = f"""
$W = New-Object -ComObject WScript.Shell
$s = $W.CreateShortcut('{lnk}')
$s.TargetPath = '{PYTHONW}'
$s.Arguments = '{arguments}'
$s.WorkingDirectory = '{HERE}'
$s.WindowStyle = 7
$s.Description = 'LocalSend clipboard receiver (zero interaction)'
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    ok = os.path.exists(lnk)
    print(json.dumps({"autostart_installed": ok, "shortcut": lnk}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def cmd_uninstall_autostart(args: Any) -> int:
    lnk = _startup_lnk()
    removed = False
    if os.path.exists(lnk):
        os.remove(lnk)
        removed = True
    print(json.dumps({"autostart_removed": removed}, ensure_ascii=False))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="LocalSend 兼容的剪贴板接收器（零交互）")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--alias", default=None)
    p.add_argument("--receive-dir", default=None)
    p.add_argument("--announce-interval", type=float, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "start", "stop", "status", "fingerprint", "announce-once",
                 "install-autostart", "uninstall-autostart"):
        sp = sub.add_parser(name)
        sp.add_argument("--state-dir", default=argparse.SUPPRESS)
        sp.add_argument("--port", type=int, default=argparse.SUPPRESS)
        sp.add_argument("--alias", default=argparse.SUPPRESS)
        sp.add_argument("--receive-dir", default=argparse.SUPPRESS)
        sp.add_argument("--announce-interval", type=float, default=argparse.SUPPRESS)
        sp.set_defaults(func=globals()["cmd_" + name.replace("-", "_")])
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
