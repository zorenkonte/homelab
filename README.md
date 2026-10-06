# homelab

Single source of truth for everything that runs in Docker on the Raspberry Pi. One folder per stack,
each with its own `compose.yaml`; secrets live in a gitignored `.env` next to it (see each `env.example`).

| Folder | What | How it is deployed |
|---|---|---|
| `nginx-proxy-manager/` | Reverse proxy for `*.home.lab` | Portainer stack from this repo (env vars set in Portainer) |
| `monitoring/` | Grafana, Prometheus, cAdvisor, node-exporter | `docker compose up -d` from the folder in `/root/src/homelab` |
| `runner-bot/` | Telegram bot for GitHub Actions runner tokens | `docker compose up -d --build` from the folder; secrets in `/root/.secrets/runner-bot` |

Portainer, AdGuard Home, Uptime Kuma and WhoDB are installed by DietPi and are not managed here.

## Updating a stack

```bash
cd /root/src/homelab && git pull
cd <stack> && docker compose up -d        # add --build for runner-bot
```
