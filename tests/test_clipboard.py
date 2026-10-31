"""Windows 剪贴板读写冒烟测试（会覆盖当前剪贴板内容）。

用法：python tests/test_clipboard.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clipboard_win as cb  # noqa: E402


def main() -> int:
    text = "localsend-clipboard smoke test 测试"
    print("set_text ...", end=" ", flush=True)
    cb.set_text(text)
    snap = cb.read_snapshot()
    assert snap.get("text") == text, f"text roundtrip failed: {snap.get('text')!r}"
    print("PASS")

    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("hello")
    try:
        print("set_files ...", end=" ", flush=True)
        cb.set_files([path])
        snap = cb.read_snapshot()
        files = snap.get("files") or []
        assert files and os.path.basename(files[0]) == os.path.basename(path), f"files roundtrip failed: {files}"
        print("PASS")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
