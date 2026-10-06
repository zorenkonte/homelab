#!/usr/bin/env bash
# setup.sh — prepares the secret directory for runner-bot and then STOPS.
#
# What it does (and nothing else):
#   1. Creates /root/.secrets/runner-bot (mode 700, owner root:root).
#   2. Creates the two secret files EMPTY, owner 10001:10001, mode 400 — only if they do not
#      already exist. Existing files are never opened, overwritten, or truncated; only their
#      ownership and mode are re-applied.
#   3. Tells you how to fill them in yourself.
#
# It never reads, prints, or asks for a secret value.

set -euo pipefail
set -o noclobber   # second safety net: a redirection can never overwrite an existing file

SECRET_DIR="/root/.secrets/runner-bot"
SECRET_FILES=("github_pat" "telegram_bot_token")
BOT_UID=10001
BOT_GID=10001

if [[ "$(id -u)" -ne 0 ]]; then
  echo "setup.sh must run as root (it writes under /root/.secrets)." >&2
  exit 1
fi

echo "==> Preparing ${SECRET_DIR}"
# Parent /root/.secrets is created private too (only if missing; an existing one is left as is).
if [[ ! -d "$(dirname "${SECRET_DIR}")" ]]; then
  mkdir -m 700 "$(dirname "${SECRET_DIR}")"
fi
mkdir -p "${SECRET_DIR}"
chown root:root "${SECRET_DIR}"
chmod 700 "${SECRET_DIR}"

for name in "${SECRET_FILES[@]}"; do
  path="${SECRET_DIR}/${name}"
  if [[ -e "${path}" ]]; then
    echo "    exists, left untouched: ${path} (re-applying owner ${BOT_UID}:${BOT_GID}, mode 400)"
  else
    # umask 077 so the file is never world/group-readable even for an instant.
    ( umask 077 && : > "${path}" )
    echo "    created empty:          ${path}"
  fi
  chown "${BOT_UID}:${BOT_GID}" "${path}"
  chmod 400 "${path}"
done

echo
echo "==> Current state (both files should show: -r-------- 1 10001 10001)"
ls -l "${SECRET_DIR}"

cat <<EOF

==> Next steps (do these yourself; this script will not touch the contents):

  1. Paste the GitHub fine-grained PAT (repo scope, Actions: read/write, Administration: read/write
     for self-hosted runners) into the first file, and the Telegram bot token into the second:

       nano ${SECRET_DIR}/github_pat
       nano ${SECRET_DIR}/telegram_bot_token

     One line each, no quotes. Trailing newline is fine.

  2. Some editors replace the file and reset its owner, so always re-run afterwards:

       chown ${BOT_UID}:${BOT_GID} ${SECRET_DIR}/github_pat ${SECRET_DIR}/telegram_bot_token
       chmod 400 ${SECRET_DIR}/github_pat ${SECRET_DIR}/telegram_bot_token

  3. Fill the REPLACE_ME placeholders in compose.yaml (GITHUB_OWNER, GITHUB_REPO, ALLOWED_USER_IDS).

  4. Start the bot:   cd /root/src/homelab/runner-bot && docker compose up -d --build
     Follow logs:     docker compose logs -f

Stopping here. Nothing has been started.
EOF
