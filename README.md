# claude-slack (duo)

Drive Claude Code on this machine from Slack. Each Slack thread is its own Claude Code session.

## Usage

| In Slack | What happens |
| --- | --- |
| `@duo isee fix the failing tests` | Starts a session in `~/work/insighture/isee` and runs in a thread |
| `@duo <task>` | If the first word isn't a repo folder, runs in `~/work/insighture` |
| `@duo --skip-permissions isee <task>` | Starts a session that never asks for approval (also `--yolo`); it stays that way when resumed |
| Reply in the thread | Steers or continues the session |
| `stop` or a ✋ reaction | Interrupts the current turn |
| `close` | Ends the session and frees its process; reply in the thread later to resume it |
| `@duo sessions` | Lists open sessions with links to their threads and whether each is working or idle |
| `@duo config` | Shows the model and effort new sessions use (in a thread: that session's) |
| `@duo effort high` / `@duo model claude-sonnet-5-5` | Sets the default for new sessions; `default` reverts to `.env` |
| `effort max` / `model …` reply in a thread | Changes just that session; it reopens with the new setting on your next message |
| Approve / Deny buttons | Shown for shell commands and other non-read tools |

- Reads, searches and file edits inside the repo run without asking.
- Progress shows in one status message that refreshes every few seconds; the final answer arrives as a new reply.
- `sessions.db` maps threads to sessions, so a reply in an old thread resumes it even after a restart.
- Only `SLACK_ALLOWED_USER` can control the bot.

## How it runs

1. **Service.** `~/.config/systemd/user/claude-slack.service` runs `uv run python bot.py` from this folder, loads `.env`, and restarts 5s after a crash. Linger is on (`loginctl enable-linger`), so it keeps running after logout and starts on boot.
2. **Slack connection.** Socket Mode: the bot opens an outgoing WebSocket to Slack. No public address or open port is needed.
3. **Claude.** Each thread gets a Claude Code session via the Claude Agent SDK. It runs as your user, in the chosen repo folder, with your Claude login, git/SSH keys and settings.

The bot has the same access you do on this machine.

## Setup

1. At api.slack.com/apps, choose **Create New App → From manifest** and paste in `manifest.yml`. Install it to the workspace.
2. Collect:
   - the bot token (`xoxb-…`), from OAuth & Permissions
   - an app-level token (`xapp-…`) with `connections:write`, from Basic Information
   - your member ID (`U…`), from your Slack profile → ⋯ → Copy member ID
3. `cp .env.example .env`, fill in the values, then `chmod 600 .env`.
4. `uv sync`
5. Try it in the foreground: `set -a && . ./.env && uv run python bot.py`
6. Install the service:
   ```
   cp claude-slack.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now claude-slack
   loginctl enable-linger $USER
   ```
7. In Slack, run `/invite @duo` in a channel, then mention it.

## Managing it

```
systemctl --user status claude-slack      # is it running
systemctl --user restart claude-slack     # after editing bot.py or .env
systemctl --user stop claude-slack        # turn it off
journalctl --user -u claude-slack -f      # live logs
```

## Configuration (`.env`)

| Variable | Meaning |
| --- | --- |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for Socket Mode |
| `SLACK_ALLOWED_USER` | The only Slack member ID allowed to control the bot |
| `CLAUDE_MODEL` | Model for sessions, e.g. `claude-opus-5-5` (blank = your Claude Code default; Slack `@duo model` overrides) |
| `CLAUDE_EFFORT` | Effort for sessions: `low`, `medium`, `high`, `xhigh`, `max` (blank = your default; Slack `@duo effort` overrides) |
| `WORK_ROOTS` | Colon-separated folders searched for repo names; the first is the default |

## Notes

- Never commit `.env`.
