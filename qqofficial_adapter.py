from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

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
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_temp_path,
)
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
MAX_DATA_URL_BYTES = 10 * 1024 * 1024
WEBHOOK_MAX_BODY_BYTES = 1_048_576
WEBHOOK_RATE_WINDOW_SECONDS = 60.0
WEBHOOK_RATE_MAX_REQUESTS = 600
WEBHOOK_RATE_MAX_KEYS = 4096


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
    if url.startswith("//"):
        return f"https:{url}"
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


def _persist_temp_media(data: bytes, file_name: str | None = None) -> str:
    """Write bytes to AstrBot temp dir and return the file path.

    Args:
        data: File content.
        file_name: Optional original file name for the extension.

    Returns:
        Temp file path string.
    """
    temp_dir = Path(get_astrbot_temp_path())
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(_safe_filename(file_name)).suffix
    tmp_path = temp_dir / f"qqfull_upload_{os.urandom(8).hex()}{suffix}"
    tmp_path.write_bytes(data)
    return str(tmp_path)


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


def _strip_mention_text(content: str, mentions: list | None) -> str:
    """Clean QQ <@openid> tokens, mirroring openclaw stripMentionText.

    Bot self mentions are removed; other users' mentions are rewritten to
    @nickname so downstream text stays readable.

    Args:
        content: Raw message content.
        mentions: Mention entries from the QQ payload.

    Returns:
        Content with mention tokens cleaned.
    """
    if not content or not mentions:
        return content
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        openid = str(
            mention.get("member_openid")
            or mention.get("user_openid")
            or mention.get("id")
            or ""
        )
        if not openid:
            continue
        replacement = ""
        if not mention.get("is_you"):
            display = mention.get("nickname") or mention.get("username")
            if display:
                replacement = f"@{display}"
        content = content.replace(f"<@!{openid}>", replacement).replace(
            f"<@{openid}>", replacement
        )
    return content


# ── 出站文本清理 (移植自 openclaw-qqbot src/outbound/sanitize.ts) ──

_INTERNAL_TAG_PATTERNS = (
    re.compile(r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>", re.I),
    re.compile(r"<previous_response\b[^>]*>[\s\S]*?</previous_response>", re.I),
    re.compile(r"<\s*/?\s*(?:system-reminder|previous_response)\b[^>]*/?\s*>", re.I),
    re.compile(r"`think`[\s\S]*?`/think`", re.I),
    re.compile(r"<\s*/?\s*think\b[^>]*/?\s*>", re.I),
    re.compile(r"<thinking\b[^>]*>[\s\S]*?</thinking>", re.I),
    re.compile(r"<\s*/?\s*thinking\b[^>]*/?\s*>", re.I),
)


def _sanitize_qq_text(text: str) -> str:
    """Strip framework scaffolding and model reasoning tags from outbound text.

    Args:
        text: Raw outbound text.

    Returns:
        Sanitized text.
    """
    result = text
    for pattern in _INTERNAL_TAG_PATTERNS:
        result = pattern.sub("", result)
    return result.strip()


# ── Table-aware 长文本分块 (移植自 openclaw-qqbot src/channel.ts chunker) ──

TEXT_CHUNK_LIMIT = 5000
_GFM_TABLE_DATA_RE = re.compile(r"^\|.+\|.*\|")
_GFM_TABLE_SEP_RE = re.compile(r"^\|[\s:-]+\|")


def _is_gfm_table_line(line: str) -> bool:
    """Check whether a line is a GFM table data or separator row.

    Args:
        line: One text line.

    Returns:
        Whether the line belongs to a markdown table.
    """
    return bool(_GFM_TABLE_DATA_RE.match(line) or _GFM_TABLE_SEP_RE.match(line))


def _chunk_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> list[str]:
    """Split text on line boundaries keeping markdown tables intact.

    Args:
        text: Text to split.
        limit: Max chunk length.

    Returns:
        One or more chunks, each within the limit when possible.
    """
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if len(raw_line) <= limit or _is_gfm_table_line(raw_line):
            lines.append(raw_line)
        else:
            # hard split over-long non-table lines to respect the API limit
            lines.extend(
                raw_line[i : i + limit] for i in range(0, len(raw_line), limit)
            )
    chunks: list[str] = []
    current = ""
    table_buffer: list[str] = []

    def flush_table() -> None:
        nonlocal current
        if not table_buffer:
            return
        table_block = "\n".join(table_buffer)
        candidate = f"{current}\n{table_block}" if current else table_block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = table_block
        else:
            current = candidate
        table_buffer.clear()

    for line in lines:
        if _is_gfm_table_line(line):
            table_buffer.append(line)
            continue
        flush_table()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    flush_table()
    if current:
        chunks.append(current)
    return chunks if chunks else [text]


# ── SSRF 防护 (移植自 openclaw-qqbot src/utils/ssrf-guard.ts) ──

_ALLOWED_REMOTE_SCHEMES = {"http", "https"}

_QQ_TRUSTED_DOMAINS = frozenset(
    {
        "api.sgroup.qq.com",
        "sandbox.api.sgroup.qq.com",
        "bots.qq.com",
        "multimedia.nt.qq.com.cn",
        "multimedia.nt.qq.com",
        "grouppro.grouppro.qq.com",
    }
)


def _is_reserved_addr(ip: str) -> bool:
    """Check whether an IP falls in a private / reserved / metadata range.

    Args:
        ip: IPv4 or IPv6 address string.

    Returns:
        Whether the address must not be fetched.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _is_qq_trusted_domain(hostname: str) -> bool:
    """Check hostname against QQ official domain whitelist (one sub level).

    Args:
        hostname: Lowercase hostname.

    Returns:
        Whether the host is trusted.
    """
    if hostname in _QQ_TRUSTED_DOMAINS:
        return True
    dot = hostname.find(".")
    return dot > 0 and hostname[dot + 1 :] in _QQ_TRUSTED_DOMAINS


async def _validate_remote_url(raw: str) -> None:
    """Validate a remote URL is safe to fetch (anti-SSRF).

    Args:
        raw: URL to validate.

    Raises:
        QQOfficialAPIError: If the scheme is unsupported or the target
            resolves to a private / reserved address.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in _ALLOWED_REMOTE_SCHEMES:
        raise QQOfficialAPIError(
            "GET", raw, 0, f'不支持的协议 "{parsed.scheme}"，仅允许 http/https'
        )
    hostname = parsed.hostname or ""
    if _is_qq_trusted_domain(hostname):
        return

    def resolve() -> list[str]:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(hostname, port)
        except socket.gaierror:
            return []
        return [str(info[4][0]) for info in infos]

    ips = await asyncio.to_thread(resolve)
    for ip in ips:
        if _is_reserved_addr(ip):
            raise QQOfficialAPIError(
                "GET",
                raw,
                0,
                f'禁止访问内网地址 "{ip}"，已拦截潜在的 SSRF 请求',
            )


# ── Markdown 图片尺寸探测 (移植自 openclaw-qqbot src/outbound/image-size.ts) ──

_IMAGE_SIZE_CACHE_TTL = 3600.0
_image_size_cache: dict[str, tuple[tuple[int, int] | None, float]] = {}
_MD_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((\S+?)(?:\s+[\"'][^\"']*[\"'])?\)")


def _parse_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Parse width/height from image header bytes (PNG/JPEG/GIF/WebP).

    Args:
        data: Leading bytes of an image file.

    Returns:
        (width, height) or None when unparsable.
    """
    if len(data) < 8:
        return None
    if data[:4] == b"\x89PNG" and len(data) >= 24:
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset < len(data) - 9:
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
                return (
                    int.from_bytes(data[offset + 7 : offset + 9], "big"),
                    int.from_bytes(data[offset + 5 : offset + 7], "big"),
                )
            offset += 2 + int.from_bytes(data[offset + 2 : offset + 4], "big")
    if data[:3] == b"GIF" and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 ":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


async def _get_image_size(
    http: httpx.AsyncClient, url: str, timeout: float = 5.0
) -> tuple[int, int] | None:
    """Probe image dimensions via a ranged HEAD-style fetch, with caching.

    Args:
        http: Shared HTTP client.
        url: Image URL.
        timeout: Probe timeout in seconds.

    Returns:
        (width, height) or None on any failure.
    """
    cached = _image_size_cache.get(url)
    now = time.time()
    if cached and now < cached[1]:
        return cached[0]
    size: tuple[int, int] | None = None
    try:
        current = url
        for _hop in range(4):
            await _validate_remote_url(current)
            response = await http.get(
                current,
                headers={"Range": "bytes=0-32767"},
                timeout=timeout,
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    break
                current = urljoin(current, location)
                continue
            if response.status_code in {200, 206}:
                size = _parse_image_dimensions(response.content)
            break
    except Exception as exc:
        logger.debug("[QQOfficialFull] Image size probe failed for %s: %s", url, exc)
    if len(_image_size_cache) > 512:
        _image_size_cache.clear()
    _image_size_cache[url] = (size, now + _IMAGE_SIZE_CACHE_TTL)
    return size


def _format_qq_markdown_image(url: str, size: tuple[int, int] | None) -> str:
    """Render a QQ markdown image tag with optional size hint.

    Args:
        url: Image URL.
        size: Optional (width, height) hint.

    Returns:
        Markdown image string.
    """
    if size:
        return f"![img #{size[0]}x{size[1]}]({url})"
    return f"![img]({url})"


async def _enhance_markdown_images(http: httpx.AsyncClient, text: str) -> str:
    """Rewrite markdown image links with QQ size hints.

    Args:
        http: Shared HTTP client.
        text: Outbound markdown text.

    Returns:
        Text with probed image tags rewritten.
    """
    if "![" not in text:
        return text
    parts: list[str] = []
    last = 0
    for match in _MD_IMAGE_PATTERN.finditer(text):
        parts.append(text[last : match.start()])
        url = match.group(1)
        rendered = match.group(0)
        if url.startswith(("http://", "https://")):
            size = await _get_image_size(http, url)
            rendered = _format_qq_markdown_image(url, size)
        parts.append(rendered)
        last = match.end()
    parts.append(text[last:])
    return "".join(parts)


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


# ── 被动回复限额 (移植自 openclaw-qqbot src/outbound/reply-limiter.ts) ──


class ReplyLimiter:
    """Track passive replies per source message and cap them.

    Args:
        limit: Max passive replies per message.
        ttl_seconds: Message id validity window in seconds.
        max_tracked: LRU cap on tracked messages.
    """

    def __init__(
        self,
        limit: int = 4,
        ttl_seconds: float = 3600.0,
        max_tracked: int = 10_000,
    ) -> None:
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self.max_tracked = max_tracked
        self._messages: OrderedDict[str, dict[str, float]] = OrderedDict()

    def check_limit(self, message_id: str) -> tuple[bool, int, str | None]:
        """Check whether another passive reply is allowed for a message.

        Args:
            message_id: Source QQ message id.

        Returns:
            allowed, remaining, and fallback reason tuple.
        """
        tracked = self._messages.get(message_id)
        if tracked is None:
            return True, self.limit, None
        if time.time() - tracked["first_seen_at"] > self.ttl_seconds:
            del self._messages[message_id]
            return False, 0, "expired"
        remaining = max(0, self.limit - int(tracked["count"]))
        if remaining <= 0:
            return False, 0, "limit_exceeded"
        return True, remaining, None

    def record(self, message_id: str) -> None:
        """Record one passive reply for a message.

        Args:
            message_id: Source QQ message id.
        """
        tracked = self._messages.get(message_id)
        if tracked is not None:
            tracked["count"] += 1
            self._messages.move_to_end(message_id)
            return
        while len(self._messages) >= self.max_tracked:
            self._messages.popitem(last=False)
        self._messages[message_id] = {
            "count": 1.0,
            "first_seen_at": time.time(),
        }

    def clear(self) -> None:
        """Drop all tracked messages."""
        self._messages.clear()


# ── 被动回复 msgId 缓存 (移植自 openclaw-qqbot src/features/msgid-cache.ts) ──

_MSGID_MAX_PER_TARGET = 10
_MSGID_TTL_SECONDS = {"group": 5 * 60.0, "c2c": 30 * 60.0}
_MSGID_MAX_TARGETS = 200


class MsgIdCache:
    """Cache recent inbound msg ids per target for passive replies."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def cache(self, scope: str, target_id: str, msg_id: str) -> None:
        """Remember one recent msg id for a target.

        Args:
            scope: group or c2c.
            target_id: QQ target id.
            msg_id: QQ message id.
        """
        if not scope or not target_id or not msg_id:
            return
        key = f"{scope}:{target_id}"
        entry = {"msg_id": msg_id, "timestamp": time.time()}
        existing = self._cache.get(key)
        if existing is not None:
            existing.append(entry)
            del existing[:-_MSGID_MAX_PER_TARGET]
            self._cache.move_to_end(key)
            return
        self._cache[key] = [entry]
        while len(self._cache) > _MSGID_MAX_TARGETS:
            self._cache.popitem(last=False)

    def get(self, scope: str, target_id: str) -> str | None:
        """Return the newest non-expired msg id for a target.

        Args:
            scope: group or c2c.
            target_id: QQ target id.

        Returns:
            Cached message id or None.
        """
        entries = self._cache.get(f"{scope}:{target_id}")
        if not entries:
            return None
        ttl = _MSGID_TTL_SECONDS.get(scope, _MSGID_TTL_SECONDS["group"])
        now = time.time()
        for entry in reversed(entries):
            if now - float(entry["timestamp"]) < ttl:
                return str(entry["msg_id"])
        return None

    def clear(self, scope: str, target_id: str) -> None:
        """Drop cached ids for one target.

        Args:
            scope: group or c2c.
            target_id: QQ target id.
        """
        self._cache.pop(f"{scope}:{target_id}", None)


# ── 引用索引持久化 (移植自 openclaw-qqbot src/features/ref-index-store.ts) ──

_REF_INDEX_MAX_ENTRIES = 50_000
_REF_INDEX_COMPACT_RATIO = 2.0


class PersistedRefIndexStore:
    """LRU ref-index (REFIDX key -> quoted message entry) with JSONL persistence.

    Args:
        file_path: JSONL store path.
        max_entries: LRU and compact capacity.
    """

    def __init__(
        self, file_path: Path, max_entries: int = _REF_INDEX_MAX_ENTRIES
    ) -> None:
        self.file_path = Path(file_path)
        self.max_entries = max(100, max_entries)
        self._memory: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._disk_line_count = 0
        self._lock = threading.Lock()
        self._init_load()

    def _init_load(self) -> None:
        """Replay JSONL from disk into the in-memory LRU."""
        try:
            if not self.file_path.exists():
                return
            lines = self.file_path.read_text(encoding="utf-8").splitlines()
            parsed: list[tuple[float, str, dict[str, Any]]] = []
            for line in lines:
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("k") and obj.get("v"):
                    parsed.append((float(obj.get("t") or 0), str(obj["k"]), obj["v"]))
            for _ts, key, value in sorted(parsed, key=lambda item: item[0]):
                self._touch_memory(key, value)
            self._disk_line_count = len(lines)
            if self._disk_line_count > self.max_entries * _REF_INDEX_COMPACT_RATIO:
                self._compact()
        except Exception as exc:
            logger.warning("[QQOfficialFull] ref-index init failed: %s", exc)

    def get(self, key: str) -> dict[str, Any] | None:
        """Look up a stored ref entry by key.

        Args:
            key: REFIDX key or message id.

        Returns:
            Stored entry or None.
        """
        with self._lock:
            value = self._memory.get(key)
            return dict(value) if value else None

    def set(self, key: str, entry: dict[str, Any]) -> None:
        """Store one ref entry in memory and append it to disk.

        Args:
            key: REFIDX key or message id.
            entry: Ref entry payload.
        """
        with self._lock:
            self._touch_memory(key, entry)
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(
                    {"k": key, "v": entry, "t": int(time.time() * 1000)},
                    ensure_ascii=False,
                )
                with self.file_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self._disk_line_count += 1
                if self._disk_line_count > self.max_entries * _REF_INDEX_COMPACT_RATIO:
                    self._compact()
            except Exception as exc:
                logger.warning("[QQOfficialFull] ref-index append failed: %s", exc)

    def _touch_memory(self, key: str, entry: dict[str, Any]) -> None:
        if key in self._memory:
            del self._memory[key]
        else:
            while len(self._memory) >= self.max_entries:
                self._memory.popitem(last=False)
        self._memory[key] = entry

    def _compact(self) -> None:
        """Rewrite the JSONL file from memory, dropping redundant lines."""
        tmp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        try:
            now = int(time.time() * 1000)
            lines = [
                json.dumps({"k": key, "v": value, "t": now}, ensure_ascii=False)
                for key, value in self._memory.items()
            ]
            content = "\n".join(lines) + "\n" if lines else ""
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, self.file_path)
            self._disk_line_count = len(lines)
        except Exception as exc:
            logger.warning("[QQOfficialFull] ref-index compact failed: %s", exc)
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @property
    def size(self) -> int:
        """Number of in-memory entries."""
        return len(self._memory)


# ── 流式控制器 (移植自 openclaw-qqbot src/outbound/streaming-controller.ts) ──

STREAM_MIN_UPDATE_INTERVAL = 1.0
# Official stream_messages REST protocol: input_mode/content_type are
# STRINGS ("replace"/"append", "markdown"/"text"); input_state is an
# integer (1=generating, 10=done). Numeric enums are rejected silently
# and the server falls back to append semantics -> duplicated content.
STREAM_INPUT_MODE_REPLACE = "replace"
STREAM_CONTENT_TYPE_MARKDOWN = "markdown"
STREAM_INPUT_STATE_GENERATING = 1
STREAM_INPUT_STATE_DONE = 10

_MSG_SEQ_LOCK = threading.Lock()
_msg_seq_counter = 0


def _next_msg_seq_global() -> int:
    """Process-wide monotonic msg_seq (QQ dedupes out-of-order packets)."""
    global _msg_seq_counter
    with _MSG_SEQ_LOCK:
        _msg_seq_counter += 1
        return _msg_seq_counter


def _normalize_ws(text: str) -> str:
    """Collapse whitespace runs into single spaces."""
    return re.sub(r"\s+", " ", text)


def _prefix_matches(accepted: str, incoming: str) -> bool:
    """Check incoming text keeps the accepted prefix (whitespace tolerant)."""
    if incoming.startswith(accepted):
        return True
    return _normalize_ws(incoming).startswith(_normalize_ws(accepted))


def _longest_common_prefix_len(a: str, b: str) -> int:
    """Return shared prefix length of two strings."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


class QQStreamingController:
    """Stateful C2C stream sender honouring QQ immutable-prefix constraints.

    Args:
        client: QQ REST client.
        openid: C2C target openid.
        event_message_id: Source message id for the stream payload.
        on_completed: Callback(ret, content) when the stream session completes.
    """

    def __init__(
        self,
        client: QQOfficialClient,
        openid: str,
        event_message_id: str,
        on_completed: Any = None,
    ) -> None:
        self.client = client
        self.openid = openid
        self.event_message_id = event_message_id
        self.on_completed = on_completed
        self.phase = "idle"
        self.session_open = False
        self.last_accepted_full = ""
        self.last_seen_text = ""
        self.sent_chunk_count = 0
        self._stream_msg_id: str | None = None
        self._index = 0
        self._last_send_ts = 0.0

    @property
    def is_terminal(self) -> bool:
        """Whether the controller reached a final phase."""
        return self.phase in {"done", "failed"}

    def _reset_session(self) -> None:
        """Reset session-scoped identifiers for a new stream segment."""
        self.session_open = False
        self._stream_msg_id = None
        self._index = 0
        self._last_send_ts = 0.0
        self.last_accepted_full = ""

    def _throttled(self) -> bool:
        """Whether an in-flight mid-stream update is within the min interval."""
        return (
            self.session_open
            and time.monotonic() - self._last_send_ts < STREAM_MIN_UPDATE_INTERVAL
        )

    def _next_msg_seq(self) -> int:
        """Global monotonic msg_seq (QQ rejects out-of-order packets)."""
        return _next_msg_seq_global()

    @property
    def should_fallback_to_static(self) -> bool:
        """Whether the stream failed without sending anything usable."""
        return self.is_terminal and self.sent_chunk_count == 0

    def _transition(self, next_phase: str, reason: str) -> None:
        if self.phase != next_phase:
            logger.info(
                "[QQOfficialFull] stream phase: %s -> %s (%s)",
                self.phase,
                next_phase,
                reason,
            )
            self.phase = next_phase

    async def on_partial(self, text: str) -> None:
        """Feed one cumulative snapshot from the model.

        Args:
            text: Full text so far.
        """
        if self.is_terminal or not text:
            return
        self.last_seen_text = text
        if _prefix_matches(self.last_accepted_full, text):
            if len(text) != len(self.last_accepted_full):
                if self._throttled():
                    return
                await self._send_update(text)
            return
        if not self.last_accepted_full:
            await self._send_update(text)
            return
        if len(text) < len(self.last_accepted_full):
            logger.info(
                "[QQOfficialFull] stream new reply: %d -> %d chars",
                len(self.last_accepted_full),
                len(text),
            )
            await self._complete_session("new_reply")
            self.last_accepted_full = ""
            await self._send_update(text)
            return
        common_len = _longest_common_prefix_len(self.last_accepted_full, text)
        merged = self.last_accepted_full + text[common_len:]
        logger.warning(
            "[QQOfficialFull] stream prefix rewritten (common=%d), "
            "appending tail to keep accepted prefix",
            common_len,
        )
        await self._send_update(merged)

    async def end_segment(self, reason: str = "break") -> None:
        """Close the current stream segment and prepare a fresh session.

        Args:
            reason: Completion reason for logs.
        """
        if self.is_terminal:
            return
        await self._complete_session(reason)
        self._reset_session()
        if self.phase == "streaming":
            self._transition("idle", reason)

    async def finalize(self) -> None:
        """Close the stream and settle the final phase."""
        if self.is_terminal:
            return
        if self.session_open:
            await self._complete_session("done")
            self._transition("done", "finalize")
            logger.info(
                "[QQOfficialFull] stream done: chunks=%d chars=%d",
                self.sent_chunk_count,
                len(self.last_accepted_full),
            )
            return
        if self.sent_chunk_count > 0:
            self._transition("done", "finalize:no_session")
        else:
            self._transition("failed", "finalize:fallback")

    async def abort(self, reason: str = "manual") -> None:
        """Abort the stream, completing any open session.

        Args:
            reason: Abort reason for logs.
        """
        if self.is_terminal:
            return
        logger.warning(
            "[QQOfficialFull] aborting stream: reason=%s sent=%d",
            reason,
            self.sent_chunk_count,
        )
        await self._complete_session(f"abort:{reason}")
        self._transition("failed", f"abort:{reason}")

    async def _send_update(self, text: str) -> None:
        if not self.session_open:
            self.session_open = True
            self._transition("streaming", "first_chunk")
        payload: dict[str, Any] = {
            "input_mode": STREAM_INPUT_MODE_REPLACE,
            "input_state": STREAM_INPUT_STATE_GENERATING,
            "content_type": STREAM_CONTENT_TYPE_MARKDOWN,
            "content_raw": text,
            "event_id": self.event_message_id,
            "msg_id": self.event_message_id,
            "msg_seq": self._next_msg_seq(),
            "index": self._index,
        }
        if self._stream_msg_id:
            payload["stream_msg_id"] = self._stream_msg_id
        try:
            ret = await self.client.send_stream_message(self.openid, payload)
        except Exception as exc:
            logger.error(
                "[QQOfficialFull] stream update failed (len=%d): %s",
                len(text),
                exc,
            )
            self.session_open = False
            self._transition("failed", "update_error")
            return
        self._last_send_ts = time.monotonic()
        self._stream_msg_id = (
            ret.get("id") or ret.get("stream_msg_id") or self._stream_msg_id
        )
        self._index += 1
        self.last_accepted_full = text
        self.sent_chunk_count += 1

    async def _complete_session(self, reason: str = "done") -> None:
        if not self.session_open:
            return
        # Throttled updates may leave last_seen ahead of last_accepted;
        # only adopt it when it keeps the immutable prefix.
        content = self.last_accepted_full
        if (
            self.last_seen_text
            and len(self.last_seen_text) > len(content)
            and _prefix_matches(content, self.last_seen_text)
        ):
            content = self.last_seen_text
        payload: dict[str, Any] = {
            "input_mode": STREAM_INPUT_MODE_REPLACE,
            "input_state": STREAM_INPUT_STATE_DONE,
            "content_type": STREAM_CONTENT_TYPE_MARKDOWN,
            "content_raw": content or "\n",
            "event_id": self.event_message_id,
            "msg_id": self.event_message_id,
            "msg_seq": self._next_msg_seq(),
            "index": self._index,
        }
        if self._stream_msg_id:
            payload["stream_msg_id"] = self._stream_msg_id
        try:
            ret = await self.client.send_stream_message(self.openid, payload)
            self._index += 1
            self.last_accepted_full = content
            if self.on_completed and ret:
                try:
                    self.on_completed(ret, content)
                except Exception as exc:
                    logger.debug("[QQOfficialFull] stream ref-index failed: %s", exc)
        except Exception as exc:
            logger.error("[QQOfficialFull] stream complete failed: %s", exc)
        self.session_open = False
        if reason.startswith("new_reply"):
            self._stream_msg_id = None
            self._index = 0


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
        url_direct_upload: bool = True,
        user_agent_suffix: str = "",
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
        self.url_direct_upload = url_direct_upload
        user_agent = "AstrBot/qq_official_full"
        if user_agent_suffix:
            user_agent = f"{user_agent} {user_agent_suffix.strip()}"
        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent})
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._upload_cache: dict[tuple[str, str, str, int], tuple[dict, float]] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """Return the shared HTTP client for direct fetches."""
        return self._http

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
        if response.status_code == 401 and auth:
            # Access token likely expired: refresh once and replay the request.
            logger.debug("[QQOfficialFull] 401 from %s %s, refreshing token", method, path)
            self.clear_token()
            retry_headers = dict(headers or {})
            retry_headers["Authorization"] = f"QQBot {await self.get_access_token()}"
            retry_headers.setdefault("Content-Type", "application/json")
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=json_data,
                    params=query_params,
                    headers=retry_headers,
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

        Raises:
            QQOfficialAPIError: If a base64/data-URL source exceeds limits.
        """
        if (
            source.startswith(("data:", "base64://"))
            and len(source) > MAX_DATA_URL_BYTES
        ):
            size_mb = len(source) / (1024 * 1024)
            raise QQOfficialAPIError(
                "POST",
                self._media_upload_path(scope, target_id),
                0,
                f"Data URL 过大（{size_mb:.1f}MB，最大 10MB）",
            )
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

        if url:
            await _validate_remote_url(url)
            if not self.url_direct_upload:
                # QQ platform may not reach the URL; download it ourselves and
                # upload as base64 (openclaw urlDirectUpload=false behavior).
                raw = await self._download_url_bytes(url)
                if len(raw) >= self.chunked_upload_threshold:
                    return await self._upload_media_chunked(
                        scope,
                        target_id,
                        file_type,
                        _persist_temp_media(raw, file_name),
                        file_name=file_name,
                    )
                file_data = base64.b64encode(raw).decode("utf-8")
                url = None

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

    async def _download_url_bytes(self, url: str) -> bytes:
        """Download remote media bytes for local re-upload.

        Args:
            url: Already SSRF-validated remote URL.

        Returns:
            Downloaded bytes.

        Raises:
            QQOfficialAPIError: On network or HTTP failure, or empty body.
        """
        data = b""
        status = 0
        current = url
        try:
            for _hop in range(5):
                await _validate_remote_url(current)
                response = await self._http.get(current, timeout=120.0)
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                data = response.content
                break
        except QQOfficialAPIError:
            raise
        except Exception as exc:
            raise QQOfficialAPIError("GET", url, 0, f"媒体下载失败: {exc}") from exc
        if status >= 400 or not data:
            raise QQOfficialAPIError(
                "GET", url, status, "媒体下载失败或内容为空"
            )
        return data

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
        await self.adapter._send_c2c_input_notify(self.session_id)

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
        if scene != "c2c" or not self.adapter.enable_streaming:
            buffer = MessageChain()
            async for chain in generator:
                buffer.chain.extend(chain.chain)
            if buffer.chain:
                await self.send(buffer)
            return

        controller = QQStreamingController(
            self.adapter.client,
            self.session_id,
            self.message_obj.message_id,
            on_completed=lambda ret, content: self.adapter._store_ref_index(
                ret, content, "c2c"
            ),
        )
        # AstrBot yields per-token deltas, but the ported controller expects
        # cumulative snapshots (openclaw onPartialReply contract). Accumulate
        # raw Plain text here and sanitize once on the whole buffer so
        # inter-chunk spaces survive.
        accumulated = ""
        async for chain in generator:
            if getattr(chain, "type", None) == "break":
                await controller.end_segment()
                accumulated = ""
                continue
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
            accumulated += "".join(
                comp.text for comp in chain.chain if isinstance(comp, Plain)
            )
            full = _sanitize_qq_text(accumulated)
            if not full:
                continue
            await controller.on_partial(full)
        await controller.finalize()
        if controller.should_fallback_to_static and controller.last_seen_text:
            buffer = MessageChain()
            buffer.chain.append(Plain(controller.last_seen_text))
            await self.send(buffer)

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
    "url_direct_upload": True,
    "enable_streaming": True,
    "enable_c2c_typing": True,
    "user_agent_suffix": "",
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
    "appid": {
        "type": "string",
        "description": "QQ bot AppID",
        "hint": "QQ 开放平台机器人的 AppID。",
        "default": "",
    },
    "secret": {
        "type": "string",
        "description": "QQ bot secret",
        "hint": "QQ 开放平台机器人的 AppSecret，用于获取 token 与 Webhook 签名。",
        "secret": True,
        "show_key": True,
        "default": "",
    },
    "is_sandbox": {
        "type": "bool",
        "description": "Use sandbox API",
        "hint": "使用沙箱环境（测试机器人）。",
        "default": False,
    },
    "use_markdown": {
        "type": "bool",
        "description": "Prefer native markdown messages when possible",
        "hint": "优先使用原生 Markdown 消息（需平台审核通过，失败自动降级纯文本）。",
        "default": True,
    },
    "intents": {
        "type": "list",
        "description": "Gateway intent aliases",
        "hint": "WebSocket 网关监听的事件类型别名列表。",
        "default": DEFAULT_CONFIG["intents"],
    },
    "intent_mask": {
        "type": "string",
        "description": "Raw gateway intent mask override",
        "hint": "原始 intent 位掩码（如 1075318862 或 0x40200C00），设置后覆盖 intents 列表。",
        "default": "",
    },
    "api_base_url": {
        "type": "string",
        "description": "API base URL override",
        "hint": "REST API 基础地址覆盖，留空使用官方地址。",
        "default": "",
    },
    "gateway_url": {
        "type": "string",
        "description": "WebSocket gateway URL override",
        "hint": "网关地址覆盖，留空自动从 API 获取。",
        "default": "",
    },
    "chunked_upload_threshold": {
        "type": "int",
        "description": "Chunked upload threshold in bytes",
        "hint": "本地文件超过该大小时走分片上传（默认 20MB）。",
        "default": 20971520,
    },
    "session_store_path": {
        "type": "string",
        "description": "Session store path override",
        "hint": "会话持久化 JSON 路径覆盖，留空使用 data 目录下默认路径。",
        "default": "",
    },
    "url_direct_upload": {
        "type": "bool",
        "description": "Pass public URLs directly to QQ platform",
        "hint": "公网 URL 直传 QQ 平台自行拉取；关闭时插件先下载再 Base64 上传（适用于 QQ 无法访问目标 URL 的场景）。",
        "default": True,
    },
    "enable_streaming": {
        "type": "bool",
        "description": "Enable C2C streaming replies",
        "hint": "启用私聊流式输出（仅 C2C 场景支持，关闭时流式内容缓冲后整条发送）。",
        "default": True,
    },
    "enable_c2c_typing": {
        "type": "bool",
        "description": "Auto send C2C typing indicator",
        "hint": "收到私聊消息时自动发送“正在输入”状态。",
        "default": True,
    },
    "user_agent_suffix": {
        "type": "string",
        "description": "User-Agent suffix",
        "hint": "追加在 HTTP User-Agent 尾部，用于私有化部署标识。",
        "default": "",
    },
}

WEBHOOK_CONFIG_METADATA = {
    "unified_webhook_mode": {
        "type": "bool",
        "description": "Use AstrBot unified webhook server",
        "hint": "使用 AstrBot 统一 Webhook 服务（需填写 webhook_uuid）。",
        "default": True,
    },
    "webhook_uuid": {
        "type": "string",
        "description": "Unified webhook UUID",
        "hint": "统一 Webhook 回调路径的 UUID，在平台配置页查看回调地址。",
        "default": "",
    },
    "verify_webhook_signature": {
        "type": "bool",
        "description": "Verify webhook Ed25519 signature",
        "hint": "校验 Webhook 回调的 Ed25519 签名。",
        "default": True,
    },
    "webhook_timestamp_tolerance_seconds": {
        "type": "int",
        "description": "Webhook timestamp tolerance in seconds",
        "hint": "回调时间戳容差（秒），0 表示不校验时间戳。",
        "default": 300,
    },
    "callback_server_host": {
        "type": "string",
        "description": "Standalone webhook server host",
        "hint": "独立 Webhook 服务监听地址。",
        "default": "0.0.0.0",
    },
    "port": {
        "type": "int",
        "description": "Standalone webhook server port",
        "hint": "独立 Webhook 服务监听端口。",
        "default": 6197,
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
        self.enable_streaming = bool(platform_config.get("enable_streaming", True))
        self.enable_c2c_typing = bool(platform_config.get("enable_c2c_typing", True))
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
            url_direct_upload=bool(platform_config.get("url_direct_upload", True)),
            user_agent_suffix=str(platform_config.get("user_agent_suffix") or ""),
        )
        self._sessions: dict[str, dict[str, Any]] = {}
        self._reply_limiter = ReplyLimiter()
        self._msg_id_cache = MsgIdCache()
        self._session_store_path = self._resolve_session_store_path(platform_config)
        self._ref_index = PersistedRefIndexStore(
            self._session_store_path.with_name("qqofficial_full_ref_index.jsonl")
        )
        self._load_sessions()
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._gateway_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()
        self._pending_event_extras: dict[str, dict[str, Any]] = {}
        self._typing_tasks: set[asyncio.Task] = set()

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
        return (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "qqofficial_full_sessions.json"
        )

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
            try:
                return int(raw_mask, 0)
            except ValueError:
                logger.warning(
                    "[QQOfficialFull] Invalid intent_mask %r, falling back to intents list",
                    raw_mask,
                )
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
        chunks = []
        for media_chunk in self._split_chain_by_media(message_chain):
            chunks.extend(self._split_chain_by_length(media_chunk))
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

        passive_degraded = False
        passive_msg_id = reply_message_id or reply_id
        if from_event and scene in {"group", "c2c"} and passive_msg_id:
            allowed, _remaining, reason = self._reply_limiter.check_limit(
                passive_msg_id
            )
            if allowed:
                self._reply_limiter.record(passive_msg_id)
            else:
                logger.info(
                    "[QQOfficialFull] Passive reply limit hit (%s) for %s, "
                    "degrading to proactive send",
                    reason,
                    passive_msg_id,
                )
                passive_degraded = True

        payload = await self._build_send_payload(
            message_chain,
            text,
            media,
            passive_msg_id,
            keyboard,
            raw_payload,
            session_info,
            scene,
            from_event and not passive_degraded,
        )
        if passive_degraded:
            payload.pop("msg_id", None)
            payload.pop("message_reference", None)
        cached_passive_id: str | None = None
        if not from_event and scene in {"group", "c2c"} and not reply_id:
            cached_passive_id = self._msg_id_cache.get(scene, target_id)
            if cached_passive_id:
                allowed, _remaining, _reason = self._reply_limiter.check_limit(
                    cached_passive_id
                )
                if allowed:
                    self._reply_limiter.record(cached_passive_id)
                    payload["msg_id"] = cached_passive_id
                else:
                    cached_passive_id = None
        ret = await self._send_payload_with_fallback(scene, target_id, payload, text)
        sent_id = self._extract_message_id(ret)
        if sent_id:
            self.remember_session(
                session.session_id,
                scene=scene,
                target_id=target_id,
                message_id=sent_id,
            )
        self._store_ref_index(
            ret,
            text or self._media_label_for_chain(message_chain),
            scene,
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
                    "msg_type": 7,
                    "media": {"file_info": media.get("file_info")},
                }
            )
            if text:
                payload["content"] = text
        elif scene in {"channel", "dm"}:
            payload.setdefault("content", text)
        elif use_markdown:
            try:
                text = await _enhance_markdown_images(self.client.http, text)
            except Exception as exc:
                logger.debug("[QQOfficialFull] Markdown image probe failed: %s", exc)
            payload.update({"markdown": {"content": text}, "msg_type": 2})
        else:
            payload.update({"content": text, "msg_type": 0})

        if scene in {"group", "c2c"}:
            payload.setdefault("msg_seq", _next_msg_seq_global())
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
                if isinstance(component, AtAll) or str(component.qq) == "all":
                    # QQ Bot API has no @all capability; drop it silently.
                    logger.debug("[QQOfficialFull] @all is unsupported, skipped")
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
        text = _sanitize_qq_text("".join(text_parts))
        return text, media, reply_id, keyboard, raw_payload or None

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
            try:
                return await self.client.upload_media(
                    scope, target_id, VOICE_FILE_TYPE, source
                )
            except QQOfficialAPIError as exc:
                if source.startswith(("base64://", "data:")):
                    file_name = "voice"
                else:
                    file_name = Path(str(source)).name or "voice"
                logger.warning(
                    "[QQOfficialFull] Voice upload failed (%s), sending as file",
                    exc,
                )
                return await self.client.upload_media(
                    scope,
                    target_id,
                    FILE_FILE_TYPE,
                    source,
                    file_name=file_name,
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

    def _split_chain_by_length(
        self, message_chain: MessageChain, limit: int = TEXT_CHUNK_LIMIT
    ) -> list[MessageChain]:
        """Split an over-long text chunk at line boundaries, tables intact.

        Args:
            message_chain: A single media-scoped message chain.
            limit: Max plain-text characters per QQ message.

        Returns:
            One or more message chains.
        """
        plain_texts = [c for c in message_chain.chain if isinstance(c, Plain)]
        total = sum(len(c.text) for c in plain_texts)
        if total <= limit:
            return [message_chain]
        combined = "".join(c.text for c in plain_texts)
        non_plain = [c for c in message_chain.chain if not isinstance(c, Plain)]
        segments = _chunk_text(combined, limit)
        out: list[MessageChain] = []
        for index, segment in enumerate(segments):
            comps: list[BaseMessageComponent] = list(non_plain) if index == 0 else []
            comps.append(Plain(segment))
            out.append(message_chain.derive(comps))
        return out

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

    def _store_ref_index(
        self, ret: Any, content: str, scope: str, *, is_bot: bool = True
    ) -> None:
        """Persist REFIDX key of an API response for quote lookups.

        Args:
            ret: QQ send/response payload.
            content: Display content of the referenced message.
            scope: QQ scene name.
            is_bot: Whether the entry was sent by this bot.
        """
        if not isinstance(ret, dict):
            return
        ext_info = ret.get("ext_info") or {}
        ref_key = str(ext_info.get("ref_idx") or ret.get("msg_idx") or "")
        if not ref_key:
            return
        message_id = ret.get("id") or ret.get("message_id") or ""
        self._ref_index.set(
            ref_key,
            {
                "messageId": str(message_id),
                "content": content or "",
                "senderId": str(ext_info.get("sender_id") or self.appid),
                "senderName": str(ext_info.get("sender_name") or self.appid),
                "timestamp": ret.get("timestamp")
                or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "isBot": is_bot,
                "scope": scope,
            },
        )

    @staticmethod
    def _media_label_for_chain(message_chain: MessageChain) -> str:
        """Return a bracketed label for the first media component in a chain.

        Args:
            message_chain: AstrBot message chain.

        Returns:
            Media label such as 图片, or empty string.
        """
        for component in message_chain.chain:
            if isinstance(component, Image):
                return "[图片]"
            if isinstance(component, Record):
                return "[语音]"
            if isinstance(component, Video):
                return "[视频]"
            if isinstance(component, File):
                return "[文件]"
        return ""

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
            content = _strip_mention_text(content, payload.get("mentions") or [])
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
            self._msg_id_cache.cache("group", group_id, abm.message_id)
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
            self._msg_id_cache.cache("c2c", sender_id, abm.message_id)
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
            ref_key = str((payload.get("ext_info") or {}).get("ref_idx") or "")
            entry = self._ref_index.get(ref_key) if ref_key else None
            if not entry:
                return None
            content = str(entry.get("content") or "")
            chain: list[BaseMessageComponent] = []
            if content:
                chain.append(Plain(content))
            return Reply(
                id=str(entry.get("messageId") or ref_key),
                chain=chain,
                sender_id=str(entry.get("senderId") or ""),
                sender_nickname=str(entry.get("senderName") or ""),
                message_str=content,
            )
        ref = msg_elements[0]
        chain: list[BaseMessageComponent] = []
        text = _parse_face_message(str(ref.get("content") or "")).strip()
        ref_key = str(ref.get("msg_idx") or "")
        if not text and ref_key:
            entry = self._ref_index.get(ref_key)
            if entry:
                text = str(entry.get("content") or "")
        if text and ref_key:
            author = ref.get("author") or {}
            self._ref_index.set(
                ref_key,
                {
                    "messageId": str(ref.get("id") or ref_key),
                    "content": text,
                    "senderId": str(
                        author.get("member_openid")
                        or author.get("user_openid")
                        or author.get("id")
                        or ""
                    ),
                    "senderName": str(author.get("username") or ""),
                    "timestamp": ref.get("timestamp")
                    or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "isBot": False,
                    "scope": "",
                },
            )
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
                # Prefer QQ's ready-made WAV link (skips SILK->WAV conversion);
                # carry the platform's built-in ASR text when available.
                wav_url = _normalize_url(
                    cast(str | None, _attr(attachment, "voice_wav_url", None))
                )
                record_url = wav_url or url
                asr_text = str(_attr(attachment, "asr_refer_text", "") or "").strip()
                components.append(
                    Record(file=record_url, url=record_url, text=asr_text or None)
                )
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
            interaction_id = str(payload.get("id") or event_id or "")
            if interaction_id:
                try:
                    await self.client.acknowledge_interaction(interaction_id)
                except Exception as exc:
                    logger.warning("[QQOfficialFull] interaction ack failed: %s", exc)
            abm = await self._parse_interaction_event(payload, interaction_id)
            if abm:
                self.commit_event(self.create_event(abm))
            return
        abm = await self._parse_message_event(event_type, payload, event_id)
        if (
            event_type == "C2C_MESSAGE_CREATE"
            and self.enable_c2c_typing
            and abm.session_id
        ):
            task = asyncio.create_task(self._send_c2c_input_notify(abm.session_id))
            self._typing_tasks.add(task)
            task.add_done_callback(self._typing_tasks.discard)
        self.commit_event(self.create_event(abm))

    async def _send_c2c_input_notify(self, openid: str) -> None:
        """Send QQ C2C input notification, swallowing failures.

        Args:
            openid: C2C target openid.
        """
        try:
            await self.client.send_v2_message(
                "c2c",
                openid,
                {
                    "msg_type": 6,
                    "input_notify": {"input_type": 1, "input_second": 60},
                    "msg_seq": _next_msg_seq_global(),
                },
            )
        except Exception as exc:
            logger.debug("[QQOfficialFull] typing notify failed: %s", exc)

    async def _parse_interaction_event(
        self, payload: dict, event_id: str
    ) -> AstrBotMessage | None:
        """Convert a QQ interaction (button click) into an AstrBot message.

        Args:
            payload: Interaction event payload.
            event_id: QQ dispatch event id.

        Returns:
            Normalized AstrBot message, or None when unusable.
        """
        data = payload.get("data") or {}
        resolved = data.get("resolved") or {}
        operator_id = str(
            payload.get("user_openid")
            or resolved.get("user_id")
            or resolved.get("user_openid")
            or payload.get("openid")
            or ""
        )
        group_id = str(payload.get("group_openid") or "")
        abm = AstrBotMessage()
        abm.raw_message = payload
        abm.timestamp = int(time.time())
        abm.message_id = str(payload.get("id") or event_id)
        abm.message_str = ""
        abm.message = [Json(data={"interaction": payload})]
        abm.self_id = "qq_official_full"
        if group_id:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = group_id
            abm.session_id = group_id
        elif operator_id:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = operator_id
        else:
            return None
        abm.sender = MessageMember(operator_id, "")
        self._pending_event_extras[abm.message_id] = {
            "qq_scene": self.session_scene(abm.session_id) or "unknown",
            "event_id": event_id,
            "raw_event_type": "INTERACTION_CREATE",
            "qq_interaction": payload,
        }
        return abm

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
        try:
            async with websockets.connect(
                gateway_url, max_size=10 * 1024 * 1024
            ) as websocket:
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
                            try:
                                await self._dispatch_payload_event(
                                    event_type, data, payload.get("id")
                                )
                            except Exception:
                                logger.exception(
                                    "[QQOfficialFull] Gateway event dispatch "
                                    "failed for %s",
                                    event_type,
                                )
                    elif op == OP_RECONNECT:
                        logger.info("[QQOfficialFull] Gateway requested reconnect.")
                        return
                    elif op == OP_INVALID_SESSION:
                        if payload.get("d"):
                            logger.warning(
                                "[QQOfficialFull] Session invalidated but "
                                "resumable; reconnecting with resume."
                            )
                        else:
                            logger.warning(
                                "[QQOfficialFull] Session invalidated; "
                                "re-identifying with a fresh token."
                            )
                            self._session_id = None
                            self._last_seq = None
                            self.client.clear_token()
                        return
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

    async def run(self) -> None:
        """Run the QQ WebSocket gateway loop."""
        reconnect_delay = 1.0
        while not self._shutdown_event.is_set():
            started_at = time.monotonic()
            try:
                await self._gateway_once()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                try:
                    reconnect_delay = self._handle_gateway_close(
                        QQGatewayClosed(exc.code, exc.reason), reconnect_delay
                    )
                except QQGatewayClosed as fatal:
                    logger.error(
                        "[QQOfficialFull] Gateway closed fatally (%s %s): "
                        "intents not allowed, stop reconnecting. Please fix "
                        "bot intents/permissions then restart.",
                        fatal.code,
                        fatal.reason,
                    )
                    await self._shutdown_event.wait()
                    return
                logger.warning(
                    "[QQOfficialFull] Gateway closed: %s, reconnecting in %.1fs",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)
                continue
            except Exception as exc:
                logger.warning(
                    "[QQOfficialFull] Gateway error: %s, reconnecting in %.1fs",
                    exc,
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)
                continue
            # Clean return (server asked to reconnect / connection closed
            # without an exception). Guard against quick-disconnect loops.
            if time.monotonic() - started_at < 5.0:
                logger.warning(
                    "[QQOfficialFull] Gateway disconnected quickly, "
                    "waiting %.1fs before reconnect",
                    GATEWAY_RATE_LIMIT_DELAY,
                )
                await asyncio.sleep(GATEWAY_RATE_LIMIT_DELAY)
            else:
                reconnect_delay = 1.0

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
        **WEBHOOK_CONFIG_METADATA,
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
        self._webhook_rate: OrderedDict[str, list[float]] = OrderedDict()

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

    def _client_ip(self, request: Any) -> str:
        """Best-effort extraction of the client IP from a webhook request.

        Args:
            request: Webhook request wrapper.

        Returns:
            Client IP string or unknown marker.
        """
        headers = getattr(request, "headers", {}) or {}
        forwarded = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        if forwarded:
            return str(forwarded).split(",")[0].strip()
        for attr in ("client_host", "remote_addr", "remote"):
            value = getattr(request, attr, None)
            if value:
                return str(value)
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return str(host) if host else "unknown"

    def _rate_allow(self, key: str) -> bool:
        """Fixed-window rate limiter for webhook ingress.

        Args:
            key: Client identity (IP).

        Returns:
            Whether the request is within the window quota.
        """
        now = time.time()
        slot = self._webhook_rate.get(key)
        if slot is None or now - slot[1] >= WEBHOOK_RATE_WINDOW_SECONDS:
            while len(self._webhook_rate) >= WEBHOOK_RATE_MAX_KEYS:
                self._webhook_rate.popitem(last=False)
            self._webhook_rate[key] = [1.0, now]
            return True
        slot[0] += 1
        return slot[0] <= WEBHOOK_RATE_MAX_REQUESTS

    async def webhook_callback(self, request: Any) -> Any:
        """Handle a unified QQ webhook callback.

        Args:
            request: AstrBot webhook request wrapper or compatible object.

        Returns:
            QQ webhook acknowledgement or error tuple.
        """
        if not self._rate_allow(self._client_ip(request)):
            logger.warning("[QQOfficialFull] Webhook ingress rate limited")
            return {"error": "Too Many Requests"}, 429
        body = await _maybe_await(request.get_data())
        if isinstance(body, str):
            body = body.encode("utf-8")
        if len(body) > WEBHOOK_MAX_BODY_BYTES:
            return {"error": "Payload Too Large"}, 413
        request_headers = getattr(request, "headers", {}) or {}
        content_type = str(
            request_headers.get("Content-Type")
            or request_headers.get("content-type")
            or ""
        )
        if content_type and "json" not in content_type.lower():
            return {"error": "Unsupported Media Type"}, 415
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
            headers = request_headers
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
