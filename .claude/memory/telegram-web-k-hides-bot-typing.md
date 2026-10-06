---
name: telegram-web-k-hides-bot-typing
description: "Telegram Web K (web.telegram.org/k) never shows a bot's typing indicator; Web A, desktop and mobile do"
metadata:
  node_type: memory
  type: reference
  originSessionId: 5a254fe4-b17f-46da-b79e-7bcc7febae63
  modified: 2026-10-06T02:56:10.006Z
---

Verified 2026-10-06 from client source: Telegram Web K (`morethanwords/tweb`, `appImManager.ts`) skips typing status for bot users in private chats, so `sendChatAction("typing")` is invisible there. Web A (`Ajaxy/telegram-tt`), Telegram Desktop and mobile all render it.

The user tests the bot from Telegram Web and did not know K and A were different clients. Related: [[runner-bot-security-constraints]].

**How to apply:** when a Telegram UI feature "doesn't show" during testing, ask whether they are on `/k` or `/a` before debugging the bot. runner-bot holds replies 1 s (`TYPING_MIN_SECONDS` in bot.py) so the indicator is visible on clients that render it.
