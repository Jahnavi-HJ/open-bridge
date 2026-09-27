"""Regression tests for the Bridge-Agent claude -p runner (``_runtime.runner``).

These lock the runner's **"always answer, never hang"** contract — the public agent
must yield exactly one non-empty answer within its deadline, no matter how the
``claude -p`` subprocess behaves. Two production incidents motivate them:

1. 2026-06-06 — a ``Read`` tool_result embeds the whole grounding CV (~100+ KiB) as a
   single NDJSON line, blowing past asyncio's DEFAULT 64 KiB StreamReader line limit
   → ``readline()`` raised ``LimitOverrunError`` → the turn became a textless FAILED.
   Fixed by spawning the subprocess with ``limit=_STREAM_LIMIT`` (64 MiB).
   → ``test_oversized_grounding_line_still_answers``.

2. 2026-06-09 — a heavy query streamed events continuously and the request ran long;
   the overall deadline must bound it so the server returns a graceful answer instead
   of running until the client/tunnel drops it (→ visitor sees no text).
   → ``test_deadline_bounds_blocking_stream`` + ``test_deadline_bounds_flooding_stream``.

The subprocess is stubbed (no real ``claude`` binary, no network), so these run fast
and deterministically in CI. The fake honours the ``limit=`` the runner passes to
``create_subprocess_exec`` — so reverting ``_STREAM_LIMIT`` makes the oversized-line
test fail, which is the point.
"""
from __future__ import annotations

import asyncio
import json
import time

from _runtime.runner import (
    _DISALLOWED_TOOLS,
    _PRIVATE_DISALLOWED_TOOLS,
    SubprocessClaudeRunner,
)


# ---------------------------------------------------------------------------
# Fakes — a controllable stand-in for the claude -p subprocess
# ---------------------------------------------------------------------------

def _line(obj: dict) -> bytes:
    """One NDJSON stream-json line as the runner reads them."""
    return (json.dumps(obj) + "\n").encode()


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_tool(name: str, **inp) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _result(text: str) -> dict:
    return {"type": "result", "result": text}


def _stream_text_delta(text: str) -> dict:
    """A partial-message text delta (``--include-partial-messages``)."""
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }}


def _stream_tool_arg_delta(partial_json: str) -> dict:
    """A partial-message TOOL-argument delta — carries no answer text."""
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }}


class _FakeProc:
    """Minimal stand-in for an ``asyncio`` subprocess: just what the runner touches."""

    def __init__(self, stdout, stderr=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _BlockingReader:
    """``readline()`` never returns — models a subprocess that produces nothing."""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _FloodReader:
    """``readline()`` always returns a line immediately and never EOFs — models a
    subprocess that streams without end. Proves the *loop-top* deadline check bounds
    the runner even when ``readline`` itself never blocks (the 2026-06-09 class)."""

    def __init__(self, line: bytes):
        self._line = line

    async def readline(self) -> bytes:
        await asyncio.sleep(0.001)
        return self._line


def _streamreader(lines: list[bytes], *, limit: int):
    r = asyncio.StreamReader(limit=limit)
    for ln in lines:
        r.feed_data(ln)
    r.feed_eof()
    return r


def _patch(monkeypatch, *, reader_factory=None, oserror=False):
    """Patch ``asyncio.create_subprocess_exec`` to return a fake proc.

    ``reader_factory(limit)`` builds the stdout stream and receives the exact ``limit``
    the runner passed — so the fake's buffering matches production.
    """
    async def fake_exec(*args, limit=None, **kwargs):
        if oserror:
            raise OSError("simulated spawn failure")
        stdout = reader_factory(limit)
        return _FakeProc(stdout, stderr=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _runner(timeout: float = 30.0, *, trust: str = "public") -> SubprocessClaudeRunner:
    return SubprocessClaudeRunner(
        binary="claude",
        model="test-model",
        system_prompt="system",
        working_dir=".",
        timeout=timeout,
        trust=trust,
    )


async def _collect(
    runner: SubprocessClaudeRunner, prompt: str = "hi", *, context_id: str | None = None
) -> list[dict]:
    return [evt async for evt in runner.stream(prompt, context_id=context_id)]


def _answers(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "answer"]


# ---------------------------------------------------------------------------
# Contract: exactly one non-empty answer, for any subprocess behaviour
# ---------------------------------------------------------------------------

async def test_normal_stream_yields_the_result_text(monkeypatch):
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line(_assistant_text("thinking")), _line(_result("Hello, here is the answer."))],
        limit=limit,
    ))
    events = await _collect(_runner())
    answers = _answers(events)
    assert len(answers) == 1, "exactly one answer event expected"
    assert answers[0]["text"] == "Hello, here is the answer."


async def test_tool_use_emits_a_step(monkeypatch):
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line(_assistant_tool("Read", file_path="config.cv.toml")), _line(_result("done"))],
        limit=limit,
    ))
    events = await _collect(_runner())
    steps = [e for e in events if e.get("kind") == "step"]
    assert any("config.cv.toml" in s.get("label", "") for s in steps)
    assert _answers(events)[0]["text"] == "done"


async def test_oversized_grounding_line_still_answers(monkeypatch):
    """The 2026-06-06 incident: a single >64 KiB NDJSON line (the CV Read result)
    must NOT crash the stream. Guards that ``_STREAM_LIMIT`` stays large enough — if
    reverted to the 64 KiB default, the fake reader (built with the runner's limit)
    raises and the real answer is replaced by a fallback, failing this assertion."""
    huge_blob = "x" * (256 * 1024)  # 256 KiB — well past the 64 KiB default
    tool_result_line = _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": huge_blob}
    ]}})
    real_answer = "Yes — based on the CV, here is the grounded answer."
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [tool_result_line, _line(_assistant_text("…")), _line(_result(real_answer))],
        limit=limit,
    ))
    answers = _answers(await _collect(_runner()))
    assert len(answers) == 1
    assert answers[0]["text"] == real_answer, "oversized line must not swallow the real answer"


async def test_no_result_event_falls_back_to_assistant_text(monkeypatch):
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line(_assistant_text("Partial answer with no terminal result event."))],
        limit=limit,
    ))
    answers = _answers(await _collect(_runner()))
    assert len(answers) == 1
    assert answers[0]["text"] == "Partial answer with no terminal result event."


async def test_empty_stdout_yields_nonempty_message(monkeypatch):
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader([], limit=limit))
    answers = _answers(await _collect(_runner()))
    assert len(answers) == 1
    assert answers[0]["text"], "must yield a non-empty fallback, never blank"


async def test_deadline_bounds_blocking_stream(monkeypatch):
    """A subprocess that produces nothing must not hang past the timeout."""
    _patch(monkeypatch, reader_factory=lambda limit: _BlockingReader())
    start = time.monotonic()
    answers = _answers(await _collect(_runner(timeout=0.2)))
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"runner hung {elapsed:.1f}s past its 0.2s deadline"
    assert len(answers) == 1 and answers[0]["text"]


async def test_deadline_bounds_flooding_stream(monkeypatch):
    """The 2026-06-09 class: a subprocess streaming continuously (readline never
    blocks) must STILL be bounded by the overall deadline, not run unbounded until
    the client drops the connection."""
    flood = _line(_assistant_text("x"))
    _patch(monkeypatch, reader_factory=lambda limit: _FloodReader(flood))
    start = time.monotonic()
    answers = _answers(await _collect(_runner(timeout=0.3)))
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"continuous stream ran {elapsed:.1f}s — deadline not enforced"
    assert len(answers) == 1 and answers[0]["text"]


async def test_spawn_failure_yields_message_not_crash(monkeypatch):
    _patch(monkeypatch, oserror=True)
    answers = _answers(await _collect(_runner()))
    assert len(answers) == 1
    assert answers[0]["text"], "spawn failure must degrade to a message, not raise"


async def test_always_exactly_one_answer_across_modes(monkeypatch):
    """The executor delivers whatever the single answer event carries — so the runner
    must yield exactly one, never zero (no text) and never several (duplicate bubbles)."""
    cases = [
        [_line(_result("a"))],
        [_line(_assistant_text("b"))],
        [],
        [_line(_assistant_text("c")), _line(_result("d"))],
    ]
    for lines in cases:
        _patch(monkeypatch, reader_factory=lambda limit, _l=lines: _streamreader(_l, limit=limit))
        assert len(_answers(await _collect(_runner()))) == 1


# ---------------------------------------------------------------------------
# Incremental streaming: partial-message text deltas (perceived-latency fix)
# ---------------------------------------------------------------------------

def test_include_partial_messages_flag_only_in_stream_cmd():
    """Streaming asks claude -p for partial messages; the buffered path must not.
    Mutation guard: drop the flag and incremental rendering silently dies."""
    r = _runner()
    assert "--include-partial-messages" in r._build_cmd("hi", stream=True)
    assert "--include-partial-messages" not in r._build_cmd("hi", stream=False)


async def test_partial_message_deltas_stream_incrementally(monkeypatch):
    """Text deltas are forwarded as growing ``delta`` snapshots — each a prefix of the
    final answer — so the widget renders the answer building up instead of all at once."""
    a, b, c = "x" * 50, "y" * 50, "z" * 50
    final = a + b + c
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line(_stream_text_delta(a)), _line(_stream_text_delta(b)),
         _line(_stream_text_delta(c)), _line(_result(final))],
        limit=limit,
    ))
    events = await _collect(_runner())
    deltas = [e for e in events if e.get("kind") == "delta"]
    assert deltas, "expected incremental delta events from partial messages"
    lengths = [len(d["text"]) for d in deltas]
    assert lengths == sorted(lengths), "snapshots must grow monotonically"
    for d in deltas:
        assert final.startswith(d["text"]), "every snapshot is a prefix of the full answer"
    # the terminal result stays the single authoritative answer
    answers = _answers(events)
    assert len(answers) == 1 and answers[0]["text"] == final


# ---------------------------------------------------------------------------
# AGENT_CONTEXT_ID: the A2A context_id exported into the subprocess env, so
# instance tools (argparse CLIs, inherited env) can identify the session that
# is calling them (e.g. intake/concern notifications carrying a real session
# id instead of "unknown").
# ---------------------------------------------------------------------------

async def test_context_id_is_exported_into_subprocess_env(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, env=None, limit=None, **kwargs):
        captured["env"] = env
        stdout = _streamreader([_line(_result("ok"))], limit=limit)
        return _FakeProc(stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _collect(_runner(), context_id="ctx-abc-123")

    env = captured["env"]
    assert env is not None, "context_id was supplied — env must be set"
    assert env.get("AGENT_CONTEXT_ID") == "ctx-abc-123"


async def test_no_context_id_means_no_agent_context_id_in_env(monkeypatch):
    """No empty-string placeholder either — the key must be absent entirely."""
    captured: dict = {}

    async def fake_exec(*args, env=None, limit=None, **kwargs):
        captured["env"] = env
        stdout = _streamreader([_line(_result("ok"))], limit=limit)
        return _FakeProc(stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await _collect(_runner(), context_id=None)

    env = captured["env"]
    assert env is None or "AGENT_CONTEXT_ID" not in env


async def test_tool_argument_deltas_do_not_stream_as_text(monkeypatch):
    """Only assistant TEXT deltas stream; tool-argument (input_json) deltas carry no
    answer text and must never leak into the visible bubble."""
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line(_stream_tool_arg_delta('{"file_path":"config.cv.toml"}')),
         _line(_result("grounded answer"))],
        limit=limit,
    ))
    events = await _collect(_runner())
    assert not [e for e in events if e.get("kind") == "delta"]
    assert _answers(events)[0]["text"] == "grounded answer"


# ---------------------------------------------------------------------------
# AGENT_CONTEXT_ID env injection — 2026-07-18 incident: report_concern's mail said
# "Sitzung: unbekannt" because the model has no reliable way to know its own
# context_id. The runtime now injects it into the claude -p subprocess env, which
# is inherited by the tool subprocesses (book_request.py / report_concern.py) it
# spawns via Bash — so the tools can read AGENT_CONTEXT_ID themselves instead of
# depending on the model to pass --context-id correctly.
# ---------------------------------------------------------------------------

def _capture_env_exec(monkeypatch, captured: dict, *, lines: list[bytes]):
    async def fake_exec(*args, env=None, limit=None, **kwargs):
        captured["env"] = env
        return _FakeProc(_streamreader(lines, limit=limit))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


async def test_stream_injects_agent_context_id_into_subprocess_env(monkeypatch):
    captured: dict = {}
    _capture_env_exec(monkeypatch, captured, lines=[_line(_result("ok"))])
    events = [evt async for evt in _runner().stream("hi", context_id="ctx-abc-123")]
    assert captured["env"] is not None
    assert captured["env"].get("AGENT_CONTEXT_ID") == "ctx-abc-123"
    assert _answers(events)[0]["text"] == "ok"


async def test_stream_without_context_id_omits_env_var(monkeypatch):
    """No context_id known (e.g. a caller that predates this feature) → the subprocess
    env is untouched, never a fabricated/placeholder value.

    Asserted as a contract, not as an implementation: CORE signals "untouched" by
    passing ``env=None`` (asyncio then inherits the parent env natively) rather than
    by handing down an explicit ``os.environ`` copy. Both satisfy the guarantee that
    matters — no placeholder reaches the child — so pinning either shape would make
    this test break on a legitimate CORE refactor, which is exactly what it did."""
    captured: dict = {}
    _capture_env_exec(monkeypatch, captured, lines=[_line(_result("ok"))])
    events = [evt async for evt in _runner().stream("hi")]
    env = captured["env"]
    assert env is None or "AGENT_CONTEXT_ID" not in env
    assert _answers(events)[0]["text"] == "ok"


async def test_call_injects_agent_context_id_into_subprocess_env(monkeypatch):
    """The buffered (__call__) path gets the same env treatment as stream()."""
    captured: dict = {}

    class _CommProc(_FakeProc):
        async def communicate(self):
            return json.dumps({"result": "buffered ok"}).encode(), b""

    async def fake_exec(*args, env=None, **kwargs):
        captured["env"] = env
        return _CommProc(None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    answer = await _runner().__call__("hi", context_id="ctx-buffered-9")
    assert captured["env"].get("AGENT_CONTEXT_ID") == "ctx-buffered-9"
    assert answer == "buffered ok"


def test_spawn_debug_line_never_carries_the_prompt():
    """The visitor's question must not reach a log line, not even at DEBUG.

    `_build_cmd` places the prompt at cmd[2]; the earlier `" ".join(cmd[:6])`
    therefore logged it in full. It stayed unnoticed because the service runs at
    INFO — one LOG_LEVEL away from shipping visitor text to whatever sink the
    runtime exports to.
    """
    from _runtime.runner import _describe

    secret = "Ich heisse Erika Musterfrau und suche einen Termin am Dienstag"
    cmd = ["/usr/bin/claude", "-p", secret, "--model", "sonnet",
           "--output-format", "json", "--append-system-prompt", "PERSONA"]
    line = _describe(cmd)
    assert secret not in line
    assert "Erika" not in line
    assert "PERSONA" not in line
    assert "sonnet" in line and "claude" in line


# ---------------------------------------------------------------------------
# Trust profile: public (default, byte-identical to pre-feature behavior) vs
# private (relaxed) argv shape. config.py owns fail-closed RESOLUTION of the
# `trust:` / AGENT_TRUST value into "public"/"private" (see test_config.py);
# these lock what the runner does with an already-resolved value, including the
# runner's OWN independent fail-closed fallback — belt-and-suspenders so a bad
# value can never widen settings even via a future direct construction.
# ---------------------------------------------------------------------------

def test_default_trust_is_public_argv_identical_to_explicit_public():
    """No trust kwarg at all (every caller before this feature) must produce the
    exact same argv as an explicit trust="public" — the regression this whole
    feature must not cause."""
    default_cmd = _runner()._build_cmd("hi", stream=False)
    explicit_public_cmd = _runner(trust="public")._build_cmd("hi", stream=False)
    assert default_cmd == explicit_public_cmd


def test_public_trust_argv_matches_pre_trust_shape():
    cmd = _runner(trust="public")._build_cmd("hi", stream=False)
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--disallowedTools") + 1] == _DISALLOWED_TOOLS
    assert "--resume" not in cmd


def test_unknown_trust_value_falls_back_to_public_argv():
    cmd = _runner(trust="pivate")._build_cmd("hi", stream=False)  # typo, not "private"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--disallowedTools") + 1] == _DISALLOWED_TOOLS


def test_blank_trust_value_falls_back_to_public_argv():
    cmd = _runner(trust="")._build_cmd("hi", stream=False)
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--disallowedTools") + 1] == _DISALLOWED_TOOLS


def test_private_trust_widens_setting_sources():
    cmd = _runner(trust="private")._build_cmd("hi", stream=False)
    assert cmd[cmd.index("--setting-sources") + 1] == "user,project"


def test_private_trust_uses_a_narrower_but_still_explicit_denylist():
    cmd = _runner(trust="private")._build_cmd("hi", stream=False)
    deny = cmd[cmd.index("--disallowedTools") + 1]
    assert deny == _PRIVATE_DISALLOWED_TOOLS
    assert deny != _DISALLOWED_TOOLS, "private must not reuse the public denylist verbatim"
    # local file / search / VCS work within its own repo is relaxed...
    for binary in ("cat", "grep", "sed", "git", "diff", "tar"):
        assert f"Bash({binary}:*)" not in deny, f"{binary} should be relaxed for private trust"
    # ...but real recon/secret-store/egress primitives stay blocked either way
    for binary in ("curl", "ssh", "security", "sqlite3", "openssl", "wget"):
        assert f"Bash({binary}:*)" in deny, f"{binary} must stay denied even for private trust"


def test_private_trust_appends_resume_flag_with_session_id():
    cmd = _runner(trust="private")._build_cmd(
        "hi", stream=False, resume_session_id="sess-123"
    )
    assert cmd[cmd.index("--resume") + 1] == "sess-123"


def test_private_trust_without_session_id_omits_resume_flag():
    cmd = _runner(trust="private")._build_cmd("hi", stream=False)
    assert "--resume" not in cmd


def test_public_trust_ignores_resume_session_id():
    """--resume is a private-trust-only mechanism — a caller passing a session id
    to a public runner must not get it appended to argv."""
    cmd = _runner(trust="public")._build_cmd(
        "hi", stream=False, resume_session_id="sess-123"
    )
    assert "--resume" not in cmd


async def test_private_trust_stream_yields_session_id_event(monkeypatch):
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line({"type": "result", "result": "answer", "session_id": "sess-abc"})],
        limit=limit,
    ))
    events = await _collect(_runner(trust="private"))
    session_events = [e for e in events if e.get("kind") == "session_id"]
    assert session_events == [{"kind": "session_id", "id": "sess-abc"}]
    assert _answers(events)[0]["text"] == "answer"


async def test_public_trust_stream_never_yields_session_id_event(monkeypatch):
    """Even if claude -p happened to emit a session_id, a public agent must not
    surface it — session resume is a private-trust-only capability."""
    _patch(monkeypatch, reader_factory=lambda limit: _streamreader(
        [_line({"type": "result", "result": "answer", "session_id": "sess-abc"})],
        limit=limit,
    ))
    events = await _collect(_runner(trust="public"))
    assert not [e for e in events if e.get("kind") == "session_id"]
