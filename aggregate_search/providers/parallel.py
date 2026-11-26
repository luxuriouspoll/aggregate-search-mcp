"""Parallel AI 搜索与正文提取（https://parallel.ai）

通过官方 Streamable HTTP MCP 接入：
    https://search.parallel.ai/mcp
免注册、免 key 可用（Fast 模式，有限流）；注册 PARALLEL_API_KEY 后额度更高。
官方说明：匿名 MCP 免费；账号每月有免费额度，Turbo/Fast 最多约 5,000 次/月。

这里用普通 HTTP 客户端直接发 tools/call，不引入额外 SDK。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import blake2b
from secrets import token_bytes
from threading import Lock
from weakref import WeakKeyDictionary

import httpx

from .. import __version__
from ..config import get_settings
from .base import (
    NETWORK_ERROR,
    FetchResult,
    ProviderError,
    SearchResult,
    as_items,
    brief,
    business_error,
    decode_mcp_response,
    expect_dict,
    fetch_error,
    text_of,
)

MCP_URL = "https://search.parallel.ai/mcp"
_PROTOCOL_VERSION = "2025-06-18"
_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass(eq=False)
class _McpSession:
    session_id: str | None = None
    protocol_version: str | None = None
    renewal: asyncio.Task[None] | None = None


@dataclass
class _Channel:
    tool_session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session: _McpSession = field(default_factory=_McpSession)
    users: int = 0


# 空闲的 loop/client 不由会话缓存保活；每个客户端最多保留 32 个通道，活跃通道不淘汰。
_Channels = OrderedDict[bytes | None, _Channel]
_CHANNELS: WeakKeyDictionary[asyncio.AbstractEventLoop, WeakKeyDictionary[httpx.AsyncClient, _Channels]] = (
    WeakKeyDictionary()
)
_CHANNELS_LOCK = Lock()
_FINGERPRINT_SALT = token_bytes(32)
_MAX_CHANNELS = 32


def _trim_channels(channels: _Channels) -> None:
    while len(channels) > _MAX_CHANNELS:
        for fingerprint, channel in channels.items():
            if not channel.users:
                del channels[fingerprint]
                break
        else:
            break


@contextmanager
def _channel_for(client: httpx.AsyncClient, key: str) -> Iterator[_Channel]:
    loop = asyncio.get_running_loop()
    with _CHANNELS_LOCK:
        by_client = _CHANNELS.get(loop)
        if by_client is None:
            by_client = _CHANNELS[loop] = WeakKeyDictionary()
        channels = by_client.get(client)
        if channels is None:
            channels = by_client[client] = OrderedDict()

    # 同一 loop 的通道表只在该 loop 内读写；缓存不保存 Bearer key 或请求头。
    fingerprint = blake2b(key.encode(), key=_FINGERPRINT_SALT, digest_size=32).digest() if key else None
    channel = channels.get(fingerprint)
    if channel is None:
        channel = channels[fingerprint] = _Channel()
    channels.move_to_end(fingerprint)
    channel.users += 1
    try:
        _trim_channels(channels)
        yield channel
    finally:
        channel.users -= 1
        if not channel.users and (renewal := channel.session.renewal) is not None:
            # 不让取消或超时后的最后一次调用留下后台握手（也不保活 loop/client）。
            channel.session = _McpSession()
            renewal.cancel()
        _trim_channels(channels)


def _headers(key: str, session: _McpSession | None = None) -> dict[str, str]:
    headers = dict(_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if session is not None:
        if session.session_id:
            headers["Mcp-Session-Id"] = session.session_id
        if session.protocol_version:
            headers["MCP-Protocol-Version"] = session.protocol_version
    return headers


async def _post(
    client: httpx.AsyncClient, key: str, payload: dict, session: _McpSession | None = None,
) -> httpx.Response:
    headers = _headers(key, session)
    request = client.build_request(
        "POST", MCP_URL, headers=headers, json=payload, timeout=get_settings().SEARCH_TIMEOUT,
    )
    # 客户端可能共享或复用；只允许本通道明确设置的认证/会话头，不转发现有 Cookie。
    for header in ("Authorization", "Mcp-Session-Id", "MCP-Protocol-Version", "Cookie"):
        if header not in headers:
            request.headers.pop(header, None)
    # 不跟随跳转，避免认证或自定义 MCP 头被带离固定端点。
    return await client.send(request, auth=None, follow_redirects=False)


async def _handshake(client: httpx.AsyncClient, key: str) -> _McpSession:
    response = await _post(
        client,
        key,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "aggregate-search", "version": __version__},
            },
        },
    )
    data = decode_mcp_response(response)
    version = data.get("protocolVersion")
    session = _McpSession(
        session_id=response.headers.get("Mcp-Session-Id"),
        protocol_version=version if isinstance(version, str) and version else _PROTOCOL_VERSION,
    )
    notify = await _post(
        client, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session,
    )
    if notify.status_code not in (200, 202, 204):
        # 保留鉴权、限流及 Retry-After，交给上层正常换 key / 冷却。
        if notify.status_code >= 400:
            decode_mcp_response(notify)
        raise ProviderError(f"MCP initialized 通知失败: HTTP {notify.status_code}")
    return session


# `session_id` 是工具参数，不能仅凭参数校验报错就认定 MCP 会话失效。
_MCP_SESSION_NAME = r"(?:mcp[ -]session[ -]id|(?:mcp )?session id)"
_HANDSHAKE_FAILURE = re.compile(
    rf"\b(?:(?:missing|invalid|unknown|expired) (?:or (?:missing|invalid) )?(?:the )?{_MCP_SESSION_NAME}"
    r"(?! (?:argument|parameter|field)\b)"
    rf"|(?:{_MCP_SESSION_NAME}|(?:mcp )?session)(?: header)? (?:is |has (?:been )?)?"
    r"(?:missing|invalid|expired|required|not found|terminated|no longer valid)"
    r"|(?:missing|invalid|unknown|expired) session(?=\s*(?:[.!,'\"}]|$))"
    r"|no (?:valid )?session id (?:provided|supplied)"
    r"|not (?:yet )?initialized|initialize first"
    r"|(?:initialization|initialize(?: request)?|handshake) (?:is )?required"
    r"|(?:must|please) (?:first )?(?:initialize|send an? initializ(?:e|ation) request))\b",
    re.IGNORECASE,
)


def _wants_handshake(exc: ProviderError, session: _McpSession) -> bool:
    if not exc.sent or exc.status not in (0, 400, 404, 405, 409, 410, 412, 422):
        return False
    # MCP 规定：请求已携带会话 ID 时，404 表示会话结束；无会话头的普通 404 不算。
    if exc.status == 404 and session.session_id:
        return True
    return bool(_HANDSHAKE_FAILURE.search(str(exc)))


async def _ready_session(channel: _Channel, pending: asyncio.Task[None] | None) -> _McpSession:
    while pending is not None:
        # 单个协程的取消不能打断还在被其它调用等待的握手。
        await asyncio.shield(pending)
        pending = channel.session.renewal
    return channel.session


def _consume_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def _renew_session(
    client: httpx.AsyncClient, key: str, channel: _Channel, rejected: _McpSession,
) -> _McpSession:
    if channel.session is rejected and rejected.renewal is None:
        async def establish() -> None:
            fresh = _McpSession()
            try:
                fresh = await _handshake(client, key)
            finally:
                if channel.session is rejected:
                    channel.session = fresh

        # 同代际（包括迟到的失败）共享这次握手结果，不用等待工具调用完成。
        rejected.renewal = asyncio.create_task(establish())
        rejected.renewal.add_done_callback(_consume_failure)
    return await _ready_session(channel, rejected.renewal or channel.session.renewal)


async def _post_call(
    client: httpx.AsyncClient, key: str, session: _McpSession, name: str, arguments: dict,
) -> dict:
    response = await _post(
        client,
        key,
        {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session,
    )
    return decode_mcp_response(response)


async def _call_tool(client: httpx.AsyncClient, name: str, arguments: dict, key: str = "") -> dict:
    try:
        with _channel_for(client, key) as channel:
            arguments = {**arguments, "session_id": channel.tool_session_id}
            pending = channel.session.renewal
            session = await _ready_session(channel, pending)
            # 无状态快路径不加异步锁。每次调用最多试一次兜底（加入现有握手也算）。
            for can_renew in (pending is None, False):
                try:
                    result = await _post_call(client, key, session, name, arguments)
                    break
                except ProviderError as exc:
                    if not _wants_handshake(exc, session):
                        raise
                    if not can_renew:
                        if channel.session is session and session.renewal is None:
                            channel.session = _McpSession()
                        raise
                    session = await _renew_session(client, key, channel, session)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{NETWORK_ERROR}: {type(exc).__name__}: {exc}") from exc
    if result.get("isError"):
        message = str(result.get("content") or result)
        raise business_error(message) if name == "web_search" else fetch_error(message)
    return result


def _tool_json(result: dict) -> dict:
    content = result.get("content") or []
    if not isinstance(content, list):
        raise ProviderError("Parallel 工具响应格式异常")
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            raw = item.get("text")
            if raw is not None and not isinstance(raw, str):
                raise ProviderError("Parallel 工具内容不是文本")
            try:
                return expect_dict(json.loads(raw or "{}"), "parallel")
            except ValueError as exc:
                raise ProviderError("Parallel 工具内容不是 JSON") from exc
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    raise ProviderError("Parallel 工具没有返回文本内容")


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    # MCP 版没有 max_results 参数，返回后本地截断。
    raw = await _call_tool(
        client,
        "web_search",
        {"objective": query, "search_queries": [query]},
        key,
    )
    data = _tool_json(raw)
    results: list[SearchResult] = []
    for item in as_items(data, "results"):
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        excerpts = item.get("excerpts") or []
        if not isinstance(excerpts, list):
            excerpts = []
        snippet = "\n".join(str(part) for part in excerpts[:2])[:900]
        results.append(
            SearchResult(
                source="parallel",
                url=url,
                title=str(item.get("title") or "").strip(),
                snippet=snippet,
                rank=len(results),
                published=str(item.get("publish_date") or item.get("published_date") or ""),
            )
        )
        if len(results) >= max_results:
            break
    return results


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    raw = await _call_tool(
        client,
        "web_fetch",
        {"urls": [url], "full_content": True},
        key,
    )
    data = _tool_json(raw)
    items = as_items(data, "results") or as_items(data, "pages")
    if not items:
        raise ProviderError(f"Parallel 未返回正文: {brief(data)}")
    item = items[0]
    content = item.get("content") or item.get("markdown") or item.get("full_content") or ""
    if isinstance(content, list):
        content = "\n".join(part for part in (text_of(part) for part in content) if part)
    return FetchResult(
        provider="parallel",
        url=url,
        title=text_of(item.get("title")),
        content=text_of(content),
    )
