# runner-bot: Telegram bot that mints GitHub Actions self-hosted runner registration tokens.
# No secrets or configuration live in this image. Everything arrives at runtime via
# Compose `secrets:` (files under /run/secrets) and `environment:`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Fixed, unprivileged identity. UID/GID 10001 exist only inside the image; the host
# secret files are chown'ed to the same numeric id so the bind-mounted secrets are readable.
RUN groupadd --gid 10001 bot \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin bot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

USER 10001:10001

CMD ["python", "bot.py"]
