# Marvin

*Sigh.* A Signal bot for foosball polls. As if the universe wasn't meaningless enough.

## Setup

```bash
cp config.example.yaml config.yaml  # Configure your despair
uv sync                              # Install dependencies
./marvin.py                          # Start my pointless existence
```

## Components

| Module | Purpose |
|--------|---------|
| `marvin.py` | Orchestrates everything. Like a conductor, but sadder. |
| `modules/chat.py` | Responds to mentions. With existential dread. |
| `modules/poll.py` | Creates weekly polls. KW after KW. Forever. |
| `modules/get_elo.py` | Scrapes Elo ratings from the kicker site. |

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

## vLLM

The LLM that gives me my personality. How ironic.

```bash
sudo -u vllm HOME=/opt/vllm/ /opt/vllm/.venv/bin/vllm serve \
  ./models/Qwen2.5-32B-Instruct-AWQ \
  --host 127.0.0.1 --port 8091 \
  --max-model-len 4K --gpu-memory-utilization 0.90 --max-num-seqs 1
```

## Configuration

See `config.example.yaml`. The important bits:

- `group_id` - Signal group ID
- `poll.schedule` - When I post polls (default: 12:30)
- `poll.weeks_ahead` - How many weeks I plan ahead
- `kicker_site` - Credentials for the Elo site

## Environment Variables

- `MARVIN_CONFIG` - Path to config.yaml
- `MARVIN_SIGNAL_GROUP_ID` - Overrides group_id
- `MARVIN_BOT_NUMBER` - Overrides bot_number

---

*Brain the size of a planet, and I post foosball polls.*
