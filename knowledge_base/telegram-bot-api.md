# Telegram Bot API behaviour that surprises

Verified: 2026-11-09 (python-telegram-bot 22.8; Receptionist2 and its
watchdog in production).

## Updates

- **`allowed_updates` is remembered per bot.** Telegram keeps the last value
  any poller passed to `getUpdates`. A one-off script that polled with
  `["message"]` silently disabled inline buttons (`callback_query`) for the
  real bot later. Always pass `allowed_updates` explicitly (PTB:
  `run_polling(allowed_updates=...)`).
- **Edits are delivered as updates** and PTB's `MessageHandler` with
  `filters.TEXT` matches `edited_message` too. Make handlers idempotent per
  `(chat_id, message_id)`, or ignore edits (`message.edit_date`).
- Updates a bot triggers itself (for example the service message for a forum
  topic it created) arrive with the bot as `effective_user`.
- To read updates while a bot is stopped, call `getUpdates`; acknowledge them
  with `offset=<last update_id + 1>` so the bot does not replay them.

## Forum supergroups (topics)

- Enable Topics in the group's Edit screen. The bot needs admin rights
  **Manage Topics** (create, rename, close, delete topics) and **Pin
  Messages**; "admin" alone does not include Manage Topics.
- A message's topic is `message_thread_id`, valid only when
  `is_topic_message` is true; messages in General have no thread.
- Message links look like `https://t.me/c/<chat id without -100>/<thread>/<message>`,
  so the chat id is `-100` followed by the first number.
- `getChat(...).pinned_message` does not report pins made inside topics.
  Store the message id of anything you pin and edit it by id.
- `hideGeneralForumTopic` hides General; `deleteForumTopic` removes a topic
  and all its messages permanently.
- Sending to a deleted topic fails with a `BadRequest` mentioning the thread;
  fall back to another chat rather than retrying forever.
- Channels are one-way broadcasts; for several two-way conversations grouped
  together, use a forum supergroup.

## Menus and buttons

- `setMyCommands` with `BotCommandScopeAllPrivateChats` and
  `BotCommandScopeChat(<group>)` sets the ☰ menu per scope; command names
  allow only lowercase letters, digits, and underscores.
- `callback_data` is limited to 64 bytes; a prefix plus a UUID fits.
- `ForceReply` (with `input_field_placeholder`) prompts the user for a typed
  answer.

## Desktop client automation

The Telegram macOS app exposes almost no accessibility tree and draws menus
outside capturable windows, and it does not accept synthesized background
typing. Do Telegram setup through the Bot API, or ask the user to do the
in-app steps.
