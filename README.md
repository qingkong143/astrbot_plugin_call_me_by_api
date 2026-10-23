# astrbot_plugin_call_me_by_api

把 AstrBot 的私聊变成一个人工回复的 **Anthropic 兼容 API**。外部程序发起请求后，插件会把请求转发到绑定的私聊；你在私聊里回复内容，插件再把它作为 API 响应返回。

- 兼容 Anthropic Messages API（支持流式 SSE）
- 提供 `/v1/models` 模型查询接口
- 插件层 API Key 可自定义，留空自动生成 `sk-` 随机密钥
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
| `require_api_key` | bool | `true` | 是否校验插件层 API Key（平台鉴权始终存在） |
| `api_key` | string | 空 | 插件层 API Key；留空时首次启动自动生成 `sk-` 随机串并写回配置 |
| `reply_timeout` | int | `300` | 等待人工回复的超时时间（秒） |
| `stream_keepalive` | int | `15` | 流式响应期间发送 SSE ping 的间隔（秒） |
| `max_chars` | int | `4000` | 转发到私聊的请求文本最大字符数，超出截断 |
| `history_messages` | int | `6` | 转发到私聊的历史消息条数 |
| `model_id` | string | `qingkong143-flash-4.1-arbirated` | 对外展示的模型 ID |
| `model_display_name` | string | `qingkong143-flash-4.1-arbirated` | 模型列表中显示的名称 |
| `tool_call_error_message` | string | `不支持工具调用` | 收到工具调用请求时返回的错误信息 |

## HTTP 接口

路由以插件目录名作为前缀。假设插件目录名为 `call_me_by_api`，AstrBot 监听 `6185` 端口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/plugins/extensions/call_me_by_api/v1/messages` | 发起对话（Anthropic Messages） |
| GET | `/api/v1/plugins/extensions/call_me_by_api/v1/models` | 查询模型列表 |
| GET | `/api/v1/plugins/extensions/call_me_by_api/v1/models/<model_id>` | 查询单个模型 |

## 鉴权

请求会经过两层鉴权，**两层相互独立**：

### 1. AstrBot 平台鉴权（始终存在）

插件路由由 AstrBot 挂载，进入插件代码前必须先通过平台鉴权。需要在 AstrBot 面板创建带 `plugin` 权限的 API Key，并通过以下任一方式传递：

- `Authorization: Bearer <ASTRBOT_API_KEY>`
- `Authorization: ApiKey <ASTRBOT_API_KEY>`
- `X-API-Key: <ASTRBOT_API_KEY>`
- query 参数 `?api_key=<ASTRBOT_API_KEY>`

> 注意：平台鉴权不读取 Anthropic SDK 默认发送的 `x-api-key`，因此这个请求头留给插件层使用。

### 2. 插件层 API Key

由 `api_key` 配置项控制：

- 在插件配置里填自定义值，例如 `sk-woshizhenren`。
- 留空时，插件首次启动会自动生成 `sk-` 开头的随机密钥，并写回插件配置。
- 插件层接受 `x-api-key`、`Authorization: Bearer/ApiKey`、`X-API-Key` 或 query 参数，其中 `x-api-key` 优先级最高。
- 关闭 `require_api_key` 后跳过这一层，只依赖平台鉴权。

在私聊中发送 `/apikey` 可查看当前密钥，发送 `/apikey reset` 可重新生成（管理员权限）。

### 两层同时使用

当插件层和平台层都启用时，Anthropic 客户端可以这样配置：`x-api-key` 放插件 key，`Authorization` 放平台 key。

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api",
    api_key="sk-woshizhenren",  # 插件层，SDK 会自动放入 x-api-key
    default_headers={"Authorization": "Bearer <ASTRBOT_API_KEY>"},  # 平台层
)

print(client.models.list())

resp = client.messages.create(
    model="qingkong143-flash-4.1-arbirated",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.content[0].text)
```

### 模型查询

```bash
curl -H "Authorization: Bearer $ASTRBOT_API_KEY" \
  -H "x-api-key: $PLUGIN_API_KEY" \
  http://127.0.0.1:6185/api/v1/plugins/extensions/call_me_by_api/v1/models
```

返回 Anthropic Models API 格式，包含 `qingkong143-flash-4.1-arbirated`。

### 发起对话

```bash
curl -X POST \
  -H "Authorization: Bearer $ASTRBOT_API_KEY" \
  -H "x-api-key: $PLUGIN_API_KEY" \
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
