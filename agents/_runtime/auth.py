"""Peer authentication for a Bridge-Agent that answers other Bridges only.

Declared in ``agent.yaml``::

    trust: peer
    auth:
      mode: bearer                       # none (default) | bearer
      peers:
        - id: axel                       # shows up in logs as the caller
          token_env: AGENT_PEER_TOKEN_AXEL   # the token itself lives in the environment
          tailscale_login: axel@example.org  # optional: bind the token to a network identity

One seam, one decision: :class:`PeerAuthBackend` turns a request into a peer or
refuses it, and nothing else in the runtime knows how. Swapping bearer tokens for
OIDC later changes this module, not the server, the executor or the card shape.

Fail closed everywhere. A block the runtime cannot honour (unknown mode, no peer, a
missing or weak token, a raw token written into the file) stops the agent from
starting; it never degrades into an open endpoint.
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"none", "bearer"})
MIN_TOKEN_CHARS = 32

# Set by ``tailscale serve`` for requests from a tailnet (or shared-in) user. Only
# trustworthy because the agent binds to loopback behind that proxy, and even then
# only together with the token: any local process could set it.
TAILSCALE_LOGIN_HEADER = "tailscale-user-login"


@dataclass(frozen=True)
class Peer:
    id: str
    token: str
    tailscale_login: str | None = None

    def __repr__(self) -> str:  # never print the token
        return f"Peer(id={self.id!r}, tailscale_login={self.tailscale_login!r})"


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "none"
    peers: tuple[Peer, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode != "none"


def parse_auth(instance: str, spec: dict | None) -> AuthConfig:
    """Resolve the ``auth:`` block of ``agent.yaml``; raise on anything unsafe."""
    if not spec:
        return AuthConfig()
    mode = str(spec.get("mode", "none")).strip().lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"agent '{instance}': unknown auth.mode {mode!r} (none | bearer)")
    if mode == "none":
        return AuthConfig()

    peers: list[Peer] = []
    for raw in spec.get("peers") or []:
        peer_id = str(raw.get("id") or "").strip()
        if not peer_id:
            raise ValueError(f"agent '{instance}': auth peer without id")
        if "token" in raw:
            raise ValueError(
                f"agent '{instance}': peer '{peer_id}' has a raw token in agent.yaml; "
                "put it in the environment and name it with token_env"
            )
        env_name = str(raw.get("token_env") or "").strip()
        token = os.getenv(env_name, "") if env_name else ""
        if not token:
            raise ValueError(
                f"agent '{instance}': peer '{peer_id}' has no token (token_env {env_name!r} unset)"
            )
        if len(token) < MIN_TOKEN_CHARS:
            raise ValueError(
                f"agent '{instance}': token for peer '{peer_id}' is shorter than "
                f"{MIN_TOKEN_CHARS} characters"
            )
        login = str(raw.get("tailscale_login") or "").strip() or None
        peers.append(Peer(id=peer_id, token=token, tailscale_login=login))

    if not peers:
        raise ValueError(f"agent '{instance}': auth.mode bearer needs at least one peer")
    return AuthConfig(mode=mode, peers=tuple(peers))


class IdentityMismatch(AuthenticationError):
    """The token is valid, but it arrived from the wrong network identity."""


class PeerAuthBackend(AuthenticationBackend):
    """Map ``Authorization: Bearer <token>`` to a known peer, or refuse.

    No header → anonymous (the card and /health stay reachable; the JSON-RPC route
    is guarded separately). A header that does not resolve to a peer is an error,
    never a silent downgrade to anonymous.
    """

    def __init__(self, auth: AuthConfig):
        self._peers = auth.peers

    async def authenticate(self, conn: HTTPConnection):
        header = conn.headers.get("authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("malformed authorization")

        match = None
        for peer in self._peers:  # compare against every peer, no early exit
            if hmac.compare_digest(peer.token.encode(), token.encode()):
                match = peer
        if match is None:
            raise AuthenticationError("unknown token")

        if match.tailscale_login:
            seen = (conn.headers.get(TAILSCALE_LOGIN_HEADER) or "").strip().lower()
            if seen != match.tailscale_login.lower():
                logger.warning(
                    "peer_auth: token of %s from wrong identity %r", match.id, seen or None
                )
                raise IdentityMismatch("identity mismatch")

        return AuthCredentials(["peer"]), SimpleUser(match.id)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="bridge-agent"'},
    )


def on_auth_error(_conn: HTTPConnection, exc: Exception) -> JSONResponse:
    if isinstance(exc, IdentityMismatch):
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)
    logger.warning("peer_auth: refused (%s)", exc)
    return _unauthorized(str(exc))


def require_peer(endpoint):
    """Wrap a route endpoint so only an authenticated peer reaches it."""

    async def guarded(request):
        user = request.scope.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            return _unauthorized("token required")
        return await endpoint(request)

    return guarded
