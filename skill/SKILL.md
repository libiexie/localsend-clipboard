---
name: localsend-clipboard
description: Windows 端接收手机 LocalSend 发送的文字/图片/文件并自动写入剪贴板（零交互独立接收器，兼容 LocalSend v2.2 协议，支持托盘、HEIC、开机自启、单文件 exe）。适用于"手机传文件到电脑后自动进剪贴板/直接粘贴"、查询最近接收内容、启停接收器、排查防火墙等场景。
---

# LocalSend 剪贴板（Windows）

把手机用 LocalSend 发来的内容自动写入 Windows 剪贴板：

- 文字消息 → 剪贴板文本
- 图片 → 剪贴板图片（支持 HEIC/HEIF，自动方向纠正）+ 文件列表
- 文件 → 保存到 `%USERPROFILE%\Downloads`，剪贴板为文件拖放列表

电脑作为局域网里的一个 LocalSend 设备（默认名「电脑剪贴板」，默认端口 53318）出现，手机首次确认证书指纹后即可零点击发送。

## 快速使用

仓库：<https://github.com/libiexie/localsend-clipboard>

```powershell
# 方式一：下载 Releases 中的 LocalSendClipboard.exe 双击运行（首次 UAC 放行防火墙）
# 方式二：源码运行
pip install -r requirements.txt
python app.py                 # 接收器 + 托盘
```

首次运行若手机连不上，放行防火墙：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\allow-firewall.ps1
```

## 常用命令

```powershell
python clipboard_receiver.py status       # 运行状态 / 端口 / 指纹 / 最近接收
python clipboard_receiver.py start|stop
python clipboard_receiver.py fingerprint  # 设备证书 SHA-256（手机核对）
python localsend_clipboard.py status      # 桥接模式状态
python mcp_server.py                      # MCP stdio server
```

## 状态与日志位置

- 配置/证书/状态：`%LOCALAPPDATA%\LocalSendClipboard\receiver\`
- 最近接收记录：`status.json` 的 `last_items`
- 接收日志：`receiver.log`
- 接收文件：`%USERPROFILE%\Downloads`

## 注意

- 不要把剪贴板私密内容输出给用户，除非用户明确要求。
- 接收器对同局域网设备开放（与官方 LocalSend 相同的局域网信任模型），如实说明这一点。
- 手机端首次发送需确认证书指纹（TOFU），可用托盘菜单「复制设备指纹（手机核对用）」核对。
- 防火墙规则绑定程序路径：移动 exe 后需重新放行（托盘菜单「修复防火墙」）。
