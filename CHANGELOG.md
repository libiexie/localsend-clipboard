# Changelog

## v1.0.0 (2026-10-31)

首个公开版本。

### 功能
- 零交互 LocalSend v2.2 接收器：组播发现 + HTTPS API（默认 53318）+ 自签证书指纹 pinning
- 文字 / 图片 / 文件自动写入 Windows 剪贴板，无需电脑端点击确认
- 图片支持 HEIC/HEIF（内置 pillow-heif）与 EXIF 方向纠正
- 系统托盘 + 气泡通知、状态/指纹/接收目录/日志/重启/自启/退出菜单
- 可选桥接模式：监听官方 LocalSend 桌面版接收历史
- MCP stdio 接口
- PyInstaller 单文件 exe（内置全部依赖，首次运行自动放行防火墙）
- 端到端测试：register / prepare-upload / upload（含 chunked）/ cancel，文字/图片/单文件/多文件
