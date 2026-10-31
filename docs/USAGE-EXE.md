# 独立 exe 使用说明（Windows）

`LocalSendClipboard.exe` 是单文件版：内置 Python 运行时与全部依赖，复制到任意 Windows 10/11 x64
即可运行，不需要安装 Python / Pillow / 任何环境。

## 第一次运行

1. 双击 `LocalSendClipboard.exe`。
   - SmartScreen 提示"未知发布者"：更多信息 → 仍要运行。
2. 若弹出 UAC（用户账户控制）→ 点"是"。
   - 这一步只做一次：自动为当前 exe 添加 Windows 防火墙入站规则（TCP 53318）。
   - 如果拒绝 UAC：程序仍会运行，但手机能看到设备后发送会失败；可稍后右键托盘 →
     「修复防火墙（需管理员）」重试。
3. 任务栏通知区域出现托盘图标（Windows 11 可能折叠在 `^` 中，可拖出固定）。

## 手机端

1. 手机与电脑在同一局域网/Wi-Fi；
2. 手机打开 LocalSend → 发送 → 设备列表选择「电脑剪贴板」；
3. 首次发送会显示证书指纹：电脑上右键托盘 →「复制设备指纹（手机核对用）」，
   两边 SHA-256 一致后确认（TOFU，一次即可）；
4. 之后发送文字/图片/文件，电脑直接 `Ctrl+V`。

## 托盘菜单

| 菜单项 | 作用 |
|---|---|
| 状态 | 运行状态、端口、设备名、防火墙是否放行、最近接收 |
| 复制设备指纹（手机核对用） | 把本机证书 SHA-256 放进剪贴板 |
| 打开接收目录 | 默认 `%USERPROFILE%\Downloads` |
| 打开日志目录 | `%LOCALAPPDATA%\LocalSendClipboard` |
| 重启接收器 | 修改配置后重启生效 |
| 修复防火墙（需管理员） | 移动 exe 位置/换端口后重新放行 |
| 开机自启（当前用户） | 勾选后登录自动启动 |
| 退出 | 停止接收器并退出程序 |

## 文件位置

- 接收文件：`%USERPROFILE%\Downloads`
- 配置：`%LOCALAPPDATA%\LocalSendClipboard\receiver\receiver.json`
- 证书：同目录 `cert.pem` / `key.pem`（删除后设备指纹会变，手机需重新确认）
- 日志：同目录 `receiver.log`、`%LOCALAPPDATA%\LocalSendClipboard\app.log`

## 卸载

1. 托盘右键「退出」；
2. 删除 `LocalSendClipboard.exe`；
3. 如需彻底清理：删除 `%LOCALAPPDATA%\LocalSendClipboard`（会删除证书与配置），
   并在托盘菜单里关闭「开机自启」（或删除注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\LocalSendClipboard`）。

## 常见问题

- **手机看不到设备**：确认同一局域网；确认托盘状态里"防火墙：已放行"；公司/酒店 Wi-Fi 可能隔离设备。
- **能看到设备但发送失败**：多为防火墙规则未生效 → 托盘「修复防火墙」。
- **手机提示指纹不一致**：核对托盘复制的指纹；若删过证书或换过电脑，手机上删除该设备重新发现。
- **图片粘贴到老软件失败**：超大图片的剪贴板数据可达数十 MB，可先保存文件再拖入。
- **移动 exe 后手机连不上**：防火墙规则绑定 exe 路径 → 「修复防火墙」。
