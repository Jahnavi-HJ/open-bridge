# Peers: whom this Bridge may call

One file per peer, `infra/a2a-peers/<name>.yaml`, from `_template.yaml` next to it.
An org overlay may ship its members' peers as `scope: org` files; your own additions
stay `scope: user`.

## Adding a peer

1. Get from the peer's owner: the card URL and, if the card declares `bearer`, a token
   they issued for you. Tokens travel over a channel with no history (a disappearing
   message, a shared vault entry), never mail or chat.
2. Put the token into your secret store yourself, for example:
   `security add-generic-password -a "$USER" -s <name>-peer-token -w` (prompts, so the
   value is not on the command line).
3. Write the peer file with `credential_ref: keychain://<name>-peer-token`.
4. Check: `a2a.sh card <name>`, then `a2a.sh ask <name> "What may I ask you?"`.

If the peer is on a private network (a tailnet), its owner shares the machine with
you first; see peer-agent.md, step 6, from the other side.

## Refusals you will see

| Answer | Meaning |
|---|---|
| 401 | no token, wrong token, or the peer does not know you |
| 403 | the token is right but arrived from another network identity than the one it is bound to |
| connection error | not on the peer's network, or the endpoint is down |
