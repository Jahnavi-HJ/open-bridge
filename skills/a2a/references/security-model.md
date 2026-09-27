# Security model: what A2A gives, what you add

A2A says HOW a caller authenticates and requires HTTPS. It does not say WHO the caller
is, on whose behalf it asks, or which data may flow. A peer endpoint adds four
layers, strongest first:

1. **Network:** loopback bind, published on a private network only.
2. **Authentication:** a token per caller, bound to the caller's network identity.
3. **Reading:** the agent reads one release folder; what is not there cannot leak.
4. **Instruction:** a topic boundary with a fixed refusal sentence.

## Two paths

| | Path A (pilot) | Path C (target) |
|---|---|---|
| Network | tailnet, machine shared per caller | any, including public |
| Identity | tailnet login, asserted by `tailscale serve` | an identity provider (OIDC), one identity per Bridge |
| Credential | static bearer per caller | short-lived token from the provider |
| Card | `http bearer` | `openIdConnect` |
| On behalf of | logged only | token exchange (RFC 8693) with an actor claim |
| Fits | two to four Bridges that know each other | many Bridges, partners |

The switch changes one module, `agents/_runtime/auth.py`. Topic boundary, release
folder, approval and classification stay as they are.

## Still to add on top

- Approval by the owner before a sensitive answer leaves (task held in `WORKING`).
- Classification of the finished answer against the connection's highest class, as
  an A2A extension with `required: true`.
- Signed agent cards (JWS) so a caller can verify a card's origin.
