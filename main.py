from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request, stream_response

PLUGIN_DIR = Path(__file__).resolve().parent.name
MESSAGES_PATH = "/v1/messages"
MODELS_PATH = "/v1/models"
MODEL_PATH = "/v1/models/<model_id>"

# Models API 返回的模型创建时间（固定值，仅用于兼容客户端）。
MODEL_CREATED_AT = "2026-01-01T00:00:00Z"
MODEL_CREATED_TS = 1767225600  # 同一时间的 Unix 时间戳，供 OpenAI 风格客户端使用

# 配置默认值，需与 _conf_schema.json 保持一致。
CONF_DEFAULTS: dict[str, Any] = {
    "reply_timeout": 300,
    "stream_keepalive": 15,
    "max_chars": 4000,
    "history_messages": 6,
    "model_id": "qingkong143-flash-4.1-arbirated",
    "model_display_name": "qingkong143-flash-4.1-arbirated",
    # 平台鉴权（require_plugin_scope）已经覆盖插件路由，插件层默认关闭。
    "require_api_key": False,
    "api_key": "",
}


class RelayError(Exception):
    def __init__(self, message: str, error_type: str = "api_error", status: int = 500):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status = status


@dataclass
class Pending:
    req_id: str
    future: asyncio.Future[str]
    model: str
    created_at: float


@register("call_me_by_api", "qingkong143", "将 AstrBot 私聊封装成 Anthropic 兼容 API", "1.1.3")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        # 保留 AstrBotConfig 本体，自动生成 API Key 时需要写回配置文件。
        self.config: dict[str, Any] = config if config is not None else {}
        self._pending: dict[str, Pending] = {}
        self._owner_umo: str | None = None
        self._owner_sender: str | None = None
        self._load_config()
        # 同时兼容插件目录名与 metadata 插件名，避免部署后路径前缀不一致。
        for prefix in self._route_prefixes():
            context.register_web_api(
                f"/{prefix}{MESSAGES_PATH}", self.messages, ["POST"], "Anthropic 兼容接口"
            )
            context.register_web_api(
                f"/{prefix}{MODELS_PATH}", self.models, ["GET"], "模型列表"
            )
            context.register_web_api(
                f"/{prefix}{MODEL_PATH}", self.model, ["GET"], "模型详情"
            )

    def _route_prefixes(self) -> list[str]:
        """返回可用的路由前缀：目录名、metadata 插件名及其去掉前缀后的短名。"""
        names = {PLUGIN_DIR}
        name = getattr(self, "name", None)
        if isinstance(name, str) and name.strip():
            name = name.strip()
            names.add(name)
            prefix = "astrbot_plugin_"
            if name.startswith(prefix):
                names.add(name[len(prefix):])
        return sorted(names)

    def _load_config(self) -> None:
        """从插件配置中读取运行时参数，非法值回退到默认值。"""
        self._timeout = self._conf_int("reply_timeout", minimum=1)
        self._keepalive = self._conf_int("stream_keepalive", minimum=1)
        self._max_chars = self._conf_int("max_chars", minimum=1)
        self._history = self._conf_int("history_messages", minimum=0)
        self._model_id = self._conf_str("model_id")
        self._model_display_name = self._conf_str("model_display_name")
        self._require_api_key = self._conf_bool("require_api_key")
        self._api_key = self._conf_str("api_key", allow_empty=True)

    async def initialize(self):
        self._owner_umo = await self.get_kv_data("owner_umo", None)
        self._owner_sender = await self.get_kv_data("owner_sender", None)
        if self._require_api_key:
            await self._ensure_api_key()
        logger.info(
            "call_me_by_api 已加载：model=%s timeout=%ss keepalive=%ss auth=%s routes=%s",
            self._model_id, self._timeout, self._keepalive,
            "on" if self._require_api_key else "off",
            ", ".join(self._route_prefixes()),
        )

    # ---------- HTTP 入口 ----------
    async def messages(self):
        denied = await self._authorize()
        if denied is not None:
            return denied
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return self._err("invalid_request_error", "请求体必须是 JSON 对象", 400)
        try:
            self._validate(payload)
        except RelayError as e:
            return self._err(e.error_type, e.message, e.status)

        model = payload["model"]
        msg_id = "msg_" + secrets.token_hex(12)
        approx_in = max(1, len(self._render(payload)) // 4)

        try:
            pending = await self._start(payload)
        except RelayError as e:
            return self._err(e.error_type, e.message, e.status)

        if payload.get("stream") is True:
            return stream_response(
                self._stream(pending, msg_id, model, approx_in),
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            text = await self._wait(pending)
        except RelayError as e:
            return self._err(e.error_type, e.message, e.status)
        return json_response(self._message(msg_id, model, text, approx_in))

    # ---------- 模型查询 ----------
    async def models(self):
        denied = await self._authorize()
        if denied is not None:
            return denied
        entry = self._model_entry()
        # 同时带上 OpenAI（object/created）和 Anthropic（type/created_at）字段，
        # 方便各类客户端解析模型列表。
        return json_response({
            "object": "list",
            "data": [entry],
            "has_more": False,
            "first_id": entry["id"],
            "last_id": entry["id"],
        })

    async def model(self, model_id: str):
        denied = await self._authorize()
        if denied is not None:
            return denied
        entry = self._model_entry()
        if model_id not in {entry["id"], "default"}:
            return self._err("not_found_error", f"模型不存在：{model_id}", 404)
        return json_response(entry)

    def _model_entry(self) -> dict[str, Any]:
        return {
            "type": "model",
            "object": "model",
            "id": self._model_id,
            "display_name": self._model_display_name,
            "created_at": MODEL_CREATED_AT,
            "created": MODEL_CREATED_TS,
            "owned_by": "call_me_by_api",
        }

    # ---------- 请求登记与等待 ----------
    async def _start(self, payload: dict[str, Any]) -> Pending:
        if not self._owner_umo:
            raise RelayError("尚未绑定私聊，请先发送 /apibind", "invalid_request_error", 400)

        req_id = self._new_req_id()
        fut = asyncio.get_running_loop().create_future()
        pending = Pending(req_id, fut, payload["model"], time.time())
        self._pending[req_id] = pending

        ok = await self.context.send_message(
            self._owner_umo,
            MessageChain().message(
                f"📨 API 调用 #{req_id}\n"
                f"model: {payload['model']}\n\n"
                f"{self._render(payload)}\n\n"
                f"回复：/apireply {req_id} <内容>"
            ),
        )
        if not ok:
            self._pending.pop(req_id, None)
            raise RelayError("无法把请求发送到私聊", "api_error", 500)
        return pending

    async def _wait(self, p: Pending) -> str:
        try:
            return await asyncio.wait_for(p.future, timeout=self._timeout)
        except asyncio.TimeoutError:
            # 用 400 而不是 5xx，避免 Anthropic SDK 自动重试导致重复转发
            raise RelayError("等待人工回复超时", "invalid_request_error", 400)
        finally:
            self._pending.pop(p.req_id, None)

    # ---------- 流式 ----------
    async def _stream(self, p: Pending, msg_id: str, model: str, approx_in: int):
        yield self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": approx_in, "output_tokens": 0},
            },
        })
        yield self._sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })

        deadline = time.monotonic() + self._timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RelayError("等待人工回复超时", "invalid_request_error", 400)

                done, _ = await asyncio.wait({p.future}, timeout=min(self._keepalive, left))
                if done:
                    if p.future.cancelled():
                        raise RelayError("请求已取消", "api_error", 500)
                    text = p.future.result()
                    break
                yield self._sse("ping", {"type": "ping"})

            for i in range(0, len(text), 200):
                yield self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text[i:i + 200]},
                })
            yield self._sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": max(1, len(text) // 4)},
            })
            yield self._sse("message_stop", {"type": "message_stop"})
        except RelayError as e:
            yield self._sse("error", {
                "type": "error",
                "error": {"type": e.error_type, "message": e.message},
            })
        finally:
            self._pending.pop(p.req_id, None)

    # ---------- 私聊绑定与回复 ----------
    @filter.command("apibind")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def apibind(self, event: AstrMessageEvent):
        if not event.is_private_chat():
            yield event.plain_result("请在私聊中执行 /apibind")
            return
        self._owner_umo = event.unified_msg_origin
        self._owner_sender = event.get_sender_id()
        await self.put_kv_data("owner_umo", self._owner_umo)
        await self.put_kv_data("owner_sender", self._owner_sender)
        yield event.plain_result("✅ 已绑定当前私聊，API 调用将转发到这里")

    @filter.command("apikey")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def apikey(self, event: AstrMessageEvent):
        if not event.is_private_chat():
            yield event.plain_result("请在私聊中执行 /apikey")
            return
        if self._owner_umo and event.unified_msg_origin != self._owner_umo:
            return

        if not self._require_api_key:
            yield event.plain_result(
                "插件层 API Key 校验已关闭（require_api_key=false）。\n"
                "请求仅由 AstrBot 平台鉴权保护，请使用带 plugin 权限的 AstrBot API Key。\n"
                "如需启用插件层校验，请在插件配置中开启 require_api_key 并设置 api_key。"
            )
            return

        parts = event.message_str.strip().split(maxsplit=1)
        action = parts[1].strip().lower() if len(parts) > 1 else ""
        if action == "reset":
            key = await self._ensure_api_key(force=True)
            yield event.plain_result(f"✅ 已重新生成 API Key：\n{key}")
            return

        key = await self._ensure_api_key()
        state = "已启用" if self._require_api_key else "已关闭（require_api_key=false）"
        yield event.plain_result(
            f"插件 API Key 校验{state}\n\n"
            f"{key}\n\n"
            f"Anthropic 客户端请求头：x-api-key: {key}\n"
            f"发送 /apikey reset 可重新生成"
        )

    @filter.command("apireply")
    async def apireply(self, event: AstrMessageEvent):
        if event.unified_msg_origin != self._owner_umo:
            return
        if self._owner_sender and event.get_sender_id() != self._owner_sender:
            return

        m = re.match(r"^\S+\s+(\w+)\s+(.*)$", event.message_str, re.S)
        if not m:
            yield event.plain_result("格式：/apireply <请求ID> <回复内容>")
            event.stop_event()
            return

        req_id, text = m.group(1), m.group(2).strip()
        pending = self._pending.get(req_id)
        if not pending or pending.future.done():
            yield event.plain_result(f"没有等待中的请求：{req_id}")
            event.stop_event()
            return

        pending.future.set_result(text)
        yield event.plain_result(f"✅ 已回复 {req_id}")
        event.stop_event()

    async def terminate(self):
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(RelayError("插件已卸载", "api_error", 500))
        self._pending.clear()

    # ---------- 工具方法 ----------
    def _validate(self, p: dict[str, Any]):
        if not isinstance(p.get("model"), str) or not p["model"].strip():
            raise RelayError("model 不能为空", "invalid_request_error", 400)
        mt = p.get("max_tokens")
        if not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0:
            raise RelayError("max_tokens 必须是正整数", "invalid_request_error", 400)
        msgs = p.get("messages")
        if not isinstance(msgs, list) or not msgs:
            raise RelayError("messages 不能为空", "invalid_request_error", 400)

    # ---------- 插件层鉴权 ----------
    async def _authorize(self):
        """校验插件自己的 API Key；通过返回 None，否则返回错误响应。"""
        if not self._require_api_key:
            return None
        expected = await self._ensure_api_key()
        provided = self._provided_api_key()
        if not provided or not secrets.compare_digest(provided, expected):
            return self._err("authentication_error", "无效的 API Key", 401)
        return None

    def _provided_api_key(self) -> str | None:
        # Anthropic SDK 默认使用 x-api-key，优先级最高；
        # Authorization 可能同时承载平台鉴权，所以放在后面读取。
        key = request.headers.get("x-api-key")
        if key and key.strip():
            return key.strip()
        auth = request.headers.get("authorization", "").strip()
        for prefix in ("Bearer ", "ApiKey "):
            if auth.startswith(prefix):
                return auth[len(prefix):].strip()
        for name in ("api_key", "key"):
            value = request.query.get(name)
            if value and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _generate_api_key() -> str:
        return "sk-" + secrets.token_urlsafe(32)

    async def _ensure_api_key(self, *, force: bool = False) -> str:
        """获取插件 API Key；未配置时生成随机密钥并写回配置。"""
        if self._api_key and not force:
            return self._api_key
        self._api_key = self._generate_api_key()
        self.config["api_key"] = self._api_key
        save = getattr(self.config, "save_config_async", None)
        if save is not None:
            try:
                await save()
            except Exception:
                logger.warning(
                    "自动生成的 API Key 写回配置失败，重启后会重新生成",
                    exc_info=True,
                )
        logger.info("插件 API Key：%s", self._api_key)
        return self._api_key

    def _new_req_id(self) -> str:
        while True:
            rid = secrets.token_hex(4)
            if rid not in self._pending:
                return rid

    def _render(self, payload: dict[str, Any]) -> str:
        limit = max(1, self._max_chars)
        # system/tools 只保留一小部分预算，避免把真正的对话内容挤掉。
        side_cap = max(200, limit // 5)

        sections: list[tuple[str, str]] = []
        system = payload.get("system")
        if system:
            sections.append((
                "system",
                self._clip(self._content_text(system), side_cap, "system 已截断"),
            ))
        # 工具/命令类请求不特殊处理，直接把工具定义和选择一并转给人看。
        tools_text = self._tools_text(payload.get("tools"))
        if tools_text:
            sections.append(("tools", self._clip(tools_text, side_cap, "tools 已截断")))
        if payload.get("tool_choice") is not None:
            sections.append((
                "tool_choice",
                self._clip(
                    json.dumps(payload["tool_choice"], ensure_ascii=False),
                    side_cap,
                    "tool_choice 已截断",
                ),
            ))

        messages = payload.get("messages") or []
        # history_messages=0 表示不转发历史，注意不能用 [-0:]（等价于整个列表）。
        if self._history > 0:
            messages = messages[-self._history:]
        else:
            messages = []
        for m in messages:
            role = {"user": "用户", "assistant": "助手"}.get(m.get("role"), m.get("role"))
            sections.append((str(role), self._content_text(m.get("content"))))

        # 从最新内容向前填充预算：保证最后一条消息优先保留。
        selected: list[str] = []
        remaining = limit
        omitted_older = False
        for idx in range(len(sections) - 1, -1, -1):
            label, body = sections[idx]
            block = f"【{label}】\n{body}"
            sep = 2 if selected else 0
            if len(block) + sep <= remaining:
                selected.insert(0, block)
                remaining -= len(block) + sep
                continue
            if not selected:
                # 最新一段本身就超长：保留开头，按剩余预算截断。
                keep = max(0, remaining - len(label) - 5)
                selected.insert(0, f"【{label}】\n{body[:keep]}…（已截断）")
                # 截断的是最新一段，只有它前面还有段落时才算省略了更早内容。
                omitted_older = idx > 0
            else:
                # 当前这段及其之前的段落都放不下了。
                omitted_older = True
            break

        if omitted_older:
            selected.insert(0, "…（更早内容已省略）")
        return "\n\n".join(selected)

    @staticmethod
    def _clip(text: str, cap: int, note: str) -> str:
        if len(text) <= cap:
            return text
        return text[:cap] + f"\n…（{note}）"

    def _tools_text(self, tools: Any) -> str:
        """把 tools 定义压缩成名字/描述列表，避免占用过多转发长度。"""
        if not isinstance(tools, list) or not tools:
            return ""
        lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                lines.append(f"- {tool}")
                continue
            name = tool.get("name", "?")
            desc = str(tool.get("description") or "").strip()
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        return "\n".join(lines)

    def _content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        out = []
        for block in content:
            if not isinstance(block, dict):
                out.append(str(block))
                continue
            t = block.get("type")
            if t == "text":
                out.append(block.get("text", ""))
            elif t == "image":
                out.append("[图片]")
            elif t == "document":
                out.append("[文档]")
            elif t == "tool_use":
                out.append(f"[tool_use {block.get('name')}] "
                           f"{json.dumps(block.get('input'), ensure_ascii=False)}")
            elif t == "tool_result":
                out.append("[tool_result]\n" + self._content_text(block.get("content")))
            else:
                out.append(f"[{t}]")
        return "\n".join(out)

    def _message(self, msg_id: str, model: str, text: str, approx_in: int) -> dict:
        return {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": approx_in,
                      "output_tokens": max(1, len(text) // 4)},
        }

    def _sse(self, event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _err(self, typ: str, message: str, status: int):
        return json_response(
            {
                "type": "error",
                "error": {"type": typ, "message": message},
                "request_id": "req_" + secrets.token_hex(8),
            },
            status_code=status,
        )

    def _conf_int(self, key: str, *, minimum: int) -> int:
        default = int(CONF_DEFAULTS[key])
        raw = self.config.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _conf_str(self, key: str, *, allow_empty: bool = False) -> str:
        raw = self.config.get(key, CONF_DEFAULTS[key])
        text = str(raw).strip() if raw is not None else ""
        if allow_empty:
            return text
        return text or str(CONF_DEFAULTS[key])

    def _conf_bool(self, key: str) -> bool:
        raw = self.config.get(key, CONF_DEFAULTS[key])
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
