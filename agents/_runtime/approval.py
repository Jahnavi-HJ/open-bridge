"""Owner approval: an answer leaves only after its owner released it.

Declared in ``agent.yaml``::

    approval:
      command: ["python3", "${tools_dir}/approve.py"]   # argv, no shell
      timeout_sec: 3600                                   # how long the owner may take

The runtime stays transport-agnostic. It starts ``command`` once per finished
answer, writes one JSON object to its stdin::

    {"task_id", "context_id", "peer", "question", "answer", "timeout_sec"}

and reads one JSON object from its stdout::

    {"decision": "approve"}                       send the answer as drafted
    {"decision": "edit", "text": "..."}           send the owner's text instead
    {"decision": "reject"}                        send nothing
    {"decision": "timeout"}                       the owner did not answer in time

How the owner is asked (a messenger, a push notification, a web page) is the
command's business and lives with the instance. Anything the runtime cannot read
as one of those four, a crash, or silence past the deadline, fails closed: the
answer is not sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

VERDICTS = frozenset({"approve", "edit", "reject", "timeout"})
# Grace on top of the owner's time, for the command to start and report its own timeout.
_GRACE_SEC = 30.0


@dataclass(frozen=True)
class ApprovalConfig:
    command: tuple[str, ...] = ()
    timeout_sec: float = 3600.0

    @property
    def enabled(self) -> bool:
        return bool(self.command)


@dataclass(frozen=True)
class Decision:
    verdict: str            # approve | edit | reject | timeout | error
    text: str = ""


def parse_approval(instance: str, spec: dict | None, *, tools_dir: str) -> ApprovalConfig:
    """Resolve the ``approval:`` block; raise on anything that would not run."""
    if not spec:
        return ApprovalConfig()
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise ValueError(
            f"agent '{instance}': approval.command must be a non-empty argv list, "
            "for example [\"python3\", \"${tools_dir}/approve.py\"]"
        )
    timeout = float(spec.get("timeout_sec", 3600))
    if timeout <= 0:
        raise ValueError(f"agent '{instance}': approval.timeout_sec must be positive")
    return ApprovalConfig(
        command=tuple(a.replace("${tools_dir}", tools_dir) for a in command),
        timeout_sec=timeout,
    )


class CommandApprover:
    """Ask the owner through ``cfg.command``; never raises, fails closed."""

    def __init__(self, cfg: ApprovalConfig):
        self._cfg = cfg

    async def __call__(self, request: dict) -> Decision:
        payload = json.dumps({**request, "timeout_sec": self._cfg.timeout_sec}, ensure_ascii=False)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._cfg.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("approval: cannot start the approver")
            return Decision("error")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")),
                timeout=self._cfg.timeout_sec + min(_GRACE_SEC, self._cfg.timeout_sec / 10),
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("approval: approver silent past the deadline")
            return Decision("timeout")
        except asyncio.CancelledError:
            proc.kill()
            raise
        if proc.returncode != 0:
            logger.warning(
                "approval: approver exited %s: %s", proc.returncode,
                err.decode("utf-8", "replace").strip()[:300],
            )
            return Decision("error")
        try:
            data = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError, UnicodeDecodeError):
            logger.warning("approval: approver answered no JSON")
            return Decision("error")
        verdict = str(data.get("decision", "")).lower()
        text = str(data.get("text") or "").strip()
        if verdict not in VERDICTS or (verdict == "edit" and not text):
            logger.warning("approval: unusable decision %r", data)
            return Decision("error")
        return Decision(verdict, text)
