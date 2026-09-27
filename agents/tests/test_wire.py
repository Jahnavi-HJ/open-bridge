"""The JSON-RPC wire end to end: HTTP request → SDK request handler → executor.

Every other suite here tests one piece: the card, the executor with a hand-built
context, the runner against a fake subprocess. None of them sends a real JSON-RPC
request through the app the SDK builds, and that layer is the one an SDK upgrade
changes most (a2a-sdk 1.1.4 alone reworked cancel/subscribe ownership, event-queue
teardown and task listing). These tests drive ``build_app`` over HTTP with only the
``claude`` subprocess replaced, so a bump that breaks sending, streaming, reading
back or cancelling a task fails here instead of in front of a visitor.

Hermetic: no network, no ``claude`` binary. The fake runner stands in for
``SubprocessClaudeRunner`` and speaks its event contract (step, delta, answer).
"""
from __future__ import annotations

import asyncio
import json
import time

import _runtime.server as server_module
import pytest
from _runtime.config import load_agent_config
from _runtime.server import build_app
from a2a.utils import DEFAULT_RPC_URL
from starlette.testclient import TestClient

ANSWER = "Antwort aus dem Test-Runner."
V1 = {"A2A-Version": "1.0"}


class _FakeRunner:
    """Replaces ``SubprocessClaudeRunner``: same constructor surface, no subprocess."""

    block = False      # class-level switch: hold the turn open so a cancel can land
    interrupted = False  # set when the held turn is really stopped, not just relabelled

    def __init__(self, **_kwargs):
        pass

    async def __call__(self, prompt, *, context_id=None, resume_session_id=None):
        return ANSWER

    async def stream(self, prompt, *, context_id=None, resume_session_id=None):
        yield {"kind": "step", "tool": "Read", "label": "Lese Unterlagen"}
        if _FakeRunner.block:
            try:
                await asyncio.sleep(30)   # cancelled by CancelTask long before this ends
            except asyncio.CancelledError:
                _FakeRunner.interrupted = True
                raise
        yield {"kind": "delta", "text": ANSWER[:10]}
        yield {"kind": "answer", "text": ANSWER}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server_module, "SubprocessClaudeRunner", _FakeRunner)
    _FakeRunner.block = False
    _FakeRunner.interrupted = False
    app = build_app(load_agent_config("_template", environment="test"))
    with TestClient(app) as c:   # keeps one event loop alive across requests
        yield c


def _rpc(client, method, params, headers=V1):
    response = client.post(
        DEFAULT_RPC_URL,
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def _v1_message(text="Wofür bist du zuständig?", message_id="m-1"):
    return {"message": {"role": "ROLE_USER", "messageId": message_id, "parts": [{"text": text}]}}


def _artifact_texts(task):
    return [p.get("text", "") for a in task.get("artifacts", []) for p in a.get("parts", [])]


def test_send_message_returns_a_completed_task_with_the_answer(client):
    task = _rpc(client, "SendMessage", _v1_message())["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert ANSWER in _artifact_texts(task)
    assert task["id"] and task["contextId"]


def test_the_0_3_wire_still_answers(client):
    params = {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": "m-legacy",
            "parts": [{"kind": "text", "text": "Hallo"}],
        }
    }
    result = _rpc(client, "message/send", params, headers={})
    assert result["kind"] == "task"
    assert result["status"]["state"] == "completed"
    texts = [p.get("text", "") for a in result.get("artifacts", []) for p in a.get("parts", [])]
    assert ANSWER in texts


def test_streaming_sends_progress_then_the_answer_then_completes(client):
    events = []
    with client.stream(
        "POST",
        DEFAULT_RPC_URL,
        json={"jsonrpc": "2.0", "id": "1", "method": "SendStreamingMessage", "params": _v1_message()},
        headers={**V1, "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:])["result"])

    kinds = [next(iter(e)) for e in events]
    assert "artifactUpdate" in kinds, kinds
    final = [e["statusUpdate"] for e in events if "statusUpdate" in e][-1]
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    streamed = [
        p.get("text", "")
        for e in events if "artifactUpdate" in e
        for p in e["artifactUpdate"]["artifact"].get("parts", [])
    ]
    assert ANSWER in streamed


def test_a_finished_task_can_be_read_back(client):
    task = _rpc(client, "SendMessage", _v1_message())["task"]
    again = _rpc(client, "GetTask", {"id": task["id"]})
    assert again["id"] == task["id"]
    assert again["status"]["state"] == "TASK_STATE_COMPLETED"


def test_a_running_task_can_be_cancelled(client):
    _FakeRunner.block = True
    params = {**_v1_message(message_id="m-cancel"), "configuration": {"returnImmediately": True}}
    task = _rpc(client, "SendMessage", params)["task"]
    assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")

    # The turn starts in the background; wait until the executor has registered it.
    deadline = time.monotonic() + 5
    while True:
        state = _rpc(client, "GetTask", {"id": task["id"]})["status"]["state"]
        if state == "TASK_STATE_WORKING" or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert state == "TASK_STATE_WORKING"
    assert not _FakeRunner.interrupted   # still running before the cancel

    cancelled = _rpc(client, "CancelTask", {"id": task["id"]})
    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"
    assert _rpc(client, "GetTask", {"id": task["id"]})["status"]["state"] == "TASK_STATE_CANCELED"

    # The state alone is not enough: the SDK reports CANCELED even when the turn keeps
    # running underneath (that is what a stuck ``claude`` process would look like). The
    # runner itself must have been stopped. Measured 2026-09-27: on a2a-sdk 1.1.x the
    # SDK cancels the running ``execute`` itself, so this holds even without the
    # executor's own ``run_task.cancel()``; that call is a second line, not the only one.
    deadline = time.monotonic() + 5
    while not _FakeRunner.interrupted and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _FakeRunner.interrupted, "CancelTask reported CANCELED but the turn kept running"
