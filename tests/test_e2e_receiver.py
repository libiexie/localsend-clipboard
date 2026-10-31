"""剪贴板接收器端到端自测。

1) 启动测试用接收器（独立端口/目录/状态，关闭组播）。
2) 模拟手机 LocalSend v2.2 客户端：register / prepare-upload / upload（普通 + chunked）/ cancel。
3) 验证文字、图片、单文件、多文件分别正确进入 Windows 剪贴板。
"""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)
import clipboard_win as cb  # noqa: E402
from PIL import Image  # noqa: E402

PY = sys.executable
SCRIPT = os.path.join(PROJ, "clipboard_receiver.py")
TEST = os.path.join(HERE, "receiver_test")
STATE = os.path.join(TEST, "state")
RECV = os.path.join(TEST, "recv")
PORT = 53418
HOST = "127.0.0.1"
results = []


def check(name, cond, detail=None):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if detail is None else "  " + json.dumps(detail, ensure_ascii=False)[:260]))


def wait_port(timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((HOST, PORT), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def make_ctx(server_fp_expected=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])

    def verify(conn, cert, errno, depth, ok):
        if server_fp_expected:
            fp = hashlib.sha256(cert).hexdigest().upper()
            if fp != server_fp_expected:
                raise ssl.SSLError(f"fingerprint mismatch: {fp} != {server_fp_expected}")
        return ok
    ctx.verify_callback = verify
    return ctx


def http_post(ctx, path, body: bytes, content_type="application/json", timeout=20):
    import http.client
    c = http.client.HTTPSConnection(HOST, PORT, context=ctx, timeout=timeout)
    try:
        c.request("POST", path, body=body, headers={"Content-Type": content_type})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def http_get(ctx, path):
    import http.client
    c = http.client.HTTPSConnection(HOST, PORT, context=ctx, timeout=20)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def prepare(ctx, info, files):
    return http_post(ctx, "/api/localsend/v2/prepare-upload",
                     json.dumps({"info": info, "files": files}).encode())


def upload(ctx, session_id, fid, token, data):
    p = f"/api/localsend/v2/upload?sessionId={session_id}&fileId={fid}&token={token}"
    return http_post(ctx, p, data, content_type="application/octet-stream")


def wait_clip(pred, timeout=10):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = cb.read_snapshot()
        if pred(last):
            return last
        time.sleep(0.2)
    return last


def chunked_upload(ctx, session_id, fid, token, data):
    """raw TLS socket + Transfer-Encoding: chunked（模拟 reqwest 流式 body）。"""
    raw = socket.create_connection((HOST, PORT), timeout=20)
    ss = ctx.wrap_socket(raw, server_hostname=HOST)
    path = f"/api/localsend/v2/upload?sessionId={session_id}&fileId={fid}&token={token}"
    head = (f"POST {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            "Content-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
    ss.sendall(head)
    view = memoryview(data)
    step = 1000
    for i in range(0, len(view), step):
        chunk = view[i:i + step]
        ss.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
    ss.sendall(b"0\r\n\r\n")
    buf = b""
    while b"\r\n\r\n" not in buf:
        part = ss.recv(4096)
        if not part:
            break
        buf += part
    ss.close()
    status_line = buf.split(b"\r\n", 1)[0].decode("latin1")
    return int(status_line.split(" ")[1]) if " " in status_line else 0


def uid():
    return uuid.uuid4().hex[:10]


def main():
    if os.path.isdir(TEST):
        shutil.rmtree(TEST)
    os.makedirs(STATE)
    os.makedirs(RECV)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [PY, SCRIPT, "run", "--state-dir", STATE, "--port", str(PORT),
         "--receive-dir", RECV, "--alias", "e2e-receiver", "--announce-interval", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        ok = wait_port()
        check("receiver listening", ok, {"port": PORT})
        if not ok:
            return 1
        oc = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        oc.check_hostname = False
        oc.verify_mode = ssl.CERT_NONE
        oc.set_alpn_protocols(["http/1.1"])
        probe = socket.create_connection((HOST, PORT), timeout=5)
        tls = oc.wrap_socket(probe, server_hostname=HOST)
        peer = tls.getpeercert(binary_form=True)
        tls.close()
        fp = hashlib.sha256(peer).hexdigest().upper()
        status = json.load(open(os.path.join(STATE, "status.json"), encoding="utf-8"))
        check("cert fingerprint advertised == presented", fp == status.get("fingerprint"), {"fp": fp[:20]})

        ctx = make_ctx(fp)  # 等同手机 LocalSend 的证书指纹 pinning
        info = {"alias": "e2e-phone", "version": "2.2", "deviceModel": "e2e", "deviceType": "desktop",
                "fingerprint": "A" * 64, "port": 54321, "protocol": "https", "download": False}

        st, body = http_post(ctx, "/api/localsend/v2/register", json.dumps(info).encode())
        check("register 200", st == 200 and json.loads(body)["alias"] == "e2e-receiver", {"status": st})
        st, body = http_get(ctx, "/api/localsend/v2/info")
        check("info 200", st == 200 and json.loads(body)["version"] == "2.2", {"status": st})

        # 文字消息（preview 承载全文，204，无上传）
        text = "零交互测试：手机发来的文字 ✅ 中文"
        tid = "msg-" + uid()
        tdto = {"id": tid, "fileName": uid() + ".txt", "size": len(text.encode()),
                "fileType": "text/plain", "preview": text}
        cb.set_text("SENTINEL")
        st, _ = prepare(ctx, info, {tid: tdto})
        check("text prepare -> 204", st == 204, {"status": st})
        snap = wait_clip(lambda s: s.get("text") == text)
        check("text -> clipboard", snap.get("text") == text, {"text": (snap.get("text") or "")[:40]})

        # 单文件（带 sha256）
        fdata = b"receiver e2e file " + uid().encode()
        fname = "e2e-recv-file.dat"
        fid = "file-" + uid()
        fdto = {"id": fid, "fileName": fname, "size": len(fdata), "fileType": "application/octet-stream",
                "sha256": hashlib.sha256(fdata).hexdigest()}
        st, body = prepare(ctx, info, {fid: fdto})
        check("file prepare 200", st == 200, {"status": st})
        resp = json.loads(body)
        st2, _ = upload(ctx, resp["sessionId"], fid, resp["files"][fid], fdata)
        snap = wait_clip(lambda s: any(os.path.basename(p) == fname for p in (s.get("files") or [])))
        saved = next((p for p in (snap.get("files") or []) if os.path.basename(p) == fname), None)
        check("file -> clipboard", st2 == 200 and bool(saved and os.path.exists(saved)), {"saved": saved})
        if saved:
            check("file content matches", open(saved, "rb").read() == fdata, None)

        # 图片
        img_local = os.path.join(TEST, "e2e-img.png")
        Image.new("RGB", (133, 77), (200, 40, 120)).save(img_local)
        idata = open(img_local, "rb").read()
        iid = "img-" + uid()
        idto = {"id": iid, "fileName": "e2e-recv-img.png", "size": len(idata), "fileType": "image/png",
                "sha256": hashlib.sha256(idata).hexdigest()}
        st, body = prepare(ctx, info, {iid: idto})
        resp = json.loads(body)
        st2, _ = upload(ctx, resp["sessionId"], iid, resp["files"][iid], idata)
        snap = wait_clip(lambda s: s.get("image") and s["image"].get("width") == 133)
        check("image -> clipboard", st2 == 200 and (snap.get("image") or {}).get("width") == 133,
              {"image": snap.get("image")})

        # 多文件（一次 prepare 两个文件）
        f1data, f2data = b"multi one", b"multi two"
        f1id, f2id = "m1-" + uid(), "m2-" + uid()
        files = {
            f1id: {"id": f1id, "fileName": "e2e-multi-1.txt", "size": len(f1data),
                   "fileType": "text/plain", "sha256": hashlib.sha256(f1data).hexdigest()},
            f2id: {"id": f2id, "fileName": "e2e-multi-2.dat", "size": len(f2data),
                   "fileType": "application/octet-stream", "sha256": hashlib.sha256(f2data).hexdigest()},
        }
        st, body = prepare(ctx, info, files)
        check("multi prepare 200", st == 200, {"status": st})
        resp = json.loads(body)
        upload(ctx, resp["sessionId"], f1id, resp["files"][f1id], f1data)
        upload(ctx, resp["sessionId"], f2id, resp["files"][f2id], f2data)
        snap = wait_clip(lambda s: len(s.get("files") or []) >= 2 and
                         all(any(n in p for p in s["files"]) for n in ("e2e-multi-1.txt", "e2e-multi-2.dat")))
        names = [os.path.basename(p) for p in (snap.get("files") or [])]
        check("multi files -> clipboard", "e2e-multi-1.txt" in names and "e2e-multi-2.dat" in names, {"names": names})

        # chunked 上传
        cdata = b"chunked body " + uid().encode()
        cid = "chunk-" + uid()
        cdto = {"id": cid, "fileName": "e2e-chunked.dat", "size": len(cdata),
                "fileType": "application/octet-stream", "sha256": hashlib.sha256(cdata).hexdigest()}
        st, body = prepare(ctx, info, {cid: cdto})
        resp = json.loads(body)
        st2 = chunked_upload(ctx, resp["sessionId"], cid, resp["files"][cid], cdata)
        snap = wait_clip(lambda s: any("e2e-chunked.dat" in p for p in (s.get("files") or [])))
        check("chunked upload -> clipboard",
              st2 == 200 and any("e2e-chunked.dat" in p for p in (snap.get("files") or [])),
              {"status": st2})

        # cancel（新连接 + 重试，避免客户端连接复用抖动）
        time.sleep(0.5)
        cid2 = "cancel-" + uid()
        st, body = prepare(ctx, info, {cid2: {"id": cid2, "fileName": "x.bin", "size": 1,
                                              "fileType": "application/octet-stream"}})
        sid = json.loads(body)["sessionId"]
        st2 = -1
        for attempt in range(2):
            try:
                st2, _ = http_post(make_ctx(fp), f"/api/localsend/v2/cancel?sessionId={sid}", b"", timeout=6)
                break
            except Exception as e:  # noqa: BLE001
                print("  (cancel attempt failed:", type(e).__name__, "retrying)")
                time.sleep(0.5)
        check("cancel 200", st2 == 200, {"status": st2})
    finally:
        open(os.path.join(STATE, "stop.flag"), "w").close()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
    passed = sum(1 for _, ok_, _ in results if ok_)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
