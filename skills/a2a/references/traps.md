# Traps, each one measured

- **A client that reads only the top-level `protocolVersion` cannot read a 1.x
  card** and drops the peer without a word. Read `supportedInterfaces` first.
- **An error text can arrive as an answer.** A `claude` process without credentials
  ended with "Not logged in" as its result; runtimes before the is_error fix passed
  it on as COMPLETED. Treat a suspiciously short answer as a finding.
- **A cancel test that only checks the state passes on a stuck process.** a2a-sdk
  1.1.x cancels the running `execute` itself; assert that the worker stopped.
- **Tokens and ssh on macOS:** the login keychain is neither readable nor writable
  from an ssh session. Start services under launchd; seed tokens from a GUI-context
  job.
- **Behind a CDN, a script can get 403 or 429** where a browser gets 200 (bot rules,
  rate limits). Test cards with a real user agent and not in a tight loop.
- **The card is public.** Skill examples and descriptions are readable without a
  token; do not put names of deals, customers or people into them.
- **A new share file takes effect on restart** when it is embedded via
  `inline_grounding`.
