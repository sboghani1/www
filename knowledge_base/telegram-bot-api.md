# Telegram Bot API behaviour that surprises

Verified: 2026-11-23 (python-telegram-bot 22.8; Receptionist2, its
watchdog and Bell in production).

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

## Sending

- **A send can be delivered and still raise `TimedOut`** (PTB defaults to
  5-second timeouts; Telegram also answered `Bad Gateway` that day). A job
  that resent on error posted one alert six times. For unprompted messages,
  record them as sent first and never retry; raise timeouts with
  `Application.builder().connect_timeout(15).read_timeout(30).write_timeout(30)`.
- Re-editing a message with identical text and markup raises `BadRequest`
  "message is not modified"; catch it.
- `setMyName` changes a bot's display name (rate-limited, call once); the
  username only changes in BotFather.

## Removing a bot's own messages

- Bots cannot read chat history. Message ids in a private chat increase by
  one per message from either side.
- To confirm a message is an exact copy before deleting it, call
  `edit_message_reply_markup` with the identical keyboard: Telegram answers
  "message is not modified" only for a match, and changes nothing. Then
  `deleteMessage`. Stop at the first id that does not match, since a
  successful edit would overwrite another message's buttons.
- Several items in one message can each have keyboard rows; on a tap, edit
  the markup to drop only that item's rows.

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

*Connections:* receptionist2.md, bell-bait-tracker.md.
