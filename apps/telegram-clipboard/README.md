# Telegram Clipboard

Private two-way Telegram clipboard for `@clipboard2_bot`.

The allowlisted user can send text, images, documents, or videos from any
Telegram device. Items persist on `pickbot` until explicitly deleted. The
latest item is exposed as bounded, untrusted user content in every
Receptionist2 startup context.

## Bot commands

- `/latest` - resend the latest item
- `/list` - show the 20 newest items
- `/get <id-prefix>` - resend one item
- `/delete <id-prefix>` - delete one item and its local file
- `/clear confirm` - delete the entire clipboard

## Agent helpers

```bash
clipboard-send /path/to/file --caption "optional"
clipboard-send --text "text to copy"
clipboard-latest
```

`clipboard-latest` prints JSON. Binary items include their absolute local path.

## Install

```bash
sudo apps/telegram-clipboard/deploy/install-telegram-clipboard
sudo /usr/local/sbin/configure-telegram-clipboard
```

The configure command prompts for the BotFather token without echoing it,
reuses Receptionist2's allowlisted Telegram user ID, enables the service, and
verifies the expected username `clipboard2_bot`.
