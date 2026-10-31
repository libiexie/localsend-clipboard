# LocalSend 协议兼容说明（v2.2）

本项目实现的是 **LocalSend v2.2 局域网协议**中"接收方"所需的部分，用于让手机端 LocalSend 把电脑当作一个
普通设备来发送内容。以下内容来自对 LocalSend 公开协议、官方客户端行为的分析，以及本项目实测记录。

## 端口与发现

| 项 | 值 |
|---|---|
| 组播地址 | `224.0.0.167` |
| 组播端口（UDP） | `53317` |
| HTTPS API 端口（TCP） | 本项目默认 `53318`（官方桌面版默认 `53317`，两者可共存） |
| 传输协议 | HTTPS（HTTP/1.1，ALPN `http/1.1`） |
| 证书 | 自签 RSA-2048，`CN=LocalSend User`，长期有效 |

### announce 报文（UDP 组播）

```json
{
  "alias": "电脑剪贴板",
  "version": "2.2",
  "deviceModel": "Windows",
  "deviceType": "desktop",
  "fingerprint": "3F67B436...270D9D",
  "port": 53318,
  "protocol": "https",
  "download": false,
  "announce": true
}
```

- 手机端收到 announce 后会向 `https://<源地址>:<port>` 发起 `POST /api/localsend/v2/register`；
- 电脑端周期性发送 announce（默认 5 秒），以保持设备在线可发现；
- `fingerprint` 为该设备证书 DER 的 **SHA-256，大写十六进制**；手机端据此做 TLS 证书 pinning（TOFU）。

## API

### `POST /api/localsend/v2/register`

请求体为对端 `info`（与 announce 类似）。响应：

```json
{"alias": "电脑剪贴板", "version": "2.2", "deviceModel": "Windows",
 "deviceType": "desktop", "fingerprint": "...", "port": 53318,
 "protocol": "https", "download": false}
```

### `GET /api/localsend/v2/info`

返回本机 info（同 register 响应）。部分客户端用于探测。

### `POST /api/localsend/v2/prepare-upload`

请求：

```json
{
  "info": { ... 手机端 info ... },
  "files": {
    "<fileId>": {
      "id": "<fileId>",
      "fileName": "IMG_0001.heic",
      "size": 1451165,
      "fileType": "image/heic",
      "sha256": "可选",
      "preview": "可选，文字消息时为全文"
    }
  }
}
```

响应：

```json
{"sessionId": "uuid", "files": {"<fileId>": "<token>"}}
```

**文字消息**：手机端把一段文字包装成单个 `text/*` 类型的 FileDto，`preview` 字段携带全文；
接收方若直接处理（把文字写入剪贴板）则返回 `HTTP 204`，手机端不会再上传文件。

### `POST /api/localsend/v2/upload?sessionId=&fileId=&token=`

请求体为文件二进制（支持 `Transfer-Encoding: chunked` 流式上传）。
响应 `200` 表示成功；token/session 不匹配返回 `403`。

### `POST /api/localsend/v2/cancel`

取消会话，返回 `200`。

## 文字消息判定规则

本项目的判定：本次 prepare-upload 只有 **1 个文件**，其 `fileType` 以 `text/` 开头，且 `preview` 非空 →
视为文字消息：写入剪贴板并返回 `204`。否则按文件接收。

## TLS 细节

- 服务端使用自签证书；手机端（rustls）以 announce 中的指纹作为期望值进行 pinning；
- 客户端（手机）也会提供自签客户端证书；本项目接收器与官方桌面版一致，**不强制要求客户端证书**
  （即接受任意有效自签客户端证书），这是 LocalSend 的局域网信任模型；
- ALPN 仅协商 `http/1.1`。

## 与官方桌面版的差异

| 项 | 官方桌面版 | 本项目接收器 |
|---|---|---|
| 监听端口 | 53317 | 默认 53318（可配置） |
| 接收确认 | 手机→电脑时需要在本机点"接受"（取决于设置） | 无确认，直接接收并写剪贴板 |
| 设备发现 | 启动时 announce（并响应 register） | 周期性 announce（默认 5s） |
| 文字消息处理 | 接收后显示消息页 | 直接写入剪贴板 |
| 依赖 | Flutter 桌面应用 | 独立 Python/单文件 exe，不依赖官方 App |

## 参考

- LocalSend 官方协议代码（GPL-3.0）：<https://github.com/localsend/localsend>
- LocalSend 官网：<https://localsend.org/>
