---
name: runner-bot-security-constraints
description: "Hard rules for the runner-bot project — never read /root/.secrets, no host changes outside allowed folders, secrets only via Compose file secrets; project moved into the homelab repo"
metadata:
  node_type: memory
  type: project
  originSessionId: 5a254fe4-b17f-46da-b79e-7bcc7febae63
  modified: 2026-10-06T09:10:33.917Z
---

runner-bot (Telegram bot for GitHub Actions runner tokens, built 2026-10-06) now lives at `/root/src/homelab/runner-bot`, a subdirectory of the `zorenkonte/homelab` GitHub repository cloned at `/root/src/homelab`. Standing rules from the user:

- Never read, cat, grep, print, or echo the GitHub PAT or Telegram bot token, and never open any file under `/root/.secrets/`. The two secret files are `/root/.secrets/runner-bot/github_pat` and `/root/.secrets/runner-bot/telegram_bot_token`, owned `10001:10001` mode `400`; only `ls -l` metadata may be inspected.
- Secrets reach the container only through Compose `secrets:` (file sources, mounted at `/run/secrets/`), never as environment variables.
- Do not modify the Pi outside the project checkout and `/root/.secrets/runner-bot`: no package installs (gh is NOT installed; user declined installing it), host users, systemd units, cron jobs, firewall changes, or touching Nginx Proxy Manager / Portainer. If a tool is missing, report it instead of installing it.
- Container runs as fixed uid/gid 10001, read-only, cap_drop ALL, no ports, long polling only.
- Git: repo-local identity only (name "Renzo Bringino", email `29701857+zorenkonte@users.noreply.github.com`), SSH commit signing with `/root/.ssh/id_ed25519.pub`, and NO Claude attribution trailers (user disabled them in `~/.claude/settings.json`).

**Why:** The user values security over features and must be able to remove the bot leaving the Pi exactly as before. They plan to integrate the bot with n8n later, which is why it was folded into the homelab repo.
**How to apply:** Use `docker compose run --rm --no-deps` for tests (never `up -d` with real secrets), snapshot/diff host state when verifying, keep verification reads to file metadata only, and run compose commands from `/root/src/homelab` naming the `runner-bot` service (part of the single `homelab` project since 2026-10-06, image `homelab-runner-bot`, network `homelab_bot`).
