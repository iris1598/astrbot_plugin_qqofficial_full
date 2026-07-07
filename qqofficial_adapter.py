from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import random
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import httpx
import websockets
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Json,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.webhook_server import FastAPIWebhookServer
from astrbot.core.utils.media_utils import MediaResolver, file_uri_to_path, is_file_uri
from astrbot.core.utils.webhook_utils import log_webhook_info

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_API_BASE_URL = "https://api.sgroup.qq.com"
SANDBOX_API_BASE_URL = "https://sandbox.api.sgroup.qq.com"
DEFAULT_GATEWAY_URL = "wss://api.sgroup.qq.com/websocket"

GATEWAY_CLOSE_AUTH_FAILED = 4004
GATEWAY_CLOSE_INVALID_SESSION = 4006
GATEWAY_CLOSE_SEQ_OUT_OF_RANGE = 4007
GATEWAY_CLOSE_RATE_LIMITED = 4008
GATEWAY_CLOSE_SESSION_TIMEOUT = 4009
GATEWAY_CLOSE_INSUFFICIENT_INTENTS = 4914
GATEWAY_CLOSE_DISALLOWED_INTENTS = 4915

GATEWAY_RATE_LIMIT_DELAY = 60.0

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11
OP_WEBHOOK_CALLBACK_ACK = 12
OP_WEBHOOK_VALIDATION = 13

INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26

INTENT_ALIASES = {
    "public_messages": INTENT_PUBLIC_GUILD_MESSAGES | INTENT_GROUP_AND_C2C,
    "public_guild_messages": INTENT_PUBLIC_GUILD_MESSAGES,
    "group_and_c2c": INTENT_GROUP_AND_C2C,
    "group_c2c": INTENT_GROUP_AND_C2C,
    "direct_message": INTENT_DIRECT_MESSAGE,
    "direct_messages": INTENT_DIRECT_MESSAGE,
    "guild_direct_message": INTENT_DIRECT_MESSAGE,
    "interaction": INTENT_INTERACTION,
    "interactions": INTENT_INTERACTION,
}

IMAGE_FILE_TYPE = 1
VIDEO_FILE_TYPE = 2
VOICE_FILE_TYPE = 3
FILE_FILE_TYPE = 4
MARKDOWN_NOT_ALLOWED_ERROR = "不允许发送原生 markdown"
WEBHOOK_SIGNATURE_HEADER = "X-Signature-Ed25519"
WEBHOOK_TIMESTAMP_HEADER = "X-Signature-Timestamp"
WEBHOOK_SEED_SIZE = 32
WEBHOOK_SIGNATURE_SIZE = 64
MD5_10M_SIZE = 10_002_432


class QQOfficialAPIError(Exception):
    """Error raised for QQ Open Platform API failures.

    Args:
        method: HTTP method used for the failed request.
        path: API path used for the failed request.
        status_code: HTTP status code, or 0 for network failures.
        message: Human-readable error message.
        biz_code: Optional QQ business error code.
    """

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        message: str,
        biz_code: int | None = None,
    ) -> None:
        super().__init__(f"{method} {path} failed ({status_code}): {message}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.message = message
        self.biz_code = biz_code


class QQGatewayClosed(Exception):
    """Gateway close event with a QQ close code.

    Args:
        code: WebSocket close code.
        reason: Optional close reason.
    """

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"QQ gateway closed: {code} {reason}".strip())
        self.code = code
        self.reason = reason


def _ed25519_seed(secret: str) -> bytes:
    """Build QQ webhook Ed25519 seed bytes from bot secret.

    Args:
        secret: QQ bot secret.

    Returns:
        A 32-byte seed.

    Raises:
        ValueError: If secret is empty.
    """
    if not secret:
        raise ValueError("QQ official bot secret is empty.")
    seed = secret.encode("utf-8")
    while len(seed) < WEBHOOK_SEED_SIZE:
        seed *= 2
    return seed[:WEBHOOK_SEED_SIZE]


def _sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Sign a QQ webhook payload.

    Args:
        secret: QQ bot secret.
        timestamp: Header timestamp string.
        body: Raw request body.

    Returns:
        Hex-encoded Ed25519 signature.
    """
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(_ed25519_seed(secret))
    return private_key.sign(timestamp.encode("utf-8") + body).hex()


def _verify_webhook_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
) -> bool:
    """Verify a QQ webhook Ed25519 signature.

    Args:
        secret: QQ bot secret.
        timestamp: Header timestamp string.
        signature: Hex-encoded signature header.
        body: Raw request body.

    Returns:
        Whether the signature is valid.
    """
    if not timestamp or not signature:
        return False
    try:
        signature_bytes = bytes.fromhex(signature)
    except ValueError:
        return False
    if len(signature_bytes) != WEBHOOK_SIGNATURE_SIZE or signature_bytes[63] & 224 != 0:
        return False
    try:
        public_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            _ed25519_seed(secret)
        ).public_key()
        public_key.verify(signature_bytes, timestamp.encode("utf-8") + body)
    except (InvalidSignature, ValueError):
        return False
    return True


def _normalize_url(url: str | None) -> str:
    """Normalize QQ attachment URLs.

    Args:
        url: Raw attachment URL.

    Returns:
        HTTP(S) URL or an empty string.
    """
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _attr(data: Any, key: str, default: Any = None) -> Any:
    """Read a field from dict-like or object-like payloads.

    Args:
        data: Payload object.
        key: Field name.
        default: Default value if the field is absent.

    Returns:
        Field value or default.
    """
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def _safe_filename(name: str | None) -> str:
    """Return a filesystem-safe display file name.

    Args:
        name: Raw file name.

    Returns:
        Sanitized file name.
    """
    if not name:
        return "file"
    basename = str(name).replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")
    for char in ':*?"<>|':
        basename = basename.replace(char, "_")
    basename = basename.strip()
    return basename if basename and basename not in {".", ".."} else "file"


def _parse_face_message(content: str) -> str:
    """Convert QQ face tags to readable placeholders.

    Args:
        content: Raw QQ message content.

    Returns:
        Message content with face tags replaced.
    """

    def replace_face(match: re.Match[str]) -> str:
        face_tag = match.group(0)
        ext_match = re.search(r'ext="([^"]*)"', face_tag)
        if ext_match:
            try:
                ext_data = json.loads(
                    base64.b64decode(ext_match.group(1)).decode("utf-8")
                )
                if text := ext_data.get("text"):
                    return f"[face:{text}]"
            except Exception:
                return "[face]"
        return "[face]"

    return re.sub(r"<faceType=\d+[^>]*>", replace_face, content)


async def _maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable.

    Args:
        value: Direct value or awaitable.

    Returns:
        Resolved value.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class QQOfficialClient:
    """Async QQ Open Platform REST client.

    Args:
        appid: QQ bot app ID.
        secret: QQ bot secret.
        is_sandbox: Whether to use sandbox API base URL.
        api_base_url: Optional API base URL override.
        token_url: Optional token URL override.
        timeout: Default HTTP timeout in seconds.
        chunked_upload_threshold: Local-file threshold for chunked uploads.
    """

    def __init__(
        self,
        appid: str,
        secret: str,
        *,
        is_sandbox: bool = False,
        api_base_url: str | None = None,
        token_url: str = TOKEN_URL,
        timeout: float = 30.0,
        chunked_upload_threshold: int = 20 * 1024 * 1024,
    ) -> None:
        self.appid = str(appid)
        self.secret = str(secret)
        self.api_base_url = (
            api_base_url
            or (SANDBOX_API_BASE_URL if is_sandbox else DEFAULT_API_BASE_URL)
        ).rstrip("/")
        self.token_url = token_url
        self.timeout = timeout
        self.chunked_upload_threshold = chunked_upload_threshold
        self._http = httpx.AsyncClient(timeout=timeout)
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._upload_cache: dict[tuple[str, str, str, int], tuple[dict, float]] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    def clear_token(self) -> None:
        """Clear cached access token."""
        self._access_token = None
        self._access_token_expires_at = 0.0

    async def get_access_token(self) -> str:
        """Get a cached or freshly fetched QQ access token.

        Returns:
            Access token string.

        Raises:
            QQOfficialAPIError: If the token endpoint fails.
        """
        now = time.time()
        if self._access_token and now < self._access_token_expires_at - 300:
            return self._access_token
        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._access_token_expires_at - 300:
                return self._access_token
            try:
                response = await self._http.post(
                    self.token_url,
                    json={"appId": self.appid, "clientSecret": self.secret},
                    timeout=self.timeout,
                )
            except Exception as exc:
                raise QQOfficialAPIError("POST", self.token_url, 0, str(exc)) from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise QQOfficialAPIError(
                    "POST", self.token_url, response.status_code, response.text
                ) from exc
            if response.status_code >= 400 or not data.get("access_token"):
                raise QQOfficialAPIError(
                    "POST",
                    self.token_url,
                    response.status_code,
                    data.get("message") or str(data),
                    data.get("code") or data.get("err_code"),
                )
            expires_in = data.get("expires_in") or 7200
            try:
                expires_seconds = int(expires_in)
            except (TypeError, ValueError):
                expires_seconds = 7200
            self._access_token = str(data["access_token"])
            self._access_token_expires_at = time.time() + max(expires_seconds, 0)
            return self._access_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict | None = None,
        query_params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
        auth: bool = True,
    ) -> Any:
        """Send a QQ REST API request.

        Args:
            method: HTTP method.
            path: Absolute URL or API path.
            json_data: JSON request body.
            query_params: URL query parameters.
            headers: Additional headers.
            timeout: Per-request timeout.
            auth: Whether to attach QQBot authorization.

        Returns:
            Parsed JSON object, or an empty dict for empty responses.

        Raises:
            QQOfficialAPIError: On HTTP, network, or JSON failures.
        """
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = f"QQBot {await self.get_access_token()}"
        request_headers.setdefault("Content-Type", "application/json")
        url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self.api_base_url}{path}"
        )
        try:
            response = await self._http.request(
                method,
                url,
                json=json_data,
                params=query_params,
                headers=request_headers,
                timeout=timeout or self.timeout,
            )
        except Exception as exc:
            raise QQOfficialAPIError(method, path, 0, str(exc)) from exc
        text = response.text
        if response.status_code >= 400:
            try:
                data = response.json()
                message = data.get("message") or data.get("error") or text
                biz_code = data.get("code") or data.get("err_code")
            except ValueError:
                message = text
                biz_code = None
            raise QQOfficialAPIError(
                method, path, response.status_code, message, biz_code
            )
        if not text:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise QQOfficialAPIError(method, path, response.status_code, text) from exc

    async def get_gateway(self) -> str:
        """Fetch the QQ WebSocket gateway URL.

        Returns:
            Gateway WebSocket URL.
        """
        data = await self.request("GET", "/gateway")
        return str(data.get("url") or DEFAULT_GATEWAY_URL)

    def _media_upload_path(self, scope: str, target_id: str) -> str:
        """Build media upload path.

        Args:
            scope: c2c or group scope.
            target_id: OpenID target.

        Returns:
            QQ REST path.
        """
        if scope in {"group", "groups"}:
            return f"/v2/groups/{target_id}/files"
        return f"/v2/users/{target_id}/files"

    def _message_path(self, scope: str, target_id: str) -> str:
        """Build v2 message path.

        Args:
            scope: c2c or group scope.
            target_id: OpenID target.

        Returns:
            QQ REST path.
        """
        if scope in {"group", "groups"}:
            return f"/v2/groups/{target_id}/messages"
        return f"/v2/users/{target_id}/messages"

    async def upload_media(
        self,
        scope: str,
        target_id: str,
        file_type: int,
        source: str,
        *,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> dict:
        """Upload media to a C2C or group target.

        Args:
            scope: c2c or group scope.
            target_id: OpenID target.
            file_type: QQ media file type.
            source: Local path, URL, data URI, or base64 source.
            file_name: Optional display file name.
            srv_send_msg: Whether QQ should send immediately after upload.

        Returns:
            Upload response dict.
        """
        local_path = self._source_to_local_path(source)
        if local_path and os.path.getsize(local_path) >= self.chunked_upload_threshold:
            return await self._upload_media_chunked(
                scope,
                target_id,
                file_type,
                local_path,
                file_name=file_name,
            )

        file_data: str | None = None
        url: str | None = None
        if local_path:
            raw = Path(local_path).read_bytes()
            file_data = base64.b64encode(raw).decode("utf-8")
        elif source.startswith("base64://"):
            file_data = source.removeprefix("base64://")
        elif source.startswith("data:") and "," in source:
            file_data = source.split(",", 1)[1]
        elif source.startswith(("http://", "https://")):
            url = source
        else:
            file_data = source

        cache_key = None
        if file_data:
            cache_key = (
                hashlib.sha256(file_data.encode("utf-8")).hexdigest(),
                scope,
                target_id,
                file_type,
            )
            cached = self._upload_cache.get(cache_key)
            if cached and time.time() < cached[1]:
                return dict(cached[0])

        payload: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": srv_send_msg,
        }
        if file_data:
            payload["file_data"] = file_data
        if url:
            payload["url"] = url
        if file_type == FILE_FILE_TYPE and file_name:
            payload["file_name"] = _safe_filename(file_name)

        result = await self.request(
            "POST",
            self._media_upload_path(scope, target_id),
            json_data=payload,
            timeout=120.0,
        )
        if cache_key and isinstance(result, dict) and result.get("file_info"):
            ttl = int(result.get("ttl") or 0)
            if ttl > 0:
                self._upload_cache[cache_key] = (dict(result), time.time() + ttl)
        return cast(dict, result)

    def _source_to_local_path(self, source: str) -> str | None:
        """Return a local path for path-like sources.

        Args:
            source: Raw media source.

        Returns:
            Existing local path, or None.
        """
        if is_file_uri(source):
            path = file_uri_to_path(source)
            return path if os.path.exists(path) else None
        try:
            return source if os.path.exists(source) else None
        except OSError:
            return None

    async def _upload_media_chunked(
        self,
        scope: str,
        target_id: str,
        file_type: int,
        file_path: str,
        *,
        file_name: str | None = None,
    ) -> dict:
        """Upload a local file through QQ chunked upload APIs.

        Args:
            scope: c2c or group scope.
            target_id: OpenID target.
            file_type: QQ media file type.
            file_path: Existing local file path.
            file_name: Optional display file name.

        Returns:
            Upload response dict.
        """
        data = Path(file_path).read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        cache_key = (md5, scope, target_id, file_type)
        cached = self._upload_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return dict(cached[0])

        sha1 = hashlib.sha1(data).hexdigest()
        md5_10m = hashlib.md5(data[:MD5_10M_SIZE]).hexdigest()
        prefix = "/v2/groups" if scope in {"group", "groups"} else "/v2/users"
        prepare_payload: dict[str, Any] = {
            "file_type": file_type,
            "file_size": len(data),
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        if file_name or file_type == FILE_FILE_TYPE:
            prepare_payload["file_name"] = _safe_filename(
                file_name or Path(file_path).name
            )
        prepared = await self.request(
            "POST",
            f"{prefix}/{target_id}/upload_prepare",
            json_data=prepare_payload,
            timeout=120.0,
        )
        upload_id = prepared["upload_id"]
        block_size = int(prepared.get("block_size") or len(data) or 1)
        parts = list(prepared.get("parts") or [])
        concurrency = max(1, min(int(prepared.get("concurrency") or 1), 10))
        semaphore = asyncio.Semaphore(concurrency)

        async def upload_part(part: dict) -> None:
            index = int(part["index"])
            start = (index - 1) * block_size
            chunk = data[start : start + block_size]
            async with semaphore:
                response = await self._http.put(
                    part["presigned_url"],
                    content=chunk,
                    headers={"Content-Length": str(len(chunk))},
                    timeout=300.0,
                )
                response.raise_for_status()
                part_md5 = hashlib.md5(chunk).hexdigest()
                attempts = 0
                while True:
                    attempts += 1
                    try:
                        await self.request(
                            "POST",
                            f"{prefix}/{target_id}/upload_part_finish",
                            json_data={
                                "upload_id": upload_id,
                                "part_index": index,
                                "block_size": len(chunk),
                                "md5": part_md5,
                            },
                            timeout=120.0,
                        )
                        return
                    except QQOfficialAPIError as exc:
                        if attempts >= 3 or exc.status_code < 500:
                            raise
                        await asyncio.sleep(0.1 * attempts)

        await asyncio.gather(*(upload_part(part) for part in parts))
        result = await self.request(
            "POST",
            f"{prefix}/{target_id}/files",
            json_data={"upload_id": upload_id},
            timeout=120.0,
        )
        if isinstance(result, dict) and result.get("file_info"):
            ttl = int(result.get("ttl") or 0)
            if ttl > 0:
                self._upload_cache[cache_key] = (dict(result), time.time() + ttl)
        return cast(dict, result)

    async def send_v2_message(
        self,
        scope: str,
        target_id: str,
        payload: dict,
    ) -> dict:
        """Send a v2 C2C or group message.

        Args:
            scope: c2c or group scope.
            target_id: OpenID target.
            payload: QQ API payload.

        Returns:
            Message response dict.
        """
        return cast(
            dict,
            await self.request(
                "POST", self._message_path(scope, target_id), json_data=payload
            ),
        )

    async def send_channel_message(self, channel_id: str, payload: dict) -> dict:
        """Send a guild channel message.

        Args:
            channel_id: QQ channel ID.
            payload: QQ API payload.

        Returns:
            Message response dict.
        """
        return cast(
            dict,
            await self.request(
                "POST", f"/channels/{channel_id}/messages", json_data=payload
            ),
        )

    async def send_dm_message(self, guild_id: str, payload: dict) -> dict:
        """Send a guild direct message.

        Args:
            guild_id: QQ guild ID.
            payload: QQ API payload.

        Returns:
            Message response dict.
        """
        return cast(
            dict,
            await self.request("POST", f"/dms/{guild_id}/messages", json_data=payload),
        )

    async def send_stream_message(self, openid: str, payload: dict) -> dict:
        """Send a C2C stream message chunk.

        Args:
            openid: User openid.
            payload: QQ stream payload.

        Returns:
            Message response dict.
        """
        return cast(
            dict,
            await self.request(
                "POST", f"/v2/users/{openid}/stream_messages", json_data=payload
            ),
        )

    async def acknowledge_interaction(self, interaction_id: str, code: int = 0) -> Any:
        """Acknowledge a QQ interaction callback.

        Args:
            interaction_id: Interaction ID.
            code: QQ interaction acknowledgement code.

        Returns:
            API response.
        """
        return await self.request(
            "PUT", f"/interactions/{interaction_id}", json_data={"code": code}
        )

    async def me(self) -> Any:
        """Return current bot user information.

        Returns:
            API response.
        """
        return await self.request("GET", "/users/@me")

    async def list_guilds(self, **query_params: Any) -> Any:
        """List guilds visible to the bot.

        Args:
            **query_params: Optional QQ API query parameters.

        Returns:
            API response.
        """
        return await self.request(
            "GET", "/users/@me/guilds", query_params=query_params or None
        )

    async def get_guild(self, guild_id: str) -> Any:
        """Get guild details.

        Args:
            guild_id: QQ guild ID.

        Returns:
            API response.
        """
        return await self.request("GET", f"/guilds/{guild_id}")

    async def list_channels(self, guild_id: str) -> Any:
        """List channels in a guild.

        Args:
            guild_id: QQ guild ID.

        Returns:
            API response.
        """
        return await self.request("GET", f"/guilds/{guild_id}/channels")

    async def get_channel(self, channel_id: str) -> Any:
        """Get channel details.

        Args:
            channel_id: QQ channel ID.

        Returns:
            API response.
        """
        return await self.request("GET", f"/channels/{channel_id}")

    async def list_members(self, guild_id: str, **query_params: Any) -> Any:
        """List guild members.

        Args:
            guild_id: QQ guild ID.
            **query_params: Optional QQ API query parameters.

        Returns:
            API response.
        """
        return await self.request(
            "GET", f"/guilds/{guild_id}/members", query_params=query_params or None
        )

    async def get_member(self, guild_id: str, user_id: str) -> Any:
        """Get one guild member.

        Args:
            guild_id: QQ guild ID.
            user_id: QQ user ID.

        Returns:
            API response.
        """
        return await self.request("GET", f"/guilds/{guild_id}/members/{user_id}")

    async def delete_group_message(
        self, group_openid: str, message_id: str, *, hidetip: bool = False
    ) -> Any:
        """Delete a group message.

        Args:
            group_openid: QQ group openid.
            message_id: QQ message ID.
            hidetip: Whether to hide QQ deletion tip.

        Returns:
            API response.
        """
        return await self.request(
            "DELETE",
            f"/v2/groups/{group_openid}/messages/{message_id}",
            query_params={"hidetip": "true" if hidetip else "false"},
        )

    async def delete_c2c_message(
        self, openid: str, message_id: str, *, hidetip: bool = False
    ) -> Any:
        """Delete a C2C message.

        Args:
            openid: User openid.
            message_id: QQ message ID.
            hidetip: Whether to hide QQ deletion tip.

        Returns:
            API response.
        """
        return await self.request(
            "DELETE",
            f"/v2/users/{openid}/messages/{message_id}",
            query_params={"hidetip": "true" if hidetip else "false"},
        )


class QQOfficialFullMessageEvent(AstrMessageEvent):
    """AstrBot message event for QQ Official full adapters."""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: QQOfficialFullPlatformAdapter,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        """Send a message as a reply to the current QQ event.

        Args:
            message: AstrBot message chain.
        """
        await self.adapter._send_to_session(
            self.session,
            message,
            reply_message_id=self.message_obj.message_id,
            from_event=True,
        )
        await super().send(message)

    async def send_typing(self) -> None:
        """Send QQ C2C input notification when the scene supports it."""
        scene = self.get_extra("qq_scene") or self.adapter.session_scene(
            self.session_id
        )
        if scene != "c2c":
            return
        await self.adapter.client.send_v2_message(
            "c2c",
            self.session_id,
            {
                "msg_type": 6,
                "input_notify": {"input_type": 1, "input_second": 60},
                "msg_seq": random.randint(1, 65535),
            },
        )

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """Send C2C streaming chunks, falling back to normal sends elsewhere.

        Args:
            generator: Stream of AstrBot message chains.
            use_fallback: Unused compatibility flag.
        """
        await super().send_streaming(generator, use_fallback)
        scene = self.get_extra("qq_scene") or self.adapter.session_scene(
            self.session_id
        )
        if scene != "c2c":
            buffer = MessageChain()
            async for chain in generator:
                buffer.chain.extend(chain.chain)
            if buffer.chain:
                await self.send(buffer)
            return

        stream_msg_id = None
        index = 0
        async for chain in generator:
            (
                text,
                media,
                reply_id,
                keyboard,
                raw_payload,
            ) = await self.adapter._parse_chain(chain, self.session, upload_media=False)
            if media or raw_payload or reply_id or keyboard:
                await self.send(chain)
                continue
            if not text:
                continue
            payload = {
                "input_mode": 1,
                "input_state": 1,
                "content_type": 1,
                "content_raw": text,
                "event_id": self.message_obj.message_id,
                "msg_id": self.message_obj.message_id,
                "msg_seq": random.randint(1, 65535),
                "index": index,
            }
            if stream_msg_id:
                payload["stream_msg_id"] = stream_msg_id
            ret = await self.adapter.client.send_stream_message(
                self.session_id, payload
            )
            stream_msg_id = ret.get("id") or ret.get("stream_msg_id") or stream_msg_id
            index += 1
        await self.adapter.client.send_stream_message(
            self.session_id,
            {
                "input_mode": 1,
                "input_state": 2,
                "content_type": 1,
                "content_raw": "\n",
                "event_id": self.message_obj.message_id,
                "msg_id": self.message_obj.message_id,
                "msg_seq": random.randint(1, 65535),
                "index": index,
                **({"stream_msg_id": stream_msg_id} if stream_msg_id else {}),
            },
        )

    async def get_group(
        self, group_id: str | None = None, **kwargs: Any
    ) -> Group | None:
        """Return basic QQ group metadata when available.

        Args:
            group_id: Optional group id override.
            **kwargs: Reserved compatibility parameters.

        Returns:
            AstrBot group object or None.
        """
        target = group_id or self.get_group_id()
        if not target:
            return None
        return Group(group_id=target)


DEFAULT_CONFIG = {
    "id": "qq_official_full",
    "appid": "",
    "secret": "",
    "is_sandbox": False,
    "use_markdown": True,
    "intents": [
        "public_messages",
        "public_guild_messages",
        "direct_message",
        "interaction",
    ],
    "intent_mask": "",
    "api_base_url": "",
    "gateway_url": "",
    "chunked_upload_threshold": 20971520,
    "session_store_path": "",
}

DEFAULT_WEBHOOK_CONFIG = {
    **DEFAULT_CONFIG,
    "id": "qq_official_full_webhook",
    "unified_webhook_mode": True,
    "webhook_uuid": "",
    "verify_webhook_signature": True,
    "webhook_timestamp_tolerance_seconds": 300,
    "callback_server_host": "0.0.0.0",
    "port": 6197,
}

CONFIG_METADATA = {
    "appid": {"type": "string", "description": "QQ bot AppID", "default": ""},
    "secret": {"type": "string", "description": "QQ bot secret", "default": ""},
    "is_sandbox": {"type": "bool", "description": "Use sandbox API", "default": False},
    "use_markdown": {
        "type": "bool",
        "description": "Prefer native markdown messages when possible",
        "default": True,
    },
    "intents": {
        "type": "list",
        "description": "Gateway intent aliases",
        "default": DEFAULT_CONFIG["intents"],
    },
    "intent_mask": {
        "type": "string",
        "description": "Raw gateway intent mask override",
        "default": "",
    },
}


@register_platform_adapter(
    "qq_official_full",
    "QQ Official full adapter",
    default_config_tmpl=dict(DEFAULT_CONFIG),
    adapter_display_name="QQ Official Full",
    support_streaming_message=True,
    config_metadata=CONFIG_METADATA,
)
class QQOfficialFullPlatformAdapter(Platform):
    """AstrBot adapter backed by QQ Official REST and WebSocket APIs."""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.appid = str(platform_config.get("appid") or "")
        self.secret = str(platform_config.get("secret") or "")
        self.use_markdown = bool(platform_config.get("use_markdown", True))
        self.intents = self._resolve_intents(platform_config)
        self.gateway_url_override = str(platform_config.get("gateway_url") or "")
        self.client = QQOfficialClient(
            self.appid,
            self.secret,
            is_sandbox=bool(platform_config.get("is_sandbox", False)),
            api_base_url=str(platform_config.get("api_base_url") or "") or None,
            chunked_upload_threshold=int(
                platform_config.get("chunked_upload_threshold") or 20 * 1024 * 1024
            ),
        )
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_store_path = self._resolve_session_store_path(platform_config)
        self._load_sessions()
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._gateway_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()
        self._pending_event_extras: dict[str, dict[str, Any]] = {}

    def _resolve_session_store_path(self, platform_config: dict) -> Path:
        """Resolve session store path from config.

        Args:
            platform_config: Platform configuration.

        Returns:
            JSON session store path.
        """
        configured = platform_config.get("_session_store_path") or platform_config.get(
            "session_store_path"
        )
        if configured:
            return Path(str(configured))
        return Path("data") / "plugin_data" / "qqofficial_full_sessions.json"

    def _load_sessions(self) -> None:
        """Load remembered QQ session metadata from disk."""
        try:
            if self._session_store_path.exists():
                data = json.loads(self._session_store_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._sessions = {
                        str(key): value
                        for key, value in data.items()
                        if isinstance(value, dict)
                    }
        except Exception as exc:
            logger.warning("[QQOfficialFull] Failed to load session store: %s", exc)

    def _save_sessions(self) -> None:
        """Persist remembered QQ session metadata to disk."""
        try:
            self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
            self._session_store_path.write_text(
                json.dumps(self._sessions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("[QQOfficialFull] Failed to save session store: %s", exc)

    def _resolve_intents(self, platform_config: dict) -> int:
        """Resolve gateway intent mask from config.

        Args:
            platform_config: Platform configuration.

        Returns:
            Integer intent mask.
        """
        raw_mask = str(platform_config.get("intent_mask") or "").strip()
        if raw_mask:
            return int(raw_mask, 0)
        mask = 0
        for item in platform_config.get("intents") or []:
            mask |= INTENT_ALIASES.get(str(item), 0)
        return mask or (
            INTENT_PUBLIC_GUILD_MESSAGES
            | INTENT_DIRECT_MESSAGE
            | INTENT_GROUP_AND_C2C
            | INTENT_INTERACTION
        )

    def meta(self) -> PlatformMetadata:
        """Return platform metadata.

        Returns:
            AstrBot platform metadata.
        """
        return PlatformMetadata(
            name="qq_official_full",
            description="QQ Official full adapter",
            id=cast(str, self.config.get("id")),
            support_streaming_message=True,
            support_proactive_message=True,
        )

    def get_client(self) -> QQOfficialClient:
        """Return the QQ REST client.

        Returns:
            QQOfficialClient instance.
        """
        return self.client

    def create_event(self, message: AstrBotMessage) -> QQOfficialFullMessageEvent:
        """Create an AstrBot QQ message event.

        Args:
            message: AstrBot message object.

        Returns:
            QQ Official full message event.
        """
        event = QQOfficialFullMessageEvent(
            message.message_str,
            message,
            self.meta(),
            message.session_id,
            self,
        )
        extras = self._pending_event_extras.pop(message.message_id, {})
        for key, value in extras.items():
            event.set_extra(key, value)
        return event

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        """Send a proactive message by AstrBot session.

        Args:
            session: AstrBot message session.
            message_chain: AstrBot message chain.
        """
        await self._send_to_session(session, message_chain, from_event=False)
        await Platform.send_by_session(self, session, message_chain)

    async def _send_to_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
        *,
        reply_message_id: str | None = None,
        from_event: bool = False,
    ) -> None:
        """Send a message chain to a remembered QQ session.

        Args:
            session: AstrBot message session.
            message_chain: AstrBot message chain.
            reply_message_id: Current event message id for reply sends.
            from_event: Whether this is an event reply instead of proactive send.
        """
        chunks = self._split_chain_by_media(message_chain)
        for chunk in chunks:
            await self._send_one_chunk(
                session,
                chunk,
                reply_message_id=reply_message_id,
                from_event=from_event,
            )

    async def _send_one_chunk(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
        *,
        reply_message_id: str | None,
        from_event: bool,
    ) -> None:
        """Send one media-compatible QQ message chunk.

        Args:
            session: AstrBot message session.
            message_chain: Chunked message chain.
            reply_message_id: Current event message id for reply sends.
            from_event: Whether this is an event reply.
        """
        session_info = self._sessions.get(session.session_id, {})
        scene = session_info.get("scene") or (
            "group" if session.message_type == MessageType.GROUP_MESSAGE else "c2c"
        )
        target_id = str(session_info.get("target_id") or session.session_id)
        text, media, reply_id, keyboard, raw_payload = await self._parse_chain(
            message_chain,
            session,
            scene=scene,
            target_id=target_id,
            upload_media=True,
        )
        if not text and not media and not raw_payload:
            return

        payload = await self._build_send_payload(
            message_chain,
            text,
            media,
            reply_id or reply_message_id,
            keyboard,
            raw_payload,
            session_info,
            scene,
            from_event,
        )
        ret = await self._send_payload_with_fallback(scene, target_id, payload, text)
        sent_id = self._extract_message_id(ret)
        if sent_id:
            self.remember_session(
                session.session_id,
                scene=scene,
                target_id=target_id,
                message_id=sent_id,
            )

    async def _build_send_payload(
        self,
        message_chain: MessageChain,
        text: str,
        media: dict | None,
        reply_id: str | None,
        keyboard: dict | None,
        raw_payload: dict | None,
        session_info: dict,
        scene: str,
        from_event: bool,
    ) -> dict:
        """Build QQ send payload from normalized message fields.

        Args:
            message_chain: Source message chain.
            text: Plain rendered text.
            media: Optional uploaded media response.
            reply_id: Optional referenced message id.
            keyboard: Optional QQ keyboard payload.
            raw_payload: Raw payload fragments from Json components.
            session_info: Remembered session metadata.
            scene: QQ scene name.
            from_event: Whether this is an event reply.

        Returns:
            QQ API payload.
        """
        payload: dict[str, Any] = dict(raw_payload or {})
        use_markdown = (
            self.use_markdown
            if message_chain.use_markdown_ is None
            else bool(message_chain.use_markdown_)
        )
        if media:
            payload.update(
                {
                    "content": text or None,
                    "msg_type": 7,
                    "media": {"file_info": media.get("file_info")},
                }
            )
        elif scene in {"channel", "dm"}:
            payload.setdefault("content", text)
        elif use_markdown:
            payload.update({"markdown": {"content": text}, "msg_type": 2})
        else:
            payload.update({"content": text, "msg_type": 0})

        if scene in {"group", "c2c"}:
            payload.setdefault("msg_seq", random.randint(1, 65535))
        if reply_id and (from_event or scene in {"channel", "dm"}):
            payload.setdefault("msg_id", reply_id)
        if reply_id:
            payload.setdefault("message_reference", {"message_id": reply_id})
        if keyboard:
            payload["keyboard"] = keyboard
        if not from_event and scene in {"group", "c2c"}:
            payload.pop("msg_id", None)
        elif not from_event and session_info.get("message_id"):
            payload.setdefault("msg_id", session_info["message_id"])
        return payload

    async def _send_payload_with_fallback(
        self, scene: str, target_id: str, payload: dict, plain_text: str
    ) -> dict:
        """Send payload and retry markdown rejections as plain text.

        Args:
            scene: QQ scene name.
            target_id: QQ target id.
            payload: QQ API payload.
            plain_text: Plain fallback text.

        Returns:
            Message response dict.
        """
        try:
            return await self._send_payload(scene, target_id, payload)
        except QQOfficialAPIError as exc:
            if MARKDOWN_NOT_ALLOWED_ERROR not in str(exc) or "markdown" not in payload:
                raise
            fallback = dict(payload)
            fallback.pop("markdown", None)
            fallback["content"] = plain_text
            if fallback.get("msg_type") == 2:
                fallback["msg_type"] = 0
            return await self._send_payload(scene, target_id, fallback)

    async def _send_payload(self, scene: str, target_id: str, payload: dict) -> dict:
        """Dispatch payload to the correct QQ send endpoint.

        Args:
            scene: QQ scene name.
            target_id: QQ target id.
            payload: QQ API payload.

        Returns:
            Message response dict.
        """
        if scene == "group":
            return await self.client.send_v2_message("group", target_id, payload)
        if scene == "channel":
            channel_payload = dict(payload)
            channel_payload.pop("msg_type", None)
            channel_payload.pop("msg_seq", None)
            return await self.client.send_channel_message(target_id, channel_payload)
        if scene == "dm":
            dm_payload = dict(payload)
            dm_payload.pop("msg_type", None)
            dm_payload.pop("msg_seq", None)
            return await self.client.send_dm_message(target_id, dm_payload)
        return await self.client.send_v2_message("c2c", target_id, payload)

    async def _parse_chain(
        self,
        message_chain: MessageChain,
        session: MessageSesion,
        *,
        scene: str | None = None,
        target_id: str | None = None,
        upload_media: bool = True,
    ) -> tuple[str, dict | None, str | None, dict | None, dict | None]:
        """Convert AstrBot components to QQ text/media payload fragments.

        Args:
            message_chain: AstrBot message chain.
            session: AstrBot session.
            scene: Optional QQ scene.
            target_id: Optional QQ target id.
            upload_media: Whether media should be uploaded.

        Returns:
            Plain text, upload response, reply id, keyboard, raw payload tuple.
        """
        text_parts: list[str] = []
        media: dict | None = None
        reply_id: str | None = None
        keyboard: dict | None = None
        raw_payload: dict[str, Any] = {}
        scene = scene or self.session_scene(session.session_id)
        target_id = target_id or session.session_id
        for component in message_chain.chain:
            if isinstance(component, Plain):
                text_parts.append(component.text)
            elif isinstance(component, At):
                if str(component.qq) == "all" or isinstance(component, AtAll):
                    text_parts.append("@all ")
                else:
                    text_parts.append(f"<@{component.qq}> ")
            elif isinstance(component, Reply):
                reply_id = str(component.id)
            elif isinstance(component, Json):
                data = dict(component.data)
                if "keyboard" in data:
                    keyboard = cast(dict, data.pop("keyboard"))
                raw_payload.update(data)
            elif isinstance(component, Image | Record | Video | File) and not media:
                if upload_media:
                    media = await self._upload_component_media(
                        scene or "c2c", target_id, component
                    )
            else:
                logger.debug("[QQOfficialFull] Ignored component: %s", component.type)
        return "".join(text_parts), media, reply_id, keyboard, raw_payload or None

    async def _upload_component_media(
        self, scene: str, target_id: str, component: BaseMessageComponent
    ) -> dict | None:
        """Upload one AstrBot media component for a QQ message.

        Args:
            scene: QQ scene name.
            target_id: QQ target id.
            component: AstrBot media component.

        Returns:
            Upload response dict, or None if unsupported.
        """
        scope = "group" if scene == "group" else "c2c"
        if scene not in {"group", "c2c"}:
            return None
        if isinstance(component, Image):
            source = component.file or component.url or ""
            return await self.client.upload_media(
                scope, target_id, IMAGE_FILE_TYPE, source
            )
        if isinstance(component, Record):
            source = component.url or component.file or ""
            try:
                source = await MediaResolver(
                    source,
                    media_type="audio",
                    default_suffix=".wav",
                ).to_path(target_format="tencent_silk")
            except Exception as exc:
                logger.warning("[QQOfficialFull] Audio conversion failed: %s", exc)
            return await self.client.upload_media(
                scope, target_id, VOICE_FILE_TYPE, source
            )
        if isinstance(component, Video):
            source = await component.convert_to_file_path()
            return await self.client.upload_media(
                scope, target_id, VIDEO_FILE_TYPE, source
            )
        if isinstance(component, File):
            source = await component.get_file(allow_return_url=True)
            return await self.client.upload_media(
                scope,
                target_id,
                FILE_FILE_TYPE,
                source,
                file_name=component.name,
            )
        return None

    def _split_chain_by_media(self, message_chain: MessageChain) -> list[MessageChain]:
        """Split chains so each QQ message has at most one media component.

        Args:
            message_chain: Source message chain.

        Returns:
            One or more sendable message chains.
        """
        chunks: list[MessageChain] = []
        current: list[BaseMessageComponent] = []
        has_media = False
        for component in message_chain.chain:
            is_media = isinstance(component, Image | Record | Video | File)
            if is_media and has_media:
                chunks.append(message_chain.derive(list(current)))
                current = []
                has_media = False
            current.append(component)
            has_media = has_media or is_media
        if current or not message_chain.chain:
            chunks.append(message_chain.derive(list(current)))
        return chunks

    def remember_session(
        self,
        session_id: str,
        *,
        scene: str,
        target_id: str | None = None,
        message_id: str | None = None,
        **extra: Any,
    ) -> None:
        """Remember QQ scene metadata for proactive sends.

        Args:
            session_id: AstrBot session id.
            scene: QQ scene name.
            target_id: QQ API target id.
            message_id: Last QQ message id.
            **extra: Additional session metadata.
        """
        if not session_id:
            return
        item = self._sessions.setdefault(session_id, {})
        item["scene"] = scene
        item["target_id"] = target_id or session_id
        if message_id:
            item["message_id"] = message_id
        item.update(extra)
        self._save_sessions()

    def remember_session_message_id(self, session_id: str, message_id: str) -> None:
        """Remember the last QQ message id for a session.

        Args:
            session_id: AstrBot session id.
            message_id: QQ message id.
        """
        if not session_id or not message_id:
            return
        item = self._sessions.setdefault(session_id, {"target_id": session_id})
        item["message_id"] = message_id
        self._save_sessions()

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        """Remember QQ scene for a session.

        Args:
            session_id: AstrBot session id.
            scene: QQ scene name.
        """
        if not session_id or not scene:
            return
        item = self._sessions.setdefault(session_id, {"target_id": session_id})
        item["scene"] = scene
        self._save_sessions()

    def session_scene(self, session_id: str) -> str | None:
        """Return remembered QQ scene.

        Args:
            session_id: AstrBot session id.

        Returns:
            QQ scene name or None.
        """
        return cast(str | None, self._sessions.get(session_id, {}).get("scene"))

    def _extract_message_id(self, ret: Any) -> str | None:
        """Extract message id from a QQ API response.

        Args:
            ret: API response object.

        Returns:
            Message ID string or None.
        """
        if isinstance(ret, dict):
            value = ret.get("id") or ret.get("message_id")
            return str(value) if value else None
        value = getattr(ret, "id", None) or getattr(ret, "message_id", None)
        return str(value) if value else None

    async def _parse_message_event(
        self, event_type: str, payload: dict, event_id: str | None = None
    ) -> AstrBotMessage:
        """Convert a QQ dispatch payload to AstrBotMessage.

        Args:
            event_type: QQ gateway/webhook event type.
            payload: QQ event payload.
            event_id: QQ dispatch event id.

        Returns:
            Normalized AstrBot message.
        """
        abm = AstrBotMessage()
        abm.raw_message = payload
        abm.timestamp = int(time.time())
        abm.message_id = str(payload.get("id") or event_id or "")
        content = _parse_face_message(str(payload.get("content") or ""))
        components: list[BaseMessageComponent] = []
        qq_scene = "unknown"

        if event_type in {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}:
            group_id = str(payload.get("group_openid") or payload.get("group_id") or "")
            author = payload.get("author") or {}
            sender_id = str(
                author.get("member_openid")
                or author.get("user_openid")
                or author.get("id")
                or ""
            )
            bot_mentions = [
                item
                for item in payload.get("mentions") or []
                if item.get("is_you") and item.get("id")
            ]
            for mention in bot_mentions:
                mention_id = str(mention["id"])
                content = content.replace(f"<@!{mention_id}>", "").replace(
                    f"<@{mention_id}>", ""
                )
            if bot_mentions:
                components.append(
                    At(
                        qq=str(bot_mentions[0]["id"]),
                        name=str(bot_mentions[0].get("username") or ""),
                    )
                )
                abm.self_id = str(bot_mentions[0]["id"])
            elif event_type == "GROUP_AT_MESSAGE_CREATE":
                components.append(At(qq="qq_official_full"))
                abm.self_id = "qq_official_full"
            else:
                abm.self_id = "qq_official_full"
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = group_id
            abm.session_id = group_id
            abm.sender = MessageMember(sender_id, author.get("username") or "")
            qq_scene = "group"
            self.remember_session(
                abm.session_id,
                scene="group",
                target_id=group_id,
                message_id=abm.message_id,
            )
        elif event_type == "C2C_MESSAGE_CREATE":
            author = payload.get("author") or {}
            sender_id = str(author.get("user_openid") or author.get("id") or "")
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = sender_id
            abm.sender = MessageMember(sender_id, author.get("username") or "")
            abm.self_id = "qq_official_full"
            qq_scene = "c2c"
            self.remember_session(
                abm.session_id,
                scene="c2c",
                target_id=sender_id,
                message_id=abm.message_id,
            )
        elif event_type == "AT_MESSAGE_CREATE":
            author = payload.get("author") or {}
            channel_id = str(payload.get("channel_id") or "")
            bot_id = "qq_official_full"
            mentions = payload.get("mentions") or []
            if mentions:
                bot_id = str(mentions[0].get("id") or bot_id)
                content = content.replace(f"<@!{bot_id}>", "").replace(
                    f"<@{bot_id}>", ""
                )
                components.append(
                    At(qq=bot_id, name=str(mentions[0].get("username") or ""))
                )
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = channel_id
            abm.session_id = channel_id
            abm.sender = MessageMember(
                str(author.get("id") or ""), author.get("username") or ""
            )
            abm.self_id = bot_id
            qq_scene = "channel"
            self.remember_session(
                abm.session_id,
                scene="channel",
                target_id=channel_id,
                message_id=abm.message_id,
            )
        elif event_type == "DIRECT_MESSAGE_CREATE":
            author = payload.get("author") or {}
            sender_id = str(author.get("id") or author.get("user_openid") or "")
            guild_id = str(payload.get("guild_id") or "")
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = sender_id or guild_id
            abm.sender = MessageMember(sender_id, author.get("username") or "")
            abm.self_id = "qq_official_full"
            qq_scene = "dm"
            self.remember_session(
                abm.session_id,
                scene="dm",
                target_id=guild_id,
                message_id=abm.message_id,
            )
        else:
            author = payload.get("author") or {}
            sender_id = str(author.get("user_openid") or author.get("id") or "")
            abm.type = MessageType.OTHER_MESSAGE
            abm.session_id = sender_id or str(payload.get("id") or event_id or "")
            abm.sender = MessageMember(sender_id, author.get("username") or "")
            abm.self_id = "qq_official_full"

        reply = await self._parse_reply_component(payload)
        if reply:
            components.insert(0, reply)
        plain = content.strip()
        abm.message_str = plain
        if plain:
            components.append(Plain(plain))
        await self._append_attachments(components, payload.get("attachments") or [])
        abm.message = components
        if not getattr(abm, "self_id", ""):
            abm.self_id = "qq_official_full"
        self._pending_event_extras[abm.message_id] = {
            "qq_scene": qq_scene,
            "event_id": event_id,
            "raw_event_type": event_type,
        }
        return abm

    async def _parse_reply_component(self, payload: dict) -> Reply | None:
        """Parse QQ quote message elements into a Reply component.

        Args:
            payload: QQ event payload.

        Returns:
            Reply component or None.
        """
        msg_elements = payload.get("msg_elements") or []
        if not msg_elements:
            return None
        ref = msg_elements[0]
        chain: list[BaseMessageComponent] = []
        text = _parse_face_message(str(ref.get("content") or "")).strip()
        if text:
            chain.append(Plain(text))
        await self._append_attachments(chain, ref.get("attachments") or [])
        author = ref.get("author") or {}
        return Reply(
            id=str(ref.get("msg_idx") or ref.get("id") or ""),
            chain=chain,
            sender_id=str(
                author.get("member_openid")
                or author.get("user_openid")
                or author.get("id")
                or ""
            ),
            sender_nickname=str(author.get("username") or ""),
            message_str=text,
        )

    async def _append_attachments(
        self, components: list[BaseMessageComponent], attachments: list
    ) -> None:
        """Append QQ attachments to an AstrBot component chain.

        Args:
            components: Component list to mutate.
            attachments: Raw QQ attachment list.
        """
        for attachment in attachments:
            content_type = str(_attr(attachment, "content_type", "") or "").lower()
            filename = str(
                _attr(attachment, "filename", None)
                or _attr(attachment, "name", None)
                or "file"
            )
            url = _normalize_url(cast(str | None, _attr(attachment, "url", None)))
            if not url:
                continue
            ext = Path(filename).suffix.lower()
            if content_type.startswith("image") or ext in {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".bmp",
            }:
                components.append(Image.fromURL(url))
            elif content_type.startswith(("voice", "audio")) or ext in {
                ".mp3",
                ".wav",
                ".ogg",
                ".m4a",
                ".amr",
                ".silk",
            }:
                components.append(Record(file=url, url=url))
            elif content_type.startswith("video") or ext in {
                ".mp4",
                ".mov",
                ".avi",
                ".mkv",
                ".webm",
            }:
                components.append(Video.fromURL(url))
            else:
                components.append(File(name=filename, file=url, url=url))

    async def _dispatch_payload_event(
        self, event_type: str, payload: dict, event_id: str | None = None
    ) -> None:
        """Parse and commit one QQ dispatch event.

        Args:
            event_type: QQ event type.
            payload: QQ event payload.
            event_id: QQ event id.
        """
        if event_type == "INTERACTION_CREATE":
            interaction_id = str(payload.get("id") or "")
            if interaction_id:
                await self.client.acknowledge_interaction(interaction_id)
            return
        abm = await self._parse_message_event(event_type, payload, event_id)
        self.commit_event(self.create_event(abm))

    async def _send_gateway_auth(self, websocket: Any, token: str) -> None:
        """Send identify or resume payload to QQ gateway.

        Args:
            websocket: WebSocket connection object.
            token: Access token without QQBot prefix.
        """
        if self._session_id and self._last_seq is not None:
            payload = {
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            }
        else:
            payload = {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self.intents,
                    "shard": [0, 1],
                    "properties": {
                        "os": os.name,
                        "browser": "AstrBot",
                        "device": "AstrBot",
                    },
                },
            }
        await websocket.send(json.dumps(payload))

    def _handle_gateway_close(
        self, exc: QQGatewayClosed, reconnect_delay: float
    ) -> float:
        """Update gateway state and choose reconnect delay after close.

        Args:
            exc: Gateway close exception.
            reconnect_delay: Current reconnect delay.

        Returns:
            Next reconnect delay in seconds.
        """
        if (
            exc.code
            in {
                GATEWAY_CLOSE_AUTH_FAILED,
                GATEWAY_CLOSE_INVALID_SESSION,
                GATEWAY_CLOSE_SEQ_OUT_OF_RANGE,
                GATEWAY_CLOSE_SESSION_TIMEOUT,
            }
            or 4900 <= exc.code <= 4913
        ):
            self._session_id = None
            self._last_seq = None
            self.client.clear_token()
        if exc.code == GATEWAY_CLOSE_RATE_LIMITED:
            return GATEWAY_RATE_LIMIT_DELAY
        if exc.code in {
            GATEWAY_CLOSE_INSUFFICIENT_INTENTS,
            GATEWAY_CLOSE_DISALLOWED_INTENTS,
        }:
            raise exc
        return reconnect_delay

    async def _gateway_heartbeat(self, websocket: Any, interval_ms: int) -> None:
        """Send gateway heartbeats until cancelled.

        Args:
            websocket: WebSocket connection.
            interval_ms: Heartbeat interval from QQ Hello payload.
        """
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(interval_ms / 1000)
                await websocket.send(
                    json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq})
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[QQOfficialFull] Gateway heartbeat stopped: %s", exc)

    async def _gateway_once(self) -> None:
        """Run one QQ gateway WebSocket connection."""
        gateway_url = self.gateway_url_override or await self.client.get_gateway()
        token = await self.client.get_access_token()
        async with websockets.connect(gateway_url) as websocket:
            async for raw_message in websocket:
                payload = json.loads(raw_message)
                op = payload.get("op")
                if payload.get("s") is not None:
                    self._last_seq = int(payload["s"])
                if op == OP_HELLO:
                    interval = int(
                        payload.get("d", {}).get("heartbeat_interval") or 45000
                    )
                    await self._send_gateway_auth(websocket, token)
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    self._heartbeat_task = asyncio.create_task(
                        self._gateway_heartbeat(websocket, interval)
                    )
                elif op == OP_DISPATCH:
                    event_type = str(payload.get("t") or "")
                    data = payload.get("d") or {}
                    if event_type == "READY":
                        self._session_id = str(data.get("session_id") or "")
                    elif event_type == "RESUMED":
                        logger.info("[QQOfficialFull] Gateway session resumed.")
                    elif event_type:
                        await self._dispatch_payload_event(
                            event_type, data, payload.get("id")
                        )
                elif op == OP_RECONNECT:
                    raise QQGatewayClosed(
                        GATEWAY_CLOSE_SESSION_TIMEOUT, "server reconnect"
                    )
                elif op == OP_INVALID_SESSION:
                    self._session_id = None
                    self._last_seq = None
                    raise QQGatewayClosed(
                        GATEWAY_CLOSE_INVALID_SESSION, "invalid session"
                    )

    async def run(self) -> None:
        """Run the QQ WebSocket gateway loop."""
        reconnect_delay = 1.0
        while not self._shutdown_event.is_set():
            try:
                await self._gateway_once()
                reconnect_delay = 1.0
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                reconnect_delay = self._handle_gateway_close(
                    QQGatewayClosed(exc.code, exc.reason), reconnect_delay
                )
                logger.warning(
                    "[QQOfficialFull] Gateway closed: %s, reconnecting in %.1fs",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)
            except Exception as exc:
                logger.warning(
                    "[QQOfficialFull] Gateway error: %s, reconnecting in %.1fs",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    async def terminate(self) -> None:
        """Terminate gateway and HTTP resources."""
        self._shutdown_event.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await self.client.close()


@register_platform_adapter(
    "qq_official_full_webhook",
    "QQ Official full adapter (Webhook)",
    default_config_tmpl=dict(DEFAULT_WEBHOOK_CONFIG),
    adapter_display_name="QQ Official Full Webhook",
    support_streaming_message=True,
    config_metadata={
        **CONFIG_METADATA,
        "unified_webhook_mode": {"type": "bool", "default": True},
        "webhook_uuid": {"type": "string", "default": ""},
        "verify_webhook_signature": {"type": "bool", "default": True},
    },
)
class QQOfficialFullWebhookPlatformAdapter(QQOfficialFullPlatformAdapter):
    """QQ Official full adapter using HTTP webhook callbacks."""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, platform_settings, event_queue)
        self._webhook_seen_events: dict[str, float] = {}
        self._webhook_shutdown = asyncio.Event()
        self._webhook_server: FastAPIWebhookServer | None = None

    def meta(self) -> PlatformMetadata:
        """Return platform metadata.

        Returns:
            AstrBot platform metadata.
        """
        return PlatformMetadata(
            name="qq_official_full_webhook",
            description="QQ Official full webhook adapter",
            id=cast(str, self.config.get("id")),
            support_streaming_message=True,
            support_proactive_message=True,
        )

    async def webhook_callback(self, request: Any) -> Any:
        """Handle a unified QQ webhook callback.

        Args:
            request: AstrBot webhook request wrapper or compatible object.

        Returns:
            QQ webhook acknowledgement or error tuple.
        """
        body = await _maybe_await(request.get_data())
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"error": "Invalid JSON"}, 400
        if not isinstance(envelope, dict):
            return {"error": "Invalid JSON"}, 400

        opcode = envelope.get("op")
        data = envelope.get("d") or {}
        if opcode == OP_WEBHOOK_VALIDATION:
            event_ts = str(data.get("event_ts") or "")
            plain_token = str(data.get("plain_token") or "")
            signature = _sign_webhook(
                self.secret,
                event_ts,
                plain_token.encode("utf-8"),
            )
            return {"plain_token": plain_token, "signature": signature}

        if bool(self.config.get("verify_webhook_signature", True)):
            headers = request.headers
            timestamp = headers.get(WEBHOOK_TIMESTAMP_HEADER)
            tolerance = int(self.config.get("webhook_timestamp_tolerance_seconds") or 0)
            if tolerance > 0:
                try:
                    too_old = abs(time.time() - int(str(timestamp))) > tolerance
                except (TypeError, ValueError):
                    too_old = True
                if too_old:
                    return {"error": "Invalid signature"}, 401
            if not _verify_webhook_signature(
                self.secret,
                timestamp,
                headers.get(WEBHOOK_SIGNATURE_HEADER),
                body,
            ):
                return {"error": "Invalid signature"}, 401

        event_id = str(envelope.get("id") or "")
        if event_id:
            now = time.monotonic()
            self._webhook_seen_events = {
                key: seen_at
                for key, seen_at in self._webhook_seen_events.items()
                if now - seen_at <= 60
            }
            if event_id in self._webhook_seen_events:
                return {"opcode": OP_WEBHOOK_CALLBACK_ACK}
            self._webhook_seen_events[event_id] = now

        if opcode == OP_DISPATCH and envelope.get("t"):
            await self._dispatch_payload_event(str(envelope["t"]), data, event_id)
        return {"opcode": OP_WEBHOOK_CALLBACK_ACK}

    async def run(self) -> None:
        """Run webhook mode, either unified or standalone."""
        webhook_uuid = self.config.get("webhook_uuid")
        if self.unified_webhook() and webhook_uuid:
            log_webhook_info(
                f"{self.meta().id}(QQ Official Full Webhook)", webhook_uuid
            )
            await self._webhook_shutdown.wait()
            return
        host = str(self.config.get("callback_server_host") or "0.0.0.0")
        port = int(self.config.get("port") or 6197)
        self._webhook_server = FastAPIWebhookServer("qq-official-full-webhook")
        self._webhook_server.add_url_rule(
            "/astrbot-qqofficial-full/callback",
            view_func=self.webhook_callback,
            methods=["POST"],
        )
        await self._webhook_server.run_task(
            host=host,
            port=port,
            shutdown_trigger=self._webhook_shutdown.wait,
        )

    async def terminate(self) -> None:
        """Terminate webhook server and HTTP resources."""
        self._webhook_shutdown.set()
        if self._webhook_server:
            await self._webhook_server.shutdown()
        await self.client.close()
