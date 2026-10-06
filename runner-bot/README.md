# runner-bot

Part of the `homelab` repository and of the single `homelab` Compose project: this folder is
`runner-bot/` inside the clone at `/root/src/homelab`. Run all `docker compose` commands below from
`/root/src/homelab` (the root `compose.yaml` includes this folder), naming the `runner-bot` service.

A small, hardened Telegram bot that runs as a Docker container on the Raspberry Pi and manages
GitHub Actions **self-hosted runners for one repository**:

| Command | What it does |
|---|---|
| `/token` | Creates a runner registration token and replies with it, its expiry in local time, and a ready-to-copy `./config.sh --url … --token …` line. The reply **and your command** are deleted after `TOKEN_MESSAGE_TTL_SECONDS` (default 300). Limited to one request per 60 s. |
| `/runners` | Lists every runner: name, id, status, busy, labels. |
| `/remove <id>` | Shows the runner's name with **Confirm / Cancel** buttons. The runner is deleted only after Confirm, and only by the allowlisted user who issued the command. Buttons expire after 120 s. |
| `/help` | Lists the commands. |

Anyone whose Telegram user id is not in `ALLOWED_USER_IDS` is ignored silently (no reply); only the
numeric id is logged. The bot answers in private chats only. It uses long polling, so it needs no open
ports, no reverse proxy, and no web UI.

## Prerequisites (already checked)

- Debian 13 on arm64, Docker ≥ 20 with the Compose v2 plugin (`docker compose version`).
- A GitHub **fine-grained PAT** scoped to the one repository with
  *Administration: Read and write* (that is the permission for self-hosted runners).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user id
  (ask [@userinfobot](https://t.me/userinfobot) or any similar bot).

## First-time setup

```bash
cd /root/src/homelab/runner-bot
./setup.sh                                   # creates /root/.secrets/runner-bot and two EMPTY files, then stops
nano /root/.secrets/runner-bot/github_pat    # paste the PAT, one line
nano /root/.secrets/runner-bot/telegram_bot_token
chown 10001:10001 /root/.secrets/runner-bot/github_pat /root/.secrets/runner-bot/telegram_bot_token
chmod 400       /root/.secrets/runner-bot/github_pat /root/.secrets/runner-bot/telegram_bot_token
nano compose.yaml                            # fill GITHUB_OWNER, GITHUB_REPO
echo "ALLOWED_USER_IDS=<your id>" > .env          # your Telegram user id(s), comma-separated; .env is gitignored
cd /root/src/homelab
docker compose up -d --build runner-bot
docker compose logs -f runner-bot
```

The secrets reach the container only as Compose file secrets, bind-mounted read-only at
`/run/secrets/github_pat` and `/run/secrets/telegram_bot_token`. They are **not** environment variables,
so `docker inspect runner-bot` and Portainer never show them.

On startup the bot checks that every config value and both secret files are present and non-empty.
If anything is missing it logs the *name* of the problem (never a value) and exits. Because of
`restart: unless-stopped` Docker will keep retrying; fix the problem and run `docker compose restart runner-bot`.

## Day-to-day

| Action | Command (run in `/root/src/homelab`) |
|---|---|
| Start / rebuild after a code change | `docker compose up -d --build runner-bot` |
| Stop (stays stopped across reboots) | `docker compose stop runner-bot` |
| Start again | `docker compose start runner-bot` |
| Restart | `docker compose restart runner-bot` |
| Logs | `docker compose logs -f runner-bot` |
| Health | `docker inspect -f '{{.State.Health.Status}}' runner-bot` |
| Update dependencies | bump the pins in `requirements.txt`, then `docker compose up -d --build runner-bot` |

Every command is logged to stdout as `cmd=/token user=<id> result=ok|fail github_status=<code>`.
Logs are capped at 3 × 10 MB per container by the `json-file` driver.

## Loading indicator

While the bot waits for GitHub it shows a placeholder ("🏃 Fetching runners…",
"🔑 Requesting registration token…", "🔍 Looking up runner…") and then edits it into the
result, so every client, including Telegram Web K, sees that something is happening.

To show an animated sticker instead, set `LOADING_STICKER` in `compose.yaml` to either
`SetName:emoji` (the bot picks the matching sticker from that set at startup) or a sticker `file_id`.
Send any sticker to the bot and it replies with its set name, emoji and file id, ready to paste.
Apply with `docker compose up -d runner-bot`. The sticker is sent while GitHub is called, then deleted and the
result is sent as a new message. If the set or emoji cannot be found the bot logs a warning and falls
back to the text placeholder. The placeholder stays on screen for at least 1.5 s so it does not just blink.
Stickers are looked up through the Telegram API only; nothing else is fetched at runtime.

## Editing a secret file later

Some editors (including `nano` in its default configuration when the file is not writable by the
current user, and most editors with backup files enabled) write a new file and rename it over the old
one, which resets the owner to root. The container runs as uid 10001 and cannot read a root-owned
`-r--------` file, so **always** re-apply ownership and mode afterwards:

```bash
nano /root/.secrets/runner-bot/github_pat          # or telegram_bot_token
chown 10001:10001 /root/.secrets/runner-bot/github_pat
chmod 400         /root/.secrets/runner-bot/github_pat
docker compose restart runner-bot                   # secrets are read once at startup
```

## Rotating the PAT or the bot token

1. Create the new credential first (new PAT on GitHub, or `/revoke` in @BotFather to get a new token).
2. Write it into the matching file as described above, fix owner and mode.
3. `docker compose restart runner-bot` and check `docker compose logs -f runner-bot` shows `runner-bot started`.
4. Only then revoke the old credential (GitHub token page / already done by `/revoke`).

## Full removal

```bash
cd /root/src/homelab/runner-bot
./uninstall.sh
```

The script removes the `runner-bot` service from the `homelab` project (`docker compose rm -sf runner-bot`, its image and network), offers to remove the
`python:3.12-slim` base image (skipped automatically if any other container or image uses it), runs
`docker builder prune -f` (this clears the whole BuildKit cache on the host, not just this bot's),
asks before deleting `/root/.secrets/runner-bot`, and finally reminds you to revoke the PAT, delete the
bot with BotFather (`/deletebot`), and `rm -rf /root/src/homelab/runner-bot` if you want the code gone too.

## Exactly what exists on the Pi because of this bot

| Item | Where | Removed by |
|---|---|---|
| Project folder (code, this README) | `/root/src/homelab/runner-bot` | you: `rm -rf /root/src/homelab/runner-bot` |
| Secret folder and two secret files | `/root/.secrets/runner-bot` (and the parent `/root/.secrets` if it was created for this) | `uninstall.sh` step 4 (asks first) |
| Container | `runner-bot` | `uninstall.sh` step 1 |
| Container logs (json-file, ≤ 30 MB) | `/var/lib/docker/containers/<id>/` | removed with the container |
| Bot image | `homelab-runner-bot` | `uninstall.sh` step 1 |
| Base image | `python:3.12-slim` | `uninstall.sh` step 2 (asks, skips if shared) |
| BuildKit build cache | Docker's data root | `uninstall.sh` step 3 |
| Compose network | `runner-bot_default` | `uninstall.sh` step 1 |

Nothing is installed on the host, no host user is created (uid 10001 exists only inside the image;
the secret files are merely owned by that number), and no systemd unit, cron job, or firewall rule is
added. The Pi is back to its previous state after `uninstall.sh` plus removing the project folder.

## Portainer

The stack is started from the CLI, but the container `runner-bot` (stack/project `runner-bot`) is a
normal container and shows up in Portainer under *Containers* and *Stacks* (as an "external" stack).
You can stop, restart, inspect logs, or remove it from there as well. Portainer's environment view
shows only the five non-secret variables; the secrets are files under `/run/secrets`, not environment.

## Security notes

- Runs as fixed uid/gid 10001 (not root, not your host user), `read_only` root filesystem, `/tmp`
  as a small `noexec` tmpfs, all capabilities dropped, `no-new-privileges`, 256 MB memory and
  100 pids limits, no ports, no volumes other than the two secret files.
- Allowlist check runs before any command handler; non-allowlisted users and group chats get nothing.
- Pending updates are dropped at startup, so commands queued while the bot was down are not executed.
- GitHub errors are shown to Telegram only as `GitHub returned <status>`; bodies, headers, and
  tracebacks stay in the container log.
- `httpx` / `httpcore` loggers are set to WARNING (their INFO lines contain the bot token in the URL),
  and a logging filter replaces any occurrence of the PAT or bot token with `[REDACTED]`.
- There is deliberately no command that prints configuration, environment, or system information.

## Verify after starting

```bash
docker exec runner-bot id                                   # uid=10001 gid=10001
docker inspect runner-bot --format '{{json .Config.Env}}'    # no PAT, no bot token
docker inspect runner-bot --format '{{json .HostConfig.ReadonlyRootfs}} {{json .HostConfig.CapDrop}}'
docker compose logs --tail 20 runner-bot                     # "runner-bot started: repo=…"
```

Then in Telegram: `/help`, `/token`, `/runners`, and from an account that is **not** in
`ALLOWED_USER_IDS`: any command should get no reply, and the log shows `rejected user_id=<id>`.
