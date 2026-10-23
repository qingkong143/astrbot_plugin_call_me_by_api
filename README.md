# astrbot_plugin_call_me_by_api

把 AstrBot 的私聊变成一个人工回复的 **Anthropic 兼容 API**。外部程序发起请求后，插件会把请求转发到绑定的私聊；你在私聊里回复内容，插件再把它作为 API 响应返回。

- 兼容 Anthropic Messages API（支持流式 SSE）
- 提供 `/v1/models` 模型查询接口
- 可选插件层 API Key：可自定义 `sk-` key，留空自动生成
- 请求中出现工具调用时，强制返回 `不支持工具调用` 错误
- 超时、保活、截断长度、历史条数等均可配置

## 安装与绑定

1. 将本插件放入 AstrBot 的 `data/plugins/` 目录并启用。
2. 私聊机器人发送 `/apibind`，绑定接收 API 请求的私聊（需要管理员权限）。
3. 外部请求会以消息形式发到该私聊，使用 `/apireply <请求ID> <内容>` 回复。

## 配置项

在 AstrBot 插件配置页中修改（定义于 `_conf_schema.json`）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `require_api_key` | bool | `false` | 是否额外校验插件层 API Key；平台鉴权始终存在，默认不需要 |
| `api_key` | string | 空 | 启用插件层校验时使用；留空会自动生成 `sk-` 随机串并写回配置 |
| `reply_timeout` | int | `300` | 等待人工回复的超时时间（秒） |
| `stream_keepalive` | int | `15` | 流式响应期间发送 SSE ping 的间隔（秒） |
| `max_chars` | int | `4000` | 转发到私聊的请求文本最大字符数，超出截断 |
| `history_messages` | int | `6` | 转发到私聊的历史消息条数 |
| `model_id` | string | `qingkong143-flash-4.1-arbirated` | 对外展示的模型 ID |
| `model_display_name` | string | `qingkong143-flash-4.1-arbirated` | 模型列表中显示的名称 |
| `tool_call_error_message` | string | `不支持工具调用` | 收到工具调用请求时返回的错误信息 |

## HTTP 接口

路由前缀会自动注册三个别名，用哪个都行：

- 插件目录名，例如 `call_me_by_api`
- metadata 里的插件名，例如 `astrbot_plugin_call_me_by_api`
- 去掉 `astrbot_plugin_` 前缀的短名，例如 `call_me_by_api`

假设 AstrBot 监听 `6185` 端口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/plugins/extensions/call_me_by_api/v1/messages` | 发起对话（Anthropic Messages） |
| GET | `/api/v1/plugins/extensions/call_me_by_api/v1/models` | 查询模型列表 |
| GET | `/api/v1/plugins/extensions/call_me_by_api/v1/models/<model_id>` | 查询单个模型 |

模型列表同时包含 Anthropic（`type`/`created_at`）和 OpenAI（`object`/`created`）字段，返回形如：

```json
{
  "object": "list",
  "data": [
    {
      "type": "model",
      "object": "model",
      "id": "qingkong143-flash-4.1-arbirated",
      "display_name": "qingkong143-flash-4.1-arbirated",
      "created_at": "2026-01-01T00:00:00Z",
      "created": 1767225600,
      "owned_by": "call_me_by_api"
    }
  ],
  "has_more": false
}
```

> 如果这里返回的是 `{"status":"error","message":"未找到该路由","data":{}}`，说明请求没有匹配到插件子路由：请确认 AstrBot 里运行的是最新插件代码、插件已重新加载，并且 URL 前缀是上面三个别名之一。

## 鉴权

### AstrBot 平台鉴权（始终存在）

插件路由由 AstrBot 挂载，进入插件代码前必须先通过平台鉴权。需要在 AstrBot 面板创建**带 `plugin` 权限范围**的 API Key，并通过以下任一方式传递：

- `Authorization: Bearer <ASTRBOT_API_KEY>`
- `Authorization: ApiKey <ASTRBOT_API_KEY>`
- `X-API-Key: <ASTRBOT_API_KEY>`
- query 参数 `?api_key=<ASTRBOT_API_KEY>`

> 平台鉴权不读取 Anthropic SDK 默认发送的 `x-api-key`，所以 SDK 里要把平台 key 放进 `Authorization` 头。

**常见错误码：**

| 响应 | 原因 |
| --- | --- |
| `401 Missing API key` | 没有携带平台 API Key |
| `401 Invalid API key` | 平台 API Key 错误或已撤销 |
| `403 Insufficient API key scope` | Key 有效，但创建时**没有勾选 `plugin` 权限** |
| `401 无效的 API Key` | 开启了插件层校验，但 `x-api-key` 不匹配（插件返回） |
| `200 {"status":"error","message":"未找到该路由","data":{}}` | 插件子路由未注册：URL 前缀不对、插件未更新或未重新加载 |

`?limit=1000` 这类分页参数不影响鉴权；模型列表接口会忽略它。

### 插件层 API Key（可选，默认关闭）

平台鉴权已经覆盖所有插件路由，因此插件层校验默认关闭（`require_api_key=false`），开启后相当于多一道防护：

- 在插件配置里填自定义值，例如 `sk-woshizhenren`；留空则自动生成 `sk-` 随机密钥并写回配置。
- 插件层接受 `x-api-key`、`Authorization: Bearer/ApiKey`、`X-API-Key` 或 query 参数，其中 `x-api-key` 优先级最高。
- 私聊发送 `/apikey` 查看当前密钥，`/apikey reset` 重新生成（管理员权限）。

### Anthropic SDK 示例（默认配置，仅平台鉴权）

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api",
    api_key="placeholder",  # SDK 要求非空，实际用下面的平台 key
    default_headers={"Authorization": "Bearer <ASTRBOT_API_KEY>"},
)

print(client.models.list())

resp = client.messages.create(
    model="qingkong143-flash-4.1-arbirated",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.content[0].text)
```

如果开启了插件层校验，再把 `api_key` 换成插件 key（SDK 会自动放进 `x-api-key`），`Authorization` 仍放平台 key：

```python
client = Anthropic(
    base_url="http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api",
    api_key="sk-woshizhenren",                                      # 插件层 → x-api-key
    default_headers={"Authorization": "Bearer <ASTRBOT_API_KEY>"},  # 平台层
)
```

### 模型查询

```bash
curl -H "Authorization: Bearer $ASTRBOT_API_KEY" \
  http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api/v1/models
```

（默认配置下不需要 `x-api-key`；若开启了 `require_api_key`，再加上 `-H "x-api-key: $PLUGIN_API_KEY"`。）

返回 Anthropic Models API 格式，包含 `qingkong143-flash-4.1-arbirated`。

### 发起对话

```bash
curl -X POST \
  -H "Authorization: Bearer $ASTRBOT_API_KEY" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api/v1/messages \
  -d '{
    "model": "qingkong143-flash-4.1-arbirated",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

请求会以消息形式出现在绑定私聊中，回复 `/apireply <请求ID> <内容>` 后，该内容作为 API 响应返回。设置 `"stream": true` 可获得 SSE 流式响应。

> 如果希望客户端只带 `sk-...` 就能访问，需要让插件绕过平台路由、自行监听一个端口。当前版本未实现该模式。

## 工具调用

本插件不执行任何工具。请求中出现以下任一内容时，返回 HTTP 400：

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "不支持工具调用"
  }
}
```

判定条件：`tools` 非空、显式指定了 `tool_choice`，或消息中包含 `tool_use` / `tool_result` 内容块。

## 相关链接

- [AstrBot 插件开发文档（中文）](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 插件开发文档（英文）](https://docs.astrbot.app/en/dev/star/plugin-new.html)
