---
name: homelab-single-stack
description: "Everything on the Pi runs as ONE Compose project 'homelab' from /root/src/homelab/compose.yaml (include: nginx-proxy-manager/, monitoring/, runner-bot/); migrated 2026-10-06; crunch-db and Portainer stack 33 are pending removal"
metadata:
  type: project
---

Since 2026-10-06 the Pi's Docker workloads are a single Compose project named `homelab`, deployed by CLI from `/root/src/homelab` (root `compose.yaml` uses `include:` for `nginx-proxy-manager/`, `monitoring/`, `runner-bot/`). Names: networks `homelab_proxy|internal|bot`, volumes `homelab_npm-data|npm-letsencrypt|npm-db|grafana-data|prometheus-data`, bot image `homelab-runner-bot`. Each folder has a gitignored `.env` (NPM Postgres creds, Grafana admin login, bot ALLOWED_USER_IDS). Portainer CE 2.45 is only a viewer: its relative-path-volumes feature is BE-only, so git-deploying from Portainer would break the bind-mounted Grafana provisioning.

Cleanup done 2026-10-06: crunch-db (container, volume, folder, old network) and the orphaned `/data/compose` dirs are deleted. The dead Portainer stack 33 was deleted by the user and the NPM Postgres password was rotated to a random value (in nginx-proxy-manager/.env) on 2026-10-06. Nothing pending.

**Why:** the user wants one source of truth and one stack in Portainer, and asked for leftovers to be deleted once verified.
**How to apply:** always run `docker compose` from `/root/src/homelab` (running it inside a subfolder creates a second project); NPM proxy hosts point at host ports (172.17.0.1), so port mappings must stay. Related: [[runner-bot-security-constraints]].
