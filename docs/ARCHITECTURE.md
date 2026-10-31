# 架构

## 组件

```text
app.py（推荐入口）
├─ 接收器线程      clipboard_receiver.py  ── TLS(53318) + UDP 组播 announce
├─ 桥接线程（可选） bridge.py              ── 轮询官方 App settings.json 的接收历史
├─ 托盘/通知        pystray + 气泡通知      ── 状态、指纹、目录、日志、重启、自启、退出
└─ 首启防火墙       提权子进程 + netsh 规则 ── 仅打包 exe 首次运行时需要
```

| 文件 | 职责 |
|---|---|
| `app.py` | 集成主程序：单进程内组合接收器、桥接、托盘、通知、防火墙引导 |
| `clipboard_receiver.py` | 零交互接收器：HTTPS 服务、组播通告、会话/token 校验、文件落盘、调用剪贴板 |
| `clipboard_win.py` | Windows 剪贴板读写：`CF_UNICODETEXT` / `CF_HDROP` / `CF_DIB` + 注册 `PNG`；HEIC/HEIF 解码与 EXIF 方向纠正 |
| `bridge.py` | 桥接模式：轮询 `%APPDATA%\LocalSend\settings.json` 中 `flutter.ls_receive_history`，新条目写剪贴板 |
| `tray.py` | 独立托盘程序（不是 exe 时的源码运行方式，会拉起/监管接收器） |
| `localsend_clipboard.py` | 桥接模式命令行：`run / start / stop / status / once / config / install-autostart` |
| `mcp_server.py` | MCP stdio server：查询状态/最近接收、等待新内容、启停服务 |
| `scripts/allow-firewall.ps1` | 放行入站 TCP 53318（管理员，一次性） |
| `scripts/build-exe.ps1` | PyInstaller 单文件打包 |

## 数据流（零交互模式）

```text
手机 LocalSend
   │ ① UDP announce（组播）
   ▼
接收器：发现/注册
   │ ② POST /register
   │ ③ POST /prepare-upload
   │     ├─ 文字消息（text/* + preview）→ 写剪贴板 → 204
   │     └─ 文件 → 返回 sessionId + 每个文件 token
   │ ④ POST /upload（流式/chunked）→ 校验 SHA-256 → 保存到接收目录
   ▼
剪贴板队列（单线程 + 重试）
   ├─ 文字 → CF_UNICODETEXT
   ├─ 图片 → CF_DIB + PNG（+ CF_HDROP）
   └─ 文件 → CF_HDROP
   ▼
托盘气泡通知：已复制到剪贴板
```

### 为什么剪贴板写入要单独排队？

Windows 剪贴板同一时刻只能被一个进程打开；HTTP 处理线程并发写入会返回
`ERROR_CLIPBOARD_NOT_OPEN (1418)`。因此所有剪贴板写入都经过一个专用工作线程串行执行并带重试，
图片的 DIB/PNG 字节也在打开剪贴板**之前**预计算好。

## 状态与持久化

| 路径 | 内容 |
|---|---|
| `%LOCALAPPDATA%\LocalSendClipboard\receiver\receiver.json` | 配置（别名、端口、接收目录、通告间隔等） |
| `...\receiver\cert.pem` / `key.pem` | 设备自签证书（首次运行生成；删除后指纹会变） |
| `...\receiver\status.json` | 运行状态、通告次数、最近接收条目 |
| `...\receiver\receiver.log` | 接收日志（register/upload/剪贴板结果） |
| `...\app.log` / `tray.log` | 主程序/托盘日志、通知记录 |
| `...\firewall-setup.json` | 打包 exe 首次防火墙放行标记（记 exe 路径与端口） |

## 兼容性策略

- 接收器只实现"接收方"能力，不需要兼容发送方的内部实现细节；
- 对移动端 LocalSend（Android/iOS）与官方桌面版发送端均按同一套 v2.2 报文处理；
- 文字消息使用 `preview` 字段（官方实现同款策略），文件按 `prepare-upload` 返回 token 校验后接收。
