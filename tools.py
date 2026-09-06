"""Agent tools for the QQ Official full adapter plugin.

Ports OpenClaw's ``qqbot_platform_api`` tool to AstrBot's native
function-calling tool system:

- :class:`QQBotPlatformApiTool` proxies arbitrary QQ Open Platform HTTP
  requests, injecting the access token automatically (backed by the running
  adapter's :class:`QQOfficialClient`).

Reminders use AstrBot's built-in ``future_task`` tool (core
``cron_manager``), so no dedicated remind tool is shipped here; the
``qqbot-remind`` skill documents the workflow.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .qqofficial_adapter import (
    QQOfficialAPIError,
    QQOfficialFullPlatformAdapter,
)

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _resolve_adapter_instance(context: AstrAgentContext) -> QQOfficialFullPlatformAdapter | None:
    """Find the QQ Official full adapter for the current event's session.

    Prefers the exact platform instance the event came from; falls back to the
    only/first running adapter when the agent runs outside a QQ session.

    Args:
        context: The agent context carrying event + star Context.

    Returns:
        A matching adapter or None.
    """
    try:
        platform_insts = list(context.context.platform_manager.platform_insts)
    except Exception:
        return None
    qq_insts = [
        inst
        for inst in platform_insts
        if isinstance(inst, QQOfficialFullPlatformAdapter)
    ]
    if not qq_insts:
        return None
    target_id = context.event.get_platform_id()
    for inst in qq_insts:
        if inst.meta().id == target_id:
            return inst
    return qq_insts[0]


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


@dataclass
class QQBotPlatformApiTool(FunctionTool[AstrAgentContext]):
    """QQ 开放平台统一 HTTP API 网关（自动鉴权）。"""

    name: str = "qqbot_platform_api"
    description: str = (
        "QQ 开放平台统一 HTTP API 网关，自动填充鉴权 Token。"
        "用于查询/操作频道、群、成员、公告、日程、帖子等 QQ 开放平台资源。"
        "常用接口：GET /users/@me/guilds | /guilds/{guild_id}/channels | "
        "/channels/{channel_id} | /v2/groups/{group_id}/members。"
        "更多接口与参数详情请阅读 qqbot-channel 技能。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "HTTP 请求方法。",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "API 路径（不含域名），占位符需替换为实际值。"
                        "示例：/users/@me/guilds、/guilds/123456/channels。"
                    ),
                },
                "body": {
                    "type": "object",
                    "description": "请求体 JSON（POST/PUT/PATCH 使用），GET/DELETE 省略。",
                },
                "query": {
                    "type": "object",
                    "description": (
                        "URL 查询参数键值对，值必须是字符串。"
                        "如 {\"limit\":\"100\",\"after\":\"0\"}。"
                    ),
                },
            },
            "required": ["method", "path"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        agent_ctx = context.context
        method = str(kwargs.get("method") or "").strip().upper()
        path = str(kwargs.get("path") or "").strip()
        body = kwargs.get("body")
        query = kwargs.get("query")

        if method not in _ALLOWED_METHODS:
            return _json({"error": f"不支持的 HTTP 方法: {method}"})
        path_error = _validate_api_path(path)
        if path_error:
            return _json({"error": path_error})
        if not isinstance(body, dict) and body is not None:
            return _json({"error": "body 必须是 JSON 对象"})
        if not isinstance(query, dict) and query is not None:
            return _json({"error": "query 必须是 JSON 对象"})
        query = {str(k): str(v) for k, v in (query or {}).items()} or None

        adapter = _resolve_adapter_instance(agent_ctx)
        if adapter is None:
            return _json(
                {"error": "未找到运行中的 QQ Official Full 适配器，无法代理 API 调用"}
            )
        try:
            data = await adapter.client.request(
                method, path, json_data=body, query_params=query
            )
        except QQOfficialAPIError as exc:
            return _json(
                {
                    "error": exc.message,
                    "status": exc.status_code,
                    "code": exc.biz_code,
                    "path": path,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc), "path": path})
        return _json(data)


def _validate_api_path(path: str) -> str | None:
    """Validate an API path (ported from OpenClaw platform.ts)."""
    if not path:
        return "path 为必填参数"
    if not path.startswith("/"):
        return "path 必须以 / 开头"
    if ".." in path or "//" in path:
        return "path 不允许包含 .. 或 //"
    if path != "/" and not re.fullmatch(r"/[a-zA-Z0-9\-._~:@!$&'()*+,;=/%]+", path):
        return "path 包含非法字符"
    return None


__all__ = ["QQBotPlatformApiTool"]
