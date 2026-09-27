# share/

Everything the peer agent may read and pass on. Nothing else is readable to it.

- One file per topic. First lines: the date it reflects and who released it.
- `*.md` here is embedded in the agent's prompt at start (`inline_grounding`), so a
  new file takes effect on the next restart.
- No customer internals, nothing private, no credentials. A file here is a release.

This README is embedded as well, so keep it short and free of anything you would not share.
