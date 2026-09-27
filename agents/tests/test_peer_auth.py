"""Peer authentication: a Bridge-Agent that only answers other Bridges it knows.

``trust: peer`` is the third trust profile. It is hardened exactly like ``public``
(cwd = a curated grounding dir, strict tool set), but the JSON-RPC endpoint refuses
every request that does not carry a known per-peer bearer token. Optionally the
token is additionally bound to a network identity asserted by a local proxy
(``Tailscale-User-Login`` from ``tailscale serve``), so a token alone, copied off
the peer's machine, is not enough, and a forged header alone is not enough either.

The card stays readable without a token (discovery), and declares the scheme, so a
client can see what it needs before it asks.
"""
from __future__ import annotations

import dataclasses

import pytest
from a2a.utils import DEFAULT_RPC_URL
from starlette.testclient import TestClient

import _runtime.config as config_module
import _runtime.server as server_module
from _runtime.auth import AuthConfig, Peer, parse_auth
from _runtime.card import build_agent_card
from _runtime.config import load_agent_config
from _runtime.server import build_app

TOKEN_AXEL = "a" * 40
TOKEN_OTHER = "b" * 40
V1 = {"A2A-Version": "1.0"}


class _FakeRunner:
    def __init__(self, **_kwargs):
        pass

    async def __call__(self, prompt, *, context_id=None, resume_session_id=None):
        return "ok"

    async def stream(self, prompt, *, context_id=None, resume_session_id=None):
        yield {"kind": "answer", "text": "ok"}


def _peer_cfg(**peer_overrides):
    cfg = load_agent_config("_template", environment="test")
    peer = Peer(id="axel", token=TOKEN_AXEL, **peer_overrides)
    return dataclasses.replace(cfg, trust="peer", auth=AuthConfig(mode="bearer", peers=(peer,)))


@pytest.fixture
def client_for(monkeypatch):
    monkeypatch.setattr(server_module, "SubprocessClaudeRunner", _FakeRunner)
    opened = []

    def make(cfg):
        c = TestClient(build_app(cfg))
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def _send(client, headers):
    return client.post(
        DEFAULT_RPC_URL,
        json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {
            "message": {"role": "ROLE_USER", "messageId": "m-1", "parts": [{"text": "Hallo"}]}
        }},
        headers={**V1, **headers},
    )


# --- the endpoint ---------------------------------------------------------------

def test_no_token_is_refused_with_401(client_for):
    r = _send(client_for(_peer_cfg()), {})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_wrong_token_is_refused_with_401(client_for):
    r = _send(client_for(_peer_cfg()), {"Authorization": f"Bearer {TOKEN_OTHER}"})
    assert r.status_code == 401


def test_malformed_authorization_is_refused_with_401(client_for):
    r = _send(client_for(_peer_cfg()), {"Authorization": f"Basic {TOKEN_AXEL}"})
    assert r.status_code == 401


def test_known_token_is_answered(client_for):
    r = _send(client_for(_peer_cfg()), {"Authorization": f"Bearer {TOKEN_AXEL}"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_bound_identity_must_match(client_for):
    client = client_for(_peer_cfg(tailscale_login="axel@example.org"))
    ok = _send(client, {"Authorization": f"Bearer {TOKEN_AXEL}", "Tailscale-User-Login": "Axel@Example.org"})
    assert ok.status_code == 200, ok.text


def test_token_without_the_bound_identity_is_refused_with_403(client_for):
    client = client_for(_peer_cfg(tailscale_login="axel@example.org"))
    missing = _send(client, {"Authorization": f"Bearer {TOKEN_AXEL}"})
    wrong = _send(client, {"Authorization": f"Bearer {TOKEN_AXEL}", "Tailscale-User-Login": "eve@example.org"})
    assert missing.status_code == 403
    assert wrong.status_code == 403


def test_identity_header_without_token_is_refused(client_for):
    # A local process can forge the proxy header; without the token it gets nothing.
    client = client_for(_peer_cfg(tailscale_login="axel@example.org"))
    r = _send(client, {"Tailscale-User-Login": "axel@example.org"})
    assert r.status_code == 401


def test_card_stays_readable_without_a_token(client_for):
    r = client_for(_peer_cfg()).get("/.well-known/agent-card.json")
    assert r.status_code == 200


def test_an_open_agent_is_unchanged(client_for):
    cfg = load_agent_config("_template", environment="test")
    r = _send(client_for(cfg), {})
    assert r.status_code == 200, r.text


# --- the card ---------------------------------------------------------------------

def test_card_declares_bearer_when_auth_is_on():
    card = build_agent_card(_peer_cfg())
    scheme = card.security_schemes["bearer"].http_auth_security_scheme
    assert scheme.scheme.lower() == "bearer"
    assert [list(r.schemes) for r in card.security_requirements] == [["bearer"]]


def test_card_declares_nothing_for_an_open_agent():
    card = build_agent_card(load_agent_config("_template", environment="test"))
    assert len(card.security_schemes) == 0
    assert len(card.security_requirements) == 0


# --- config: fail closed ----------------------------------------------------------

def test_parse_auth_reads_tokens_from_the_environment(monkeypatch):
    monkeypatch.setenv("PEER_TOKEN_AXEL", TOKEN_AXEL)
    auth = parse_auth("x", {"mode": "bearer", "peers": [
        {"id": "axel", "token_env": "PEER_TOKEN_AXEL", "tailscale_login": "axel@example.org"}
    ]})
    assert auth.enabled
    assert auth.peers == (Peer(id="axel", token=TOKEN_AXEL, tailscale_login="axel@example.org"),)


def test_parse_auth_without_block_is_off():
    assert not parse_auth("x", None).enabled


@pytest.mark.parametrize("spec", [
    {"mode": "magic"},                                                   # unknown mode
    {"mode": "bearer", "peers": []},                                     # nobody may call
    {"mode": "bearer", "peers": [{"id": "axel", "token_env": "UNSET_PEER_TOKEN"}]},  # token missing
    {"mode": "bearer", "peers": [{"id": "axel", "token_env": "SHORT_PEER_TOKEN"}]},  # token too weak
    {"mode": "bearer", "peers": [{"id": "axel", "token": TOKEN_AXEL}]},  # raw secret in the file
])
def test_parse_auth_refuses_to_start_on_a_bad_block(monkeypatch, spec):
    monkeypatch.delenv("UNSET_PEER_TOKEN", raising=False)
    monkeypatch.setenv("SHORT_PEER_TOKEN", "short")
    with pytest.raises(ValueError):
        parse_auth("x", spec)


def _write_instance(tmp_path, body):
    inst = tmp_path / "peertest"
    inst.mkdir()
    (inst / "agent.yaml").write_text(body, "utf-8")
    (inst / "system-prompt.md").write_text("Du bist ein Test.", "utf-8")
    return inst


def test_trust_peer_without_auth_refuses_to_start(monkeypatch, tmp_path):
    _write_instance(tmp_path, "name: t\ntrust: peer\n")
    monkeypatch.setattr(config_module, "AGENTS_DIR", tmp_path)
    with pytest.raises(ValueError):
        load_agent_config("peertest", environment="test")


def test_trust_peer_is_confined_like_public(monkeypatch, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("PEER_TOKEN_AXEL", TOKEN_AXEL)
    _write_instance(tmp_path, (
        f"name: t\ntrust: peer\ngrounding_dir: {share}\n"
        "auth:\n  mode: bearer\n  peers:\n    - id: axel\n      token_env: PEER_TOKEN_AXEL\n"
    ))
    monkeypatch.setattr(config_module, "AGENTS_DIR", tmp_path)
    cfg = load_agent_config("peertest", environment="test")
    assert cfg.trust == "peer"
    assert cfg.working_dir == str(share.resolve())   # never the repo root
    assert cfg.auth.enabled


def test_the_peer_id_reaches_the_executor_log(client_for, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="_runtime.executor")
    r = _send(client_for(_peer_cfg()), {"Authorization": f"Bearer {TOKEN_AXEL}"})
    assert r.status_code == 200, r.text
    peers = [getattr(rec, "peer", None) for rec in caplog.records if rec.msg == "executor: peer request"]
    assert peers == ["axel"]
