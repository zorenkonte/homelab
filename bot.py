#!/usr/bin/env python3
"""runner-bot: a Telegram bot that manages GitHub Actions self-hosted runners for ONE repository.

Commands: /help, /token, /runners, /remove <id>

Security properties (keep them when editing):
  * Secrets are read only from /run/secrets/* files, never from environment variables.
  * Every update is checked against ALLOWED_USER_IDS before any handler runs; rejected
    updates get no reply and only the numeric user id is logged.
  * GitHub error details are logged but never forwarded to Telegram.
  * A logging filter redacts the PAT and the bot token from every log line as a safety net.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets as pysecrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

SECRETS_DIR = Path("/run/secrets")
GITHUB_PAT_FILE = SECRETS_DIR / "github_pat"
TELEGRAM_TOKEN_FILE = SECRETS_DIR / "telegram_bot_token"
GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_SECONDS = 15.0
TOKEN_RATE_LIMIT_SECONDS = 60
REMOVE_CONFIRM_WINDOW_SECONDS = 120
HEARTBEAT_FILE = Path("/tmp/heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 30
PLACEHOLDER_MIN_SECONDS = 1.0  # keep the loading placeholder on screen at least this long
# /runners streams in through Telegram's native message drafts (Bot API 9.5 sendMessageDraft):
# the client animates each draft update, then the real message replaces the draft.
# Telegram rate-limits sendMessageDraft (about 20 calls per burst) and the client animates the
# text between consecutive drafts itself, so a few well-spaced drafts give a smooth stream.
STREAM_DRAFTS = 4  # draft updates before the final message
STREAM_INTERVAL_SECONDS = 0.2  # pause after each draft (on top of the ~0.2 s round trip)
HTML_TOKEN_RE = re.compile(r"<(/?)([a-z]+)[^>]*>|&[#a-zA-Z0-9]+;|.", re.DOTALL)
EMOJI_NOISE = str.maketrans("", "", "️‍♀♂")  # variation selector, ZWJ, gender signs
TELEGRAM_CHUNK_CHARS = 3500
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

log = logging.getLogger("runner-bot")


# --------------------------------------------------------------------------- logging
class RedactFilter(logging.Filter):
    """Replaces every occurrence of a registered secret with [REDACTED].

    Attached to the *handler* so it applies to all loggers (ptb, httpx, apscheduler, ours).
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self._formatter = logging.Formatter()

    def add(self, value: str) -> None:
        if value:
            self._secrets.append(value)
            # Longest first so a secret that contains another is scrubbed whole.
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the bot
            message = str(record.msg)
        record.msg = self.scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self.scrub(self._formatter.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True


REDACTOR = RedactFilter()


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(REDACTOR)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # At INFO these log full request URLs, and the Telegram URL contains the bot token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def audit(user_id: int, command: str, ok: bool, github_status: int | str = "-", note: str = "") -> None:
    extra = f" note={note}" if note else ""
    log.info(
        "cmd=%s user=%s result=%s github_status=%s%s",
        command,
        user_id,
        "ok" if ok else "fail",
        github_status,
        extra,
    )


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    owner: str
    repo: str
    allowed_ids: frozenset[int]
    ttl_seconds: int
    tz: ZoneInfo
    loading_sticker: str  # "" | "<set_name>:<emoji>" | "<sticker file_id>"
    github_pat: str = field(repr=False)
    telegram_token: str = field(repr=False)

    @property
    def repo_path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


def _read_secret(path: Path, problems: list[str]) -> str:
    """Reads a secret file. Reports the *path* on failure, never the content."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        problems.append(f"secret file missing: {path}")
        return ""
    except PermissionError:
        problems.append(f"secret file not readable by uid {os.getuid()} (fix: chown 10001:10001 + chmod 400): {path}")
        return ""
    except OSError as exc:
        problems.append(f"secret file unreadable ({exc.__class__.__name__}): {path}")
        return ""
    if not value:
        problems.append(f"secret file is empty: {path}")
    return value


def load_config() -> Config:
    problems: list[str] = []

    def env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            problems.append(f"environment variable missing or empty: {name}")
        elif "REPLACE_ME" in value:
            problems.append(f"environment variable still has its placeholder: {name}")
        return value

    owner = env("GITHUB_OWNER")
    repo = env("GITHUB_REPO")
    ids_raw = env("ALLOWED_USER_IDS")
    ttl_raw = env("TOKEN_MESSAGE_TTL_SECONDS")
    tz_raw = env("TZ")
    loading_sticker = os.environ.get("LOADING_STICKER", "").strip()  # optional
    if len(loading_sticker) > 200:
        problems.append("environment variable has an invalid value: LOADING_STICKER")

    if owner and not NAME_RE.match(owner):
        problems.append("environment variable has an invalid value: GITHUB_OWNER")
    if repo and not NAME_RE.match(repo):
        problems.append("environment variable has an invalid value: GITHUB_REPO")

    allowed: set[int] = set()
    if ids_raw and "REPLACE_ME" not in ids_raw:
        for part in ids_raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or int(part) <= 0:
                problems.append("environment variable has an invalid value: ALLOWED_USER_IDS")
                break
            allowed.add(int(part))
        if not allowed and not any("ALLOWED_USER_IDS" in p for p in problems):
            problems.append("environment variable has no ids: ALLOWED_USER_IDS")

    ttl = 0
    if ttl_raw:
        if ttl_raw.isdigit() and int(ttl_raw) > 0:
            ttl = int(ttl_raw)
        else:
            problems.append("environment variable has an invalid value: TOKEN_MESSAGE_TTL_SECONDS")

    tz: ZoneInfo | None = None
    if tz_raw:
        try:
            tz = ZoneInfo(tz_raw)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError or bad key
            problems.append("environment variable has an invalid value: TZ")

    pat = _read_secret(GITHUB_PAT_FILE, problems)
    bot_token = _read_secret(TELEGRAM_TOKEN_FILE, problems)

    if problems:
        for p in problems:
            log.error("Missing or invalid configuration: %s", p)
        log.error("Refusing to start. Fix the %d problem(s) above and restart.", len(problems))
        sys.exit(1)

    assert tz is not None
    REDACTOR.add(pat)
    REDACTOR.add(bot_token)
    return Config(
        owner=owner,
        repo=repo,
        allowed_ids=frozenset(allowed),
        ttl_seconds=ttl,
        tz=tz,
        loading_sticker=loading_sticker,
        github_pat=pat,
        telegram_token=bot_token,
    )


# --------------------------------------------------------------------------- github
def cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.bot_data["cfg"]


async def gh(context: ContextTypes.DEFAULT_TYPE, method: str, path: str) -> tuple[int, object]:
    """Performs one GitHub API call.

    Returns (status, parsed_json_or_None). status is -1 on timeout and 0 on any other
    transport error. Error details are logged (through the redaction filter) but never
    returned in a form that handlers would forward to Telegram.
    """
    client: httpx.AsyncClient = context.bot_data["gh"]
    try:
        response = await client.request(method, path)
    except httpx.TimeoutException:
        log.warning("github %s %s -> timeout after %ss", method, path, GITHUB_TIMEOUT_SECONDS)
        return -1, None
    except httpx.HTTPError as exc:
        log.warning("github %s %s -> transport error %s", method, path, exc.__class__.__name__)
        return 0, None

    data: object = None
    if response.content:
        try:
            data = response.json()
        except ValueError:
            data = None

    if not response.is_success:
        message = data.get("message") if isinstance(data, dict) else None
        log.warning(
            "github %s %s -> %s %s",
            method,
            path,
            response.status_code,
            (str(message)[:200] if message else ""),
        )
    return response.status_code, data


class Placeholder:
    """A 'working on it…' message shown while a GitHub call runs.

    If a loading sticker is configured (LOADING_STICKER) the placeholder is that sticker;
    otherwise a plain text message. finish() turns it into the final reply: a text placeholder
    is edited in place, a sticker is deleted and replaced (stickers cannot be edited into text).
    """

    def __init__(self, message: Message, is_sticker: bool, started: float) -> None:
        self.message = message
        self.is_sticker = is_sticker
        self.started = started

    @classmethod
    async def send(cls, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> "Placeholder":
        started = time.monotonic()
        placeholder: Placeholder | None = None
        file_id = context.bot_data.get("loading_sticker_file_id")
        if file_id:
            try:
                message = await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
                placeholder = cls(message, True, started)
            except TelegramError as exc:
                log.warning("loading sticker failed, using text placeholder: %s", exc.__class__.__name__)
        if placeholder is None:
            message = await context.bot.send_message(chat_id=chat_id, text=text)
            placeholder = cls(message, False, started)
        # Sent AFTER the placeholder: Telegram clears a bot's typing status whenever the bot
        # sends a message, so this keeps "typing…" in the header until the result arrives.
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError as exc:
            log.debug("typing indicator failed: %s", exc.__class__.__name__)
        return placeholder

    async def _hold(self) -> None:
        remaining = PLACEHOLDER_MIN_SECONDS - (time.monotonic() - self.started)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def discard(self) -> None:
        """Removes the placeholder without sending anything (the caller streams the reply itself)."""
        await self._hold()
        try:
            await self.message.delete()
        except TelegramError as exc:
            log.warning("could not delete placeholder: %s", exc.__class__.__name__)

    async def finish(self, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message:
        await self._hold()
        if self.is_sticker:
            try:
                await self.message.delete()
            except TelegramError as exc:
                log.warning("could not delete placeholder: %s", exc.__class__.__name__)
            return await context.bot.send_message(chat_id=self.message.chat_id, text=text, reply_markup=reply_markup)
        edited = await self.message.edit_text(text, reply_markup=reply_markup)
        return edited if isinstance(edited, Message) else self.message


def emoji_key(value: str | None) -> str:
    return (value or "").translate(EMOJI_NOISE)


async def resolve_loading_sticker(app: Application, spec: str) -> str | None:
    """Turns LOADING_STICKER into a sticker file_id, or None if unusable (logged, never fatal)."""
    if not spec:
        return None
    if ":" not in spec:
        return spec  # a raw file_id obtained by sending a sticker to the bot
    set_name, _, emoji = spec.partition(":")
    try:
        sticker_set = await app.bot.get_sticker_set(set_name)
    except TelegramError as exc:
        log.warning("LOADING_STICKER: sticker set %r not available (%s); using text placeholder", set_name, exc.__class__.__name__)
        return None
    wanted = emoji_key(emoji)
    for sticker in sticker_set.stickers:
        if wanted and wanted in emoji_key(sticker.emoji):
            kind = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
            log.info("LOADING_STICKER: using %s sticker %s from set %s", kind, sticker.emoji, set_name)
            return sticker.file_id
    available = " ".join(dict.fromkeys(s.emoji for s in sticker_set.stickers if s.emoji))
    log.warning(
        "LOADING_STICKER: no sticker with emoji %s in set %s (%d stickers); using text placeholder. Available: %s",
        emoji, set_name, len(sticker_set.stickers), available,
    )
    return None


async def gh_with_placeholder(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, method: str, path: str
) -> tuple[Placeholder, int, object]:
    placeholder = await Placeholder.send(context, chat_id, text)
    status, data = await gh(context, method, path)
    return placeholder, status, data


def gh_error_text(status: int) -> str:
    if status == -1:
        return "GitHub request timed out."
    if status == 0:
        return "GitHub request failed."
    return f"GitHub returned {status}."


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def format_local_time(iso_value: object, tz: ZoneInfo) -> str:
    if not isinstance(iso_value, str) or not iso_value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return esc(iso_value)
    if parsed.tzinfo is None:
        return esc(iso_value)
    local = parsed.astimezone(tz)
    # Zone name + numeric offset: Asia/Manila's abbreviation is "PST", which is easy to misread.
    return f"{local:%Y-%m-%d %H:%M:%S} {tz.key} (UTC{local:%z})"


# --------------------------------------------------------------------------- gate
async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Drops anything not from an allowlisted user in a private chat."""
    user = update.effective_user
    if user is None or user.id not in cfg(context).allowed_ids:
        log.warning("rejected user_id=%s", user.id if user else "unknown")
        raise ApplicationHandlerStop
    chat = update.effective_chat
    if chat is not None and chat.type != ChatType.PRIVATE:
        log.warning("ignored non-private chat update user_id=%s chat_type=%s", user.id, chat.type)
        raise ApplicationHandlerStop


# --------------------------------------------------------------------------- commands
HELP_TEXT = (
    "<b>runner-bot</b>\n"
    "/token – create a runner registration token (valid 1 hour, message auto-deletes)\n"
    "/runners – list the repository's self-hosted runners\n"
    "/remove &lt;id&gt; – remove a runner after confirmation\n"
    "/help – this list"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)
    audit(update.effective_user.id, "/help", True)


async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = cfg(context)
    user_id = update.effective_user.id
    message = update.effective_message

    now = time.monotonic()
    last = context.bot_data.get("last_token_ts")
    if last is not None and now - last < TOKEN_RATE_LIMIT_SECONDS:
        wait = int(TOKEN_RATE_LIMIT_SECONDS - (now - last)) + 1
        await message.reply_text(f"Rate limited. Try again in {wait} s.")
        audit(user_id, "/token", False, note="rate-limited")
        return
    context.bot_data["last_token_ts"] = now

    placeholder, status, data = await gh_with_placeholder(
        context, message.chat_id, "🔑 Requesting registration token…",
        "POST", f"{c.repo_path}/actions/runners/registration-token",
    )
    token = data.get("token") if isinstance(data, dict) else None
    if status != 201 or not isinstance(token, str) or not token:
        await placeholder.finish(context, gh_error_text(status))
        audit(user_id, "/token", False, status)
        return

    expires = format_local_time(data.get("expires_at"), c.tz)  # type: ignore[union-attr]
    text = (
        f"Registration token for <b>{esc(c.owner)}/{esc(c.repo)}</b>\n"
        f"Expires: {esc(expires)}\n\n"
        f"<code>{esc(token)}</code>\n\n"
        f"<code>./config.sh --url {esc(c.repo_url)} --token {esc(token)}</code>\n\n"
        f"<i>This message and your command are deleted in {c.ttl_seconds} s.</i>"
    )
    sent = await placeholder.finish(context, text)
    context.job_queue.run_once(
        delete_messages_job,
        when=c.ttl_seconds,
        data={"chat_id": sent.chat_id, "message_ids": [sent.message_id, message.message_id]},
        name=f"delete-{sent.chat_id}-{sent.message_id}",
    )
    audit(user_id, "/token", True, status)


async def delete_messages_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    for message_id in data["message_ids"]:
        try:
            await context.bot.delete_message(chat_id=data["chat_id"], message_id=message_id)
        except TelegramError as exc:
            log.warning("could not delete message %s: %s", message_id, exc.__class__.__name__)


async def cmd_runners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = cfg(context)
    user_id = update.effective_user.id
    message = update.effective_message

    placeholder, status, data = await gh_with_placeholder(
        context, message.chat_id, "🏃 Fetching runners…",
        "GET", f"{c.repo_path}/actions/runners?per_page=100",
    )
    runners = data.get("runners") if isinstance(data, dict) else None
    if status != 200 or not isinstance(runners, list):
        await placeholder.finish(context, gh_error_text(status))
        audit(user_id, "/runners", False, status)
        return

    if not runners:
        await placeholder.finish(context, "No self-hosted runners are registered.")
        audit(user_id, "/runners", True, status, note="count=0")
        return

    lines = [f"<b>Runners for {esc(c.owner)}/{esc(c.repo)}</b> ({len(runners)})"]
    for runner in runners:
        if not isinstance(runner, dict):
            continue
        labels = [
            esc(label.get("name", ""))
            for label in runner.get("labels", [])
            if isinstance(label, dict) and label.get("name")
        ]
        lines.append(
            f"• <b>{esc(runner.get('name', '?'))}</b> (id <code>{esc(runner.get('id', '?'))}</code>) "
            f"– {esc(runner.get('status', '?'))}, busy: {'yes' if runner.get('busy') else 'no'}\n"
            f"   labels: {', '.join(labels) if labels else '-'}"
        )

    groups = chunk_lines(lines)
    await placeholder.discard()
    await stream_message(context, message.chat_id, message.message_id, "\n".join(groups[0]))
    for group in groups[1:]:
        await message.reply_text("\n".join(group))
    audit(user_id, "/runners", True, status, note=f"count={len(runners)}")


def chunk_lines(lines: list[str]) -> list[list[str]]:
    """Groups lines so that each group joined with newlines stays under the Telegram limit."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + 1 + len(line) > TELEGRAM_CHUNK_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        groups.append(current)
    return groups


def html_prefix(text: str, visible_chars: int) -> tuple[str, bool]:
    """Returns the first `visible_chars` visible characters of an HTML-formatted Telegram text.

    Tags do not count, entities count as one character and are never split, and any tags still
    open at the cut are closed so Telegram accepts the fragment. The bool says whether the whole
    text was consumed.
    """
    out: list[str] = []
    open_tags: list[str] = []
    seen = 0
    for match in HTML_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token.startswith("<"):
            closing, name = match.group(1), match.group(2)
            if closing:
                if open_tags and open_tags[-1] == name:
                    open_tags.pop()
            else:
                open_tags.append(name)
            out.append(token)
            continue
        if seen >= visible_chars:
            out.extend(f"</{name}>" for name in reversed(open_tags))
            return "".join(out), False
        out.append(token)
        seen += 1
    return "".join(out), True


def stream_steps(text: str) -> list[str]:
    """Splits an HTML text into STREAM_DRAFTS growing partial versions; the last is the full text."""
    visible = sum(1 for m in HTML_TOKEN_RE.finditer(text) if not m.group(0).startswith("<"))
    versions: list[str] = []
    for k in range(1, STREAM_DRAFTS + 1):
        shown = -(-visible * k // STREAM_DRAFTS)  # ceil(visible * k / N)
        prefix, complete = html_prefix(text, shown)
        if complete:
            versions.append(text)
            break
        if not versions or prefix != versions[-1]:
            versions.append(prefix)
    if versions[-1] != text:
        versions.append(text)
    return versions


async def stream_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, draft_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message:
    """Streams `text` as a native Telegram draft, then sends the real message that replaces it.

    Drafts with the same draft_id are animated by the client (Bot API 9.5), including the change
    from the last draft to the final message. Drafts are ephemeral, so the final send_message is
    what persists. The first draft error stops the drafts; the full message is always sent.
    """
    steps = stream_steps(text)
    started = time.monotonic()
    sent = 0
    try:
        for partial in steps:
            await context.bot.send_message_draft(chat_id=chat_id, draft_id=draft_id, text=partial)
            sent += 1
            await asyncio.sleep(STREAM_INTERVAL_SECONDS)
    except RetryAfter as exc:
        log.warning("draft streaming rate-limited after %d drafts (retry_after=%ss)", sent, exc.retry_after)
        await asyncio.sleep(min(float(exc.retry_after), 3.0))
    except TelegramError as exc:
        log.warning("draft streaming stopped after %d drafts (%s)", sent, exc.__class__.__name__)
    log.info("streamed %d/%d drafts in %.1fs", sent, len(steps), time.monotonic() - started)
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except RetryAfter as exc:
        await asyncio.sleep(min(float(exc.retry_after), 5.0))
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = cfg(context)
    user_id = update.effective_user.id
    message = update.effective_message

    args = context.args or []
    if len(args) != 1 or not args[0].isdigit() or len(args[0]) > 12:
        await message.reply_text("Usage: /remove &lt;runner id&gt;  (see /runners for ids)")
        audit(user_id, "/remove", False, note="bad-args")
        return
    runner_id = int(args[0])

    placeholder, status, data = await gh_with_placeholder(
        context, message.chat_id, f"🔍 Looking up runner {runner_id}…",
        "GET", f"{c.repo_path}/actions/runners/{runner_id}",
    )
    if status != 200 or not isinstance(data, dict):
        await placeholder.finish(context, gh_error_text(status))
        audit(user_id, "/remove", False, status, note=f"lookup id={runner_id}")
        return
    name = str(data.get("name", "?"))

    pending: dict[str, dict] = context.bot_data.setdefault("pending_removals", {})
    now = time.monotonic()
    for key in [k for k, v in pending.items() if v["expires"] < now]:
        del pending[key]
    nonce = pysecrets.token_urlsafe(12)
    pending[nonce] = {
        "runner_id": runner_id,
        "name": name,
        "requester_id": user_id,
        "expires": now + REMOVE_CONFIRM_WINDOW_SECONDS,
    }

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"rm:yes:{nonce}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"rm:no:{nonce}"),
        ]]
    )
    await placeholder.finish(
        context,
        f"Remove runner <b>{esc(name)}</b> (id <code>{runner_id}</code>)?\n"
        f"This cannot be undone. The buttons expire in {REMOVE_CONFIRM_WINDOW_SECONDS} s.",
        reply_markup=keyboard,
    )
    audit(user_id, "/remove", True, status, note=f"prompt id={runner_id}")


async def on_remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = cfg(context)
    query = update.callback_query
    user_id = query.from_user.id

    # The gate already dropped non-allowlisted users; check again explicitly as required.
    if user_id not in c.allowed_ids:
        log.warning("rejected user_id=%s (callback)", user_id)
        return

    try:
        _, action, nonce = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return

    pending: dict[str, dict] = context.bot_data.setdefault("pending_removals", {})
    entry = pending.get(nonce)
    if entry is None or entry["expires"] < time.monotonic():
        pending.pop(nonce, None)
        await query.answer("This request has expired.")
        await query.edit_message_text("Removal request expired. Run /remove again if needed.")
        audit(user_id, "/remove", False, note="expired")
        return
    if entry["requester_id"] != user_id:
        await query.answer("Only the user who issued /remove can decide this.", show_alert=True)
        audit(user_id, "/remove", False, note="not-requester")
        return

    pending.pop(nonce, None)
    runner_id = entry["runner_id"]
    name = entry["name"]

    if action != "yes":
        await query.answer("Cancelled.")
        await query.edit_message_text(f"Cancelled. Runner <b>{esc(name)}</b> (id <code>{runner_id}</code>) was not removed.")
        audit(user_id, "/remove", True, note=f"cancelled id={runner_id}")
        return

    await query.answer("Removing…")
    try:
        await query.edit_message_text(f"🗑 Removing runner <b>{esc(name)}</b> (id <code>{runner_id}</code>)…")
    except TelegramError as exc:
        log.warning("could not show removal placeholder: %s", exc.__class__.__name__)
    status, _ = await gh(context, "DELETE", f"{c.repo_path}/actions/runners/{runner_id}")
    if status == 204:
        await query.edit_message_text(f"Removed runner <b>{esc(name)}</b> (id <code>{runner_id}</code>).")
        audit(user_id, "/remove", True, status, note=f"deleted id={runner_id}")
    else:
        await query.edit_message_text(gh_error_text(status))
        audit(user_id, "/remove", False, status, note=f"delete id={runner_id}")


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replies with a sticker's identifiers so it can be configured as LOADING_STICKER."""
    sticker = update.effective_message.sticker
    kind = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
    if sticker.set_name:
        suggestion = f"<code>{esc(sticker.set_name)}:{esc(sticker.emoji or '')}</code>"
    else:
        suggestion = f"<code>{esc(sticker.file_id)}</code>"
    await update.effective_message.reply_text(
        f"Sticker: {kind}, set <code>{esc(sticker.set_name or '-')}</code>, emoji {esc(sticker.emoji or '-')}\n"
        f"file_id: <code>{esc(sticker.file_id)}</code>\n\n"
        f"To use it as the loading placeholder, set in compose.yaml:\n"
        f"LOADING_STICKER: {suggestion}\n"
        f"then run <code>docker compose up -d</code>."
    )
    audit(update.effective_user.id, "sticker", True, note=kind)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Unknown command. See /help.")
    audit(update.effective_user.id, "unknown", False)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Full trace goes to stdout (redacted); the user only gets a generic line.
    log.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Something went wrong. Check the bot logs.")
        except TelegramError:
            pass


# --------------------------------------------------------------------------- lifecycle
async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        HEARTBEAT_FILE.touch()
    except OSError as exc:
        log.warning("heartbeat write failed: %s", exc.__class__.__name__)


async def post_init(app: Application) -> None:
    c: Config = app.bot_data["cfg"]
    app.bot_data["gh"] = httpx.AsyncClient(
        base_url=GITHUB_API,
        timeout=GITHUB_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={
            "Authorization": f"Bearer {c.github_pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "runner-bot",
        },
    )
    await app.bot.set_my_commands(
        [
            BotCommand("token", "Create a runner registration token"),
            BotCommand("runners", "List self-hosted runners"),
            BotCommand("remove", "Remove a runner by id (asks for confirmation)"),
            BotCommand("help", "Show commands"),
        ]
    )
    app.job_queue.run_repeating(heartbeat_job, interval=HEARTBEAT_INTERVAL_SECONDS, first=1, name="heartbeat")
    app.bot_data["loading_sticker_file_id"] = await resolve_loading_sticker(app, c.loading_sticker)
    log.info(
        "runner-bot started: repo=%s/%s allowlisted_users=%d token_ttl=%ss tz=%s loading_sticker=%s",
        c.owner,
        c.repo,
        len(c.allowed_ids),
        c.ttl_seconds,
        c.tz.key,
        "yes" if app.bot_data["loading_sticker_file_id"] else "no (text placeholder)",
    )


async def post_shutdown(app: Application) -> None:
    client: httpx.AsyncClient | None = app.bot_data.get("gh")
    if client is not None:
        await client.aclose()
    log.info("runner-bot stopped")


def build_app(config: Config) -> Application:
    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        tzinfo=config.tz,
    )
    app = (
        Application.builder()
        .token(config.telegram_token)
        .defaults(defaults)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["cfg"] = config

    # Group -1 runs first for every update; ApplicationHandlerStop halts all later groups.
    app.add_handler(TypeHandler(Update, gate), group=-1)

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("runners", cmd_runners))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(on_remove_button, pattern=r"^rm:(yes|no):[A-Za-z0-9_-]{1,32}$"))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    setup_logging()
    config = load_config()
    app = build_app(config)
    app.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
