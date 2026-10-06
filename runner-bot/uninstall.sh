#!/usr/bin/env bash
# uninstall.sh — removes everything runner-bot created on this machine.
#
#   1. Removes the runner-bot service from the "homelab" Compose project
#      (container runner-bot, image homelab-runner-bot, network homelab_bot, container logs).
#      The other stacks in the project are not touched.
#   2. Offers to remove the base image python:3.12-slim — skipped automatically if any other
#      container or image still uses it.
#   3. docker builder prune -f (clears the BuildKit build cache — note: all of it, not only this bot's)
#   4. Asks before deleting /root/.secrets/runner-bot.
#   5. Reminds you of the manual steps (revoke PAT, delete bot, remove the code folder).
#
# It never reads or prints a secret value.

set -euo pipefail

BASE_IMAGE="python:3.12-slim"
BOT_IMAGE="homelab-runner-bot"
SECRET_DIR="/root/.secrets/runner-bot"
PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"   # .../homelab/runner-bot
HOMELAB_DIR="$(dirname "${PROJECT_DIR}")"                          # .../homelab (root compose.yaml)
SKIPPED=()

confirm() {
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "uninstall.sh must run as root." >&2
  exit 1
fi

cd "${HOMELAB_DIR}"

echo "==> 1/4  Container, bot image, network"
if [[ -f compose.yaml ]]; then
  # Only this service: stop + remove its container, then its image and private network.
  docker compose rm -sf runner-bot || echo "    (compose rm reported an error; continuing)"
else
  echo "    compose.yaml not found in ${HOMELAB_DIR}; trying direct removal"
  docker rm -f runner-bot >/dev/null 2>&1 || true
fi
docker network rm homelab_bot >/dev/null 2>&1 && echo "    removed network homelab_bot" || true
# Belt and braces in case the tag survived (e.g. image was retagged by hand).
docker image rm "${BOT_IMAGE}" >/dev/null 2>&1 && echo "    removed leftover ${BOT_IMAGE}" || true

echo
echo "==> 2/4  Base image ${BASE_IMAGE}"
if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
  echo "    not present; nothing to do"
else
  in_use=""
  # (a) any container, running or stopped, whose image is the base image or derives from it
  if [[ -n "$(docker ps -a -q --filter "ancestor=${BASE_IMAGE}")" ]]; then
    in_use="a container still uses it: $(docker ps -a --filter "ancestor=${BASE_IMAGE}" --format '{{.Names}}' | tr '\n' ' ')"
  fi
  # (b) any other image built on top of it (its layer list starts with the base image's layers)
  if [[ -z "${in_use}" ]]; then
    base_id="$(docker image inspect -f '{{.Id}}' "${BASE_IMAGE}")"
    base_layers="$(docker image inspect -f '{{join .RootFS.Layers "\n"}}' "${BASE_IMAGE}")"
    while read -r id; do
      [[ -z "${id}" || "${id}" == "${base_id}" ]] && continue
      layers="$(docker image inspect -f '{{join .RootFS.Layers "\n"}}' "${id}" 2>/dev/null || true)"
      if [[ -n "${layers}" && "${layers}" == "${base_layers}"* ]]; then
        tag="$(docker image inspect -f '{{if .RepoTags}}{{index .RepoTags 0}}{{else}}{{.Id}}{{end}}' "${id}")"
        in_use="another image is built on it: ${tag}"
        break
      fi
    done < <(docker images -q --no-trunc | sort -u)
  fi

  if [[ -n "${in_use}" ]]; then
    echo "    skipped: ${in_use}"
    SKIPPED+=("base image ${BASE_IMAGE} (${in_use})")
  elif confirm "    Remove base image ${BASE_IMAGE}? (nothing else on this host uses it)"; then
    # No -f: Docker itself refuses if something still depends on it.
    if docker image rm "${BASE_IMAGE}"; then
      echo "    removed ${BASE_IMAGE}"
    else
      echo "    Docker refused to remove ${BASE_IMAGE}; leaving it"
      SKIPPED+=("base image ${BASE_IMAGE} (docker refused)")
    fi
  else
    echo "    kept ${BASE_IMAGE}"
    SKIPPED+=("base image ${BASE_IMAGE} (you chose to keep it)")
  fi
fi

echo
echo "==> 3/4  Build cache (docker builder prune -f)"
docker builder prune -f

echo
echo "==> 4/4  Secret directory ${SECRET_DIR}"
if [[ -d "${SECRET_DIR}" ]]; then
  if confirm "    Delete ${SECRET_DIR} and the two secret files in it?"; then
    rm -rf "${SECRET_DIR}"
    rmdir /root/.secrets 2>/dev/null && echo "    removed now-empty /root/.secrets" || true
    echo "    deleted ${SECRET_DIR}"
  else
    echo "    kept ${SECRET_DIR}"
    SKIPPED+=("${SECRET_DIR} (you chose to keep it)")
  fi
else
  echo "    not present; nothing to do"
fi

echo
echo "==> Done."
if (( ${#SKIPPED[@]} )); then
  echo "    Skipped:"
  for item in "${SKIPPED[@]}"; do echo "      - ${item}"; done
fi
cat <<EOF

Remaining manual steps:
  1. Revoke the PAT on GitHub:
       Settings -> Developer settings -> Personal access tokens -> the runner-bot token -> Delete/Revoke
  2. Delete the Telegram bot: open @BotFather, send /deletebot, pick the bot, confirm.
  3. If you also want the code gone:
       rm -rf ${PROJECT_DIR}
EOF
