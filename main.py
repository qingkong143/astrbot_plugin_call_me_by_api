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
ROUTE = f"/{PLUGIN_DIR}/v1/messages"
MODELS_ROUTE = f"/{PLUGIN_DIR}/v1/models"
MODEL_ROUTE = f"/{PLUGIN_DIR}/v1/models/<model_id>"

# Anthropic Models API 返回的模型创建时间（固定值，仅用于兼容客户端）。
MODEL_CREATED_AT = "2026-01-01T00:00:00Z"

# 配置默认值，需与 _conf_schema.json 保持一致。
CONF_DEFAULTS: dict[str, Any] = {
    "reply_timeout": 300,
    "stream_keepalive": 15,
    "max_chars": 4000,
    "history_messages": 6,
    "model_id": "qingkong143-flash-4.1-arbirated",
    "model_display_name": "qingkong143-flash-4.1-arbirated",
    "tool_call_error_message": "不支持工具调用",
    # 平台鉴权（require_plugin_scope）已经覆盖插件路由，插件层默认关闭。
    "require_api_key": False,
    "api_key": "",
}

# 出现这些内容块即视为工具调用请求。
TOOL_BLOCK_TYPES = frozenset({"tool_use", "tool_result"})


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


@register("call_me_by_api", "qingkong143", "将 AstrBot 私聊封装成 Anthropic 兼容 API", "1.1.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        # 保留 AstrBotConfig 本体，自动生成 API Key 时需要写回配置文件。
        self.config: dict[str, Any] = config if config is not None else {}
        self._pending: dict[str, Pending] = {}
        self._owner_umo: str | None = None
        self._owner_sender: str | None = None
        self._load_config()
        context.register_web_api(ROUTE, self.messages, ["POST"], "Anthropic 兼容接口")
        context.register_web_api(MODELS_ROUTE, self.models, ["GET"], "模型列表")
        context.register_web_api(MODEL_ROUTE, self.model, ["GET"], "模型详情")

    def _load_config(self) -> None:
        """从插件配置中读取运行时参数，非法值回退到默认值。"""
        self._timeout = self._conf_int("reply_timeout", minimum=1)
        self._keepalive = self._conf_int("stream_keepalive", minimum=1)
        self._max_chars = self._conf_int("max_chars", minimum=1)
        self._history = self._conf_int("history_messages", minimum=0)
        self._model_id = self._conf_str("model_id")
        self._model_display_name = self._conf_str("model_display_name")
        self._tool_error_message = self._conf_str("tool_call_error_message")
        self._require_api_key = self._conf_bool("require_api_key")
        self._api_key = self._conf_str("api_key", allow_empty=True)

    async def initialize(self):
        self._owner_umo = await self.get_kv_data("owner_umo", None)
        self._owner_sender = await self.get_kv_data("owner_sender", None)
        if self._require_api_key:
            await self._ensure_api_key()
        logger.info(
            "call_me_by_api 已加载：model=%s timeout=%ss keepalive=%ss auth=%s",
            self._model_id, self._timeout, self._keepalive,
            "on" if self._require_api_key else "off",
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
        return json_response({
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
            "id": self._model_id,
            "display_name": self._model_display_name,
            "created_at": MODEL_CREATED_AT,
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
        # 先拦截工具调用，保证这类请求总是得到明确的“不支持工具调用”错误。
        self._reject_tool_calls(p)
        if not isinstance(p.get("model"), str) or not p["model"].strip():
            raise RelayError("model 不能为空", "invalid_request_error", 400)
        mt = p.get("max_tokens")
        if not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0:
            raise RelayError("max_tokens 必须是正整数", "invalid_request_error", 400)
        msgs = p.get("messages")
        if not isinstance(msgs, list) or not msgs:
            raise RelayError("messages 不能为空", "invalid_request_error", 400)

    def _reject_tool_calls(self, p: dict[str, Any]) -> None:
        """本插件无法执行工具调用，收到任何工具请求直接返回错误。"""
        tools = p.get("tools")
        if isinstance(tools, list) and tools:
            self._raise_tool_error()
        if p.get("tool_choice") is not None:
            self._raise_tool_error()
        for m in p.get("messages") or []:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in TOOL_BLOCK_TYPES:
                    self._raise_tool_error()

    def _raise_tool_error(self):
        raise RelayError(self._tool_error_message, "invalid_request_error", 400)

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
        parts = []
        system = payload.get("system")
        if system:
            parts.append("【system】\n" + self._content_text(system))
        messages = payload.get("messages") or []
        # history_messages=0 表示不转发历史，注意不能用 [-0:]（等价于整个列表）。
        if self._history > 0:
            messages = messages[-self._history:]
        else:
            messages = []
        for m in messages:
            role = {"user": "用户", "assistant": "助手"}.get(m.get("role"), m.get("role"))
            parts.append(f"【{role}】\n{self._content_text(m.get('content'))}")
        text = "\n\n".join(parts)
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + "\n…（已截断）"
        return text

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
