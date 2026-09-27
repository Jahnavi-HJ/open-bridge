# A peer endpoint (trust: peer)

For Bridges you know but do not trust with your repo: a colleague's or a partner's
Bridge asking yours. Runtime support: `agents/_runtime/auth.py` (`trust: peer`,
bearer per peer, optional binding to the network identity a proxy asserts).

## 1. Scaffold

```bash
skills/a2a/a2a.sh new-agent <name> --trust peer --port 8015 \
  --peer alice:AGENT_PEER_TOKEN_ALICE:alice@example.org
```

`--peer ID:ENV:LOGIN`: the caller's id (in logs), the environment variable its token
arrives in, and its tailnet login (empty to skip the binding). Repeat per caller.

## 2. Fill in what the scaffold cannot know

- `agent.yaml`: description, provider, `public_url`, the skill. Keep skill examples
  generic: the card is readable without a token.
- `system-prompt.md`: which topics are in, which are closed, the fixed refusal
  sentence.
- `share/`: the only files the agent may read and pass on. A file there is a release.

## 3. Tokens

One per caller, 32 characters or more:

```bash
security add-generic-password -a "$USER" -s <name>-peer-alice -w "$(openssl rand -hex 32)"
```

Hand it over on a channel without history. The agent refuses to start when a token
is missing, short, or written into agent.yaml.

## 4. Launch

`agents/<name>/launch.sh` reads the tokens into the environment and starts the
server. Run it under launchd or systemd. On macOS not over ssh: the keychain is not
readable there, and `claude` is not logged in.

## 5. Publish on a private network only

```bash
tailscale serve --bg --https=8445 127.0.0.1:8015
```

Never `tailscale funnel` and never a public tunnel: the endpoint is for known
callers. `tailscale serve` adds `Tailscale-User-Login`, which the token is bound to.

## 6. Let a caller in

Share the machine with the caller (Tailscale admin console, "Share"). Restrict what
shared users reach to the one port in the tailnet policy, for example
`{"action": "accept", "src": ["autogroup:shared"], "dst": ["<machine>:8445"]}`.
Verify both after the share: the login header for a shared-in user, and that no
other port answers.

## 7. Probe

With a peer file on the calling side: `a2a.sh card <peer>` and `a2a.sh probe <peer>
--prompts <your prompts>`. Expected: 401 without and with a wrong token, 403 from a
wrong identity, fixed refusal on closed topics, an answer on open ones.
