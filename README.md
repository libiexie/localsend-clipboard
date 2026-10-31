# LocalSend 剪贴板 · LocalSend Clipboard

> 手机用 [LocalSend](https://localsend.org/) 发来的 **文字 / 图片 / 文件**，自动写入 Windows 剪贴板，直接 `Ctrl+V`。
> 电脑端**零点击**，不需要在弹窗里点"接受"，也不依赖官方桌面版（可选桥接模式）。

[![platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-0078D6)](https://github.com/libiexie/localsend-clipboard)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)

![tray](assets/tray.png)

---

## 功能

- **零交互**：电脑作为局域网中的一个 LocalSend 设备（默认名「电脑剪贴板」）；手机首次确认一次证书指纹后，
  发文字/图片/文件，电脑端无需任何操作，内容直接进剪贴板。
- **文字 → 文本**；**图片 → 剪贴板图片**（支持 HEIC/HEIF，自动按 EXIF 方向纠正）；**文件 → 保存到 `Downloads` 并复制文件列表**。
- **协议兼容 LocalSend v2.2**：UDP 组播发现（`224.0.0.167:53317`）、HTTPS API（默认 `53318`）、
  自签证书 + SHA-256 指纹 pinning、`register / info / prepare-upload / upload / cancel` 全流程。
- **系统托盘 + 气泡通知**：收到什么就弹什么；右键菜单管理运行状态、指纹、接收目录、开机自启。
- **可选桥接模式**：监听官方 LocalSend 桌面版的接收历史，发送给电脑版 App 时也能自动写剪贴板。
- **MCP 接口**：附带 stdio MCP server，AI Agent 可查询「最近收到什么」、启停服务。
- **单文件 exe**：PyInstaller 打包（内置 Python、Pillow、pillow-heif、cryptography、pystray），
  复制到任意 Windows 10/11 x64 双击即用，无需安装任何依赖。
- **完全本机运行**：只监听局域网端口，不访问互联网；接收内容只写入剪贴板和接收目录。

## 快速开始（推荐：下载 exe）

1. 到 [Releases](https://github.com/libiexie/localsend-clipboard/releases) 下载 `LocalSendClipboard.exe`。
2. 双击运行；首次会弹一次 **UAC**（用于自动添加 Windows 防火墙入站规则 TCP `53318`，否则手机连不上）。
   - SmartScreen 提示"未知发布者"时：更多信息 → 仍要运行。
3. 任务栏通知区域出现托盘图标（Win11 可能折叠在 `^` 里）。
4. 手机打开 LocalSend → 发送 → 选择设备 **「电脑剪贴板」**；
   首次发送时手机会显示证书指纹：电脑上右键托盘 → **「复制设备指纹（手机核对用）」**，两边一致后确认一次。
5. 之后手机发文字 / 照片 / 文件，电脑直接 `Ctrl+V` 使用。

> 接收文件默认保存在 `%USERPROFILE%\Downloads`；配置/证书/日志在 `%LOCALAPPDATA%\LocalSendClipboard`。

## 从源码运行（开发）

```powershell
pip install -r requirements.txt

# 方式一：集成主程序（接收器 + 托盘 + 可选桥接）
python app.py

# 方式二：分别运行
python clipboard_receiver.py run          # 零交互接收器（前台）
python clipboard_receiver.py start        # 后台启动
python clipboard_receiver.py status       # 状态/指纹/最近接收
python clipboard_receiver.py stop
python localsend_clipboard.py run         # 桥接模式（监听官方 App 接收历史）
python tray.py                            # 仅托盘图标（管理接收器）
python mcp_server.py                      # MCP stdio server
```

常用参数：

```text
python app.py --port 53318 --no-bridge        # 自定义端口 / 不启用桥接
python app.py --state-dir .\state             # 自定义状态目录（测试用）
python clipboard_receiver.py fingerprint      # 打印本机证书指纹
```

## 手机端首次使用

1. 手机与电脑在**同一局域网/Wi-Fi**；
2. 手机 LocalSend → 发送 → 设备列表选择「电脑剪贴板」；
3. 首次会要求确认指纹（TOFU），核对电脑托盘菜单里的指纹后确认；
4. 以后直接发送即可。

> Windows 网络位置建议设为"专用网络"；公司/酒店 Wi-Fi 可能隔离设备导致发现失败。

## 工作原理

```text
┌────────────┐   UDP 组播 224.0.0.167:53317（announce）   ┌──────────────────────────────┐
│  手机端     │ ─────────────────────────────────────────▶ │  Windows 端（本项目）          │
│ LocalSend  │   HTTPS POST /api/localsend/v2/register     │  ├ 接收器（端口 53318，TLS）    │
│            │ ◀───────────────────────────────────────── │  ├ 剪贴板写入（图片/文本/文件）  │
│            │   prepare-upload / upload（文件流）          │  ├ 托盘 + 通知                  │
└────────────┘ ─────────────────────────────────────────▶ │  └ 可选：桥接官方 App 接收历史   │
                                                          └──────────────────────────────┘
```

- **设备身份**：自签 RSA-2048 证书（`CN=LocalSend User`），设备指纹 = 证书 DER 的 SHA-256（大写十六进制）；
- **发现**：周期性发送 LocalSend v2.2 announce 报文；手机端收到后回调 `register`；
- **文字消息**：手机把文字作为单个 `text/*` FileDto 发送，`preview` 字段携带全文；接收方返回 `204` 表示"无需文件传输"；
- **文件**：`prepare-upload` 返回 `sessionId` 与每个文件的 token，随后 `upload` 流式上传（支持 chunked）。

更多细节见 [docs/PROTOCOL.md](docs/PROTOCOL.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 配置

`%LOCALAPPDATA%\LocalSendClipboard\receiver\receiver.json`（首次运行自动生成）：

```json
{
  "alias": "电脑剪贴板",
  "device_model": "Windows",
  "port": 53318,
  "receive_dir": "%USERPROFILE%\\Downloads",
  "multicast_group": "224.0.0.167",
  "multicast_port": 53317,
  "announce_interval": 5.0,
  "verify_checksums": true
}
```

| 字段 | 说明 |
|---|---|
| `alias` | 手机设备列表里显示的名字 |
| `port` | HTTPS 接收端口（防火墙需放行该端口） |
| `receive_dir` | 接收文件保存目录，支持环境变量 |
| `announce_interval` | 组播通告间隔秒数（设备发现） |
| `verify_checksums` | 校验上传文件的 SHA-256 |

桥接模式配置：`%LOCALAPPDATA%\LocalSendClipboard\config.json`（`file_batch_gap_seconds` 为多文件合并窗口）。

## 安全说明

- 电脑端使用**自签证书**，手机端按 announce 中的 SHA-256 指纹 pinning；首次确认（TOFU）后固定。
- 接收器与官方 LocalSend 一样采用"局域网信任"模型：**同一局域网内知道该设备指纹的对端都可以向它发送文件**。
  如需更严格限制，可自行在代码中加入 PIN/白名单（欢迎 PR）。
- 本项目不访问互联网，只监听局域网端口；接收内容仅写入剪贴板与接收目录。
- 本项目是独立实现，**不是** LocalSend 官方客户端，与 LocalSend 项目无隶属关系；LocalSend 是 Tien Do Nam 的 GPL-3.0 开源项目。
  本仓库代码为独立实现（协议互操作），未包含 LocalSend 源码。

## 构建单文件 exe

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1
# 产物：dist\LocalSendClipboard.exe（内置运行依赖，无需 Python）
```

或手动执行：

```powershell
pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --noconsole ^
  --name LocalSendClipboard --icon build\app.ico --version-file packaging\version_info.txt ^
  --collect-all pillow_heif --collect-all pystray --collect-submodules cryptography ^
  --hidden-import PIL app.py
```

首次运行时 exe 会自动请求一次 UAC 添加防火墙规则（`scripts\allow-firewall.ps1` 也提供了手动放行方式）。

## 测试

```powershell
pip install -r requirements.txt
python tests\test_e2e_receiver.py   # 需要 Windows（会读写剪贴板）
python tests\test_clipboard.py
```

`tests/test_e2e_receiver.py` 会启动一个测试用接收器（独立端口/目录），模拟手机完成
`register → prepare-upload → upload`，覆盖文字、图片、单文件、多文件、chunked 上传与 cancel。

## 目录结构

```text
app.py                 集成主程序（接收器 + 托盘 + 桥接 + 防火墙引导）
clipboard_receiver.py  零交互 LocalSend 兼容接收器（协议 v2.2）
clipboard_win.py       Windows 剪贴板读写（文本/图片 DIB+PNG/文件列表，HEIC 支持）
bridge.py              桥接模式：监听官方 LocalSend 桌面版接收历史
tray.py                独立托盘程序（开发/源码运行用）
localsend_clipboard.py 桥接模式 CLI
mcp_server.py          MCP stdio server
scripts/               防火墙放行、exe 打包脚本
docs/                  协议、架构、exe 使用说明
packaging/             PyInstaller 版本信息
tests/                 端到端测试
skill/                 AI Agent 技能说明（可选）
```

## License

[MIT](LICENSE) © 2026 libiexie and contributors
