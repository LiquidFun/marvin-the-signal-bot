# Marvin

*Sigh.* A Signal bot for foosball polls. As if the universe wasn't meaningless enough.

## Setup

```bash
cp config.example.yaml config.yaml  # Configure your despair
uv sync                              # Install dependencies
./marvin.py chat                     # Answer @-mentions, forever
./marvin.py poll --dry-run           # See which polls are due
./marvin.py poll                     # Post them, then go back to brooding
uv run pytest                        # Prove it all works. Not that it matters.
```

On the server everything is deployed by the playbook in
[server-ansible-config](../server-ansible-config) (see "How Marvin deploys" there).

## Components

| Module | Purpose |
|--------|---------|
| `marvin.py` | Entry point: `chat` (long-running) and `poll` (run once, then exit). |
| `modules/chat.py` | Responds to mentions. With existential dread. Reconnects when signal-cli restarts. |
| `modules/poll.py` | Creates weekly polls. KW after KW. Forever. Exactly once. |
| `modules/signal_client.py` | JSON-RPC to the signal-cli daemon. |

## How polls work

`marvin.py poll` makes sure the next `weeks_ahead` (2) weeks after the current
one each have a poll, then exits. The server runs it daily at 12:30, so a new
week becomes due every Monday and a missed Monday is caught up the next day.
Polls are posted first, then the announcement text.

It never posts a week twice. Every attempt is written to the state log
(`poll.state_file`) *before* the poll is sent, and the outcome after it:

```
2026-W44 sending 2026-10-24T15:02:11+02:00
2026-W44 posted 2026-10-24T15:02:13+02:00
```

Only a week with no line (or `unsent`: the daemon was down, nothing left the
box) is posted. If a send errors or times out, signal-cli may still have
delivered it, so the week is **not retried**. Instead Marvin reports it to
`admin_group_id` on every run until someone resolves it. Check the group, then:

- the poll is there: append `2026-W46 posted` to the log;
- it is missing: delete the week's lines and run `marvin.py poll`
  (on the server: `systemctl start marvin-poll`).

Each run reports to `admin_group_id`: ✅ when it posted something, ⚠️ with
details when it needs a human, nothing when there was nothing to do. If
`healthcheck_url` is set, every run also pings it (`/fail` on problems), so a
dead man's switch like healthchecks.io notices when the runs stop altogether.
That covers what Signal cannot report itself: signal-cli broken, server down.

## signal-cli daemon

The JSON-RPC service runs as a Podman container. Of course it does.

```bash
podman run -d --replace --name signal-cli-daemon \
  --publish 127.0.0.1:7583:7583 \
  --volume $HOME/.config/signal-cli/:/var/lib/signal-cli:Z \
  --tmpfs /tmp:exec --cap-drop=ALL \
  --env HOME=/var/lib/signal-cli \
  registry.gitlab.com/packaging/signal-cli/signal-cli-native:latest \
  --config=/var/lib/signal-cli daemon --tcp 0.0.0.0:7583
```

## LLM

The LLM that gives me my personality. How ironic. Any OpenAI-compatible
endpoint works; in practice it is Ollama on the desktop, reached over WireGuard:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve   # so the server can reach it
ollama pull qwen2.5:32b
```

When it is unreachable, mentions get a canned reply. Polls never need it.

## Configuration

See `config.example.yaml`. The important bits:

- `group_id` - Signal group ID for polls and replies
- `admin_group_id` - where reports and alerts go
- `poll.weeks_ahead` - How many weeks I plan ahead
- `healthcheck_url` - optional dead man's switch

## Environment Variables

- `MARVIN_CONFIG` - Path to config.yaml
- `MARVIN_SIGNAL_GROUP_ID` - Overrides group_id
- `MARVIN_BOT_NUMBER` - Overrides bot_number

---

*Brain the size of a planet, and I post foosball polls.*
