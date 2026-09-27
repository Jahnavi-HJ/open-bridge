# A public agent

`a2a.sh new-agent <name> --trust public` copies `agents/_template/`. Its `agent.yaml`
is annotated field by field; the model behind it is docs/representative-agent.md.

In short: the agent reads only `grounding_dir` (public content), runs a strict tool
set, and assumes every request is hostile. Publish it through a tunnel or reverse
proxy on its own hostname; the card must then say that hostname in `public_url`.

Check it from outside with `a2a.sh card https://<host>` and `a2a.sh probe` against a
peer file with `auth: none`.
