# homelab

Single source of truth for everything that runs in Docker on the Raspberry Pi, deployed as **one
Compose project named `homelab`**. The root `compose.yaml` includes one folder per stack; each folder
has its own `compose.yaml` and a gitignored `.env` for its secrets (see each `env.example`).

| Folder | Services | Notes |
|---|---|---|
| `nginx-proxy-manager/` | `npm`, `npm-db` | Reverse proxy for `*.home.lab`; data in named volumes |
| `monitoring/` | `grafana`, `prometheus`, `cadvisor`, `node-exporter` | Provisioning and Prometheus config are committed here |
| `runner-bot/` | `runner-bot` | Telegram bot for GitHub Actions runner tokens; secrets in `/root/.secrets/runner-bot` |

Everything is prefixed by the project: networks `homelab_*`, volumes `homelab_*`, built images
`homelab-*`. Portainer shows it as the single stack `homelab`. Portainer itself, AdGuard Home,
Uptime Kuma and WhoDB are installed by DietPi and are not managed here.

## Deploy / update

```bash
cd /root/src/homelab && git pull
docker compose up -d --build            # whole stack
docker compose up -d --build runner-bot # or just one service
docker compose logs -f grafana
```

Always run `docker compose` from the repo root. Running it inside a stack folder would create a
second, separate project.
