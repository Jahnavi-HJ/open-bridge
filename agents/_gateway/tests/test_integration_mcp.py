"""Integration tests: real MCP client <-> real gateway app, fully in-process
(SPEC.md § 7 test plan, TRN-1..3, TIER e2e, CONV e2e).

Wiring (verified hands-on against mcp==2.2.0; first written against 1.28.1):

- ``build_server(...).streamable_http_app()`` returns the Starlette ASGI app.
- The app MUST run inside the session manager's lifespan —
  ``server.session_manager.run()`` — otherwise every request dies
  with ``RuntimeError: Task group is not initialized. Make sure to use run().``
  (``httpx2.ASGITransport`` never runs a lifespan itself.) The ``gateway``
  fixture owns that lifespan in a dedicated background task: pytest-asyncio
  (1.x) executes fixture setup and teardown in DIFFERENT asyncio tasks, and
  the anyio cancel scope inside ``run()`` must enter and exit in the SAME
  task — a plain ``async with`` across the fixture ``yield`` can therefore
  never complete teardown under the SPEC § 7 pinned test command.
- MCPServer auto-enables DNS-rebinding protection for host 127.0.0.1 with
  ``allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`` — the wildcard
  patterns require an explicit port in the Host header, so the in-process
  base URL must carry one (a portless or foreign host 421s).
- ``streamable_http_client`` takes a ready ``httpx2.AsyncClient`` (mcp 2.x
  dropped the 1.x client factory); built on an ``ASGITransport``, so no real
  socket ever opens. Result fields are snake_case in 2.x
  (``structured_content``, ``output_schema``, ``is_error``).

Hermetic: the upstream A2A side is a FakeA2A (in-process ASGI as well); the
client token list is injected via monkeypatch on GATEWAY_AUTH_TOKENS — dummy
values only, never real secrets.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx2
import pytest
from conftest import GOOD_TOKEN, two_tier_registry
from fake_a2a import FakeA2A
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gateway.config import GatewayConfig
from gateway.server import build_server

# Explicit port required — see module docstring (DNS-rebinding allowlist).
BASE_URL = "http://127.0.0.1:8900"

EXPECTED_TOOLS = {"list_bridges", "get_bridge_card", "ask_bridge"}

ASK_RESULT_KEYS = {"ok", "bridge", "conversation", "text", "error"}


@pytest.fixture
async def gateway(monkeypatch, make_http):
    """A running in-process gateway app + its FakeA2A upstream.

    The session-manager lifespan is owned by a dedicated background task (see
    module docstring): its anyio cancel scope then enters AND exits in that
    one task, which pytest-asyncio's split setup/teardown tasks cannot offer.
    Semantics are identical to ``async with server.session_manager.run():``.
    """
    monkeypatch.setenv("GATEWAY_AUTH_TOKENS", GOOD_TOKEN)
    fake = FakeA2A()
    server = build_server(
        GatewayConfig(), two_tier_registry(), http=make_http(fake)
    )
    app = server.streamable_http_app()

    started = asyncio.Event()
    stop = asyncio.Event()

    async def lifespan_owner() -> None:
        async with server.session_manager.run():
            started.set()
            await stop.wait()

    owner = asyncio.create_task(lifespan_owner())
    await started.wait()
    try:
        yield app, fake
    finally:
        stop.set()
        await owner


@asynccontextmanager
async def mcp_session(app, headers: dict[str, str] | None = None):
    """An initialized ClientSession against the gateway app, in-process."""

    # mcp 2.x takes a ready httpx2 client instead of a factory; the ASGI
    # transport keeps the whole exchange in-process.
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE_URL,
        headers=headers,
    )
    async with client, streamable_http_client(
        f"{BASE_URL}/mcp", http_client=client
    ) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# TRN-1/2 — three tools, each with an outputSchema
# ---------------------------------------------------------------------------


async def test_list_tools_exposes_exactly_three_tools_with_output_schema(gateway):
    app, _fake = gateway

    async with mcp_session(app) as session:
        tools = (await session.list_tools()).tools

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.output_schema is not None, f"{tool.name} lacks outputSchema"


# ---------------------------------------------------------------------------
# TIER-4/5 end-to-end — anonymous session
# ---------------------------------------------------------------------------


async def test_anonymous_session_hides_authenticated_only_bridge(gateway):
    app, _fake = gateway

    async with mcp_session(app) as session:
        result = await session.call_tool("list_bridges", {})

    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["tier"] == "anonymous"
    assert [b["id"] for b in payload["bridges"]] == ["open-fake"]


async def test_anonymous_ask_on_authenticated_only_bridge_returns_tier_denied(
    gateway,
):
    app, fake = gateway

    async with mcp_session(app) as session:
        result = await session.call_tool(
            "ask_bridge", {"bridge": "secure-fake", "message": "hello"}
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["ok"] is False
    assert payload["error"]["code"] == "tier_denied"
    # The upstream bridge must not have been contacted (TIER-5).
    assert fake.requests == []


# ---------------------------------------------------------------------------
# TIER-2 + TRN-2/3 end-to-end — authenticated session
# ---------------------------------------------------------------------------


async def test_authenticated_session_sees_all_bridges_and_ask_succeeds(gateway):
    app, _fake = gateway
    headers = {"Authorization": f"Bearer {GOOD_TOKEN}"}

    async with mcp_session(app, headers=headers) as session:
        listed = await session.call_tool("list_bridges", {})
        asked = await session.call_tool(
            "ask_bridge", {"bridge": "secure-fake", "message": "hello"}
        )

    listed_payload = listed.structured_content
    assert listed_payload is not None
    assert listed_payload["ok"] is True
    assert listed_payload["tier"] == "authenticated"
    assert sorted(b["id"] for b in listed_payload["bridges"]) == [
        "open-fake",
        "secure-fake",
    ]

    asked_payload = asked.structured_content
    assert asked_payload is not None
    # structuredContent present and shaped exactly like AskBridgeResult (TRN-2).
    assert set(asked_payload.keys()) == ASK_RESULT_KEYS
    assert asked_payload["ok"] is True
    assert asked_payload["bridge"] == "secure-fake"
    assert asked_payload["text"] == "artifact reply text"
    assert asked_payload["conversation"] == "ctx-fake-1"
    assert asked_payload["error"] is None


# ---------------------------------------------------------------------------
# TIER-3 end-to-end — bad token never downgrades silently
# ---------------------------------------------------------------------------


async def test_bad_token_session_gets_unauthorized_envelope_from_every_tool(
    gateway,
):
    app, fake = gateway
    headers = {"Authorization": "Bearer not-a-configured-token"}

    async with mcp_session(app, headers=headers) as session:
        calls = {
            "list_bridges": await session.call_tool("list_bridges", {}),
            "get_bridge_card": await session.call_tool(
                "get_bridge_card", {"bridge": "open-fake"}
            ),
            "ask_bridge": await session.call_tool(
                "ask_bridge", {"bridge": "open-fake", "message": "hello"}
            ),
        }

    for tool_name, result in calls.items():
        payload = result.structured_content
        assert payload is not None, f"{tool_name} returned no structuredContent"
        assert payload["ok"] is False, f"{tool_name} did not fail"
        assert payload["error"]["code"] == "unauthorized", tool_name
    assert fake.requests == []


# ---------------------------------------------------------------------------
# CONV-1/2 end-to-end — multi-turn keeps one contextId on the wire
# ---------------------------------------------------------------------------


async def test_multi_turn_ask_reuses_the_same_context_id_on_the_wire(gateway):
    app, fake = gateway

    async with mcp_session(app) as session:
        first = await session.call_tool(
            "ask_bridge", {"bridge": "open-fake", "message": "first turn"}
        )
        conversation = first.structured_content["conversation"]
        assert conversation == "ctx-fake-1"

        second = await session.call_tool(
            "ask_bridge",
            {
                "bridge": "open-fake",
                "message": "second turn",
                "conversation": conversation,
            },
        )

    assert second.structured_content["ok"] is True
    assert second.structured_content["conversation"] == conversation

    rpc_messages = [
        r["body"]["params"]["message"] for r in fake.requests if r["path"] == "/rpc"
    ]
    assert len(rpc_messages) == 2
    # CONV-1: the opening ask sends NO contextId — the upstream generates it.
    assert "contextId" not in rpc_messages[0]
    # CONV-2: the follow-up carries the returned conversation verbatim, so the
    # fake saw the very same contextId again on the wire.
    assert rpc_messages[1]["contextId"] == conversation


# ---------------------------------------------------------------------------
# allowed_hosts — Host allowlist behind a tunnel (SPEC § 3 + § 7 421 footnote)
# ---------------------------------------------------------------------------


async def test_allowed_hosts_config_serves_under_public_tunnel_hostname(
    monkeypatch, make_http
):
    """With ``allowed_hosts=["gw.test"]`` the app answers under
    ``http://gw.test`` — exactly the request shape a tunnel produces (public
    hostname in the Host header, gateway bound to 127.0.0.1) that the SDK's
    localhost auto-protection would otherwise reject with HTTP 421.

    Self-contained wiring (own lifespan task + client factory) so the shared
    ``gateway`` fixture / ``mcp_session`` helper stay untouched.
    """
    monkeypatch.setenv("GATEWAY_AUTH_TOKENS", GOOD_TOKEN)
    fake = FakeA2A()
    server = build_server(
        GatewayConfig(allowed_hosts=("gw.test",)),
        two_tier_registry(),
        http=make_http(fake),
    )
    app = server.streamable_http_app()
    tunnel_base = "http://gw.test"

    started = asyncio.Event()
    stop = asyncio.Event()

    async def lifespan_owner() -> None:
        async with server.session_manager.run():
            started.set()
            await stop.wait()

    owner = asyncio.create_task(lifespan_owner())
    await started.wait()
    try:
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=tunnel_base
        )
        async with client, streamable_http_client(
            f"{tunnel_base}/mcp", http_client=client
        ) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool("list_bridges", {})
    finally:
        stop.set()
        await owner

    payload = result.structured_content
    assert payload is not None
    assert payload["ok"] is True
    assert [b["id"] for b in payload["bridges"]] == ["open-fake"]
