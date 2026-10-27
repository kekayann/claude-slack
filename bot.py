"""Slack <-> Claude Code bridge (Socket Mode).

@duo [repo] <task>   start a session in a thread (repo = folder under WORK_ROOTS)
@duo --skip-permissions [repo] <task>   same, but Claude never asks for approval
reply in thread         steer / continue the session
"stop" or ✋ reaction    interrupt the current turn
"close"                 end the session (reply later to resume it)
@duo sessions           list open sessions
@duo effort|model <x>   set the default for new sessions (@duo config shows it)
"effort|model <x>"      in a thread: change it for that session
"""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    tool,
    TextBlock,
    ToolUseBlock,
)
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

ALLOWED_USER = os.environ["SLACK_ALLOWED_USER"]
WORK_ROOTS = [Path(p).expanduser() for p in os.environ.get("WORK_ROOTS", "~/work/insighture:~/work").split(":")]
DEFAULT_CWD = WORK_ROOTS[0]
SLACK_TOOLS = {f"mcp__slack__{n}" for n in ("post_to_thread", "send_message", "upload_file")}
SAFE_TOOLS = SLACK_TOOLS | {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch", "WebFetch", "Task", "NotebookRead"}
UPDATE_EVERY = 3  # seconds between status edits
SLACK_LIMIT = 3500
SKIP_FLAGS = {"--skip-permissions", "--yolo", "—skip-permissions", "—yolo"}  # Slack may turn -- into —
EFFORTS = ("low", "medium", "high", "xhigh", "max")

app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
db = sqlite3.connect(Path(__file__).with_name("sessions.db"))
db.execute("CREATE TABLE IF NOT EXISTS sessions (thread TEXT PRIMARY KEY, channel TEXT, session_id TEXT, cwd TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
sessions: dict[str, "Session"] = {}
pending: dict[str, asyncio.Future] = {}  # tool_use_id -> approval future


def resolve_cwd(words: list[str]) -> tuple[Path, list[str]]:
    if words:
        for root in WORK_ROOTS:
            if (root / words[0]).is_dir():
                return root / words[0], words[1:]
    return DEFAULT_CWD, words


def setting(key: str, thread: str | None = None) -> str | None:
    """Thread override, else global default from Slack, else env."""
    for k in ([f"{thread}:{key}"] if thread else []) + [key]:
        row = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if row:
            return row[0]
    return os.environ.get(f"CLAUDE_{key.upper()}") or None


def save_setting(key: str, value: str):
    if value:
        db.execute("REPLACE INTO settings VALUES (?,?)", (key, value))
    else:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    db.commit()


def short(obj, n=300) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return s if len(s) <= n else s[:n] + "…"


class Session:
    def __init__(self, channel: str, thread: str, cwd: Path, resume: str | None = None):
        self.channel, self.thread, self.cwd = channel, thread, cwd
        self.client = ClaudeSDKClient(ClaudeAgentOptions(
            cwd=str(cwd),
            resume=resume,
            permission_mode=setting("mode", thread) or "acceptEdits",
            model=setting("model", thread),
            effort=setting("effort", thread),
            can_use_tool=self.approve,
            setting_sources=["user", "project", "local"],
            mcp_servers={"slack": self.slack_tools()},
        ))
        self.status_ts: str | None = None
        self.log: list[str] = []
        self.last_edit = 0.0
        self.closing = False

    async def start(self):
        await self.client.connect()
        asyncio.create_task(self.pump())

    async def send(self, text: str):
        if not self.status_ts:
            r = await app.client.chat_postMessage(channel=self.channel, thread_ts=self.thread, text=":hourglass: working…")
            self.status_ts = r["ts"]
        await self.client.query(text)

    async def post(self, text: str, **kw):
        for i in range(0, max(len(text), 1), SLACK_LIMIT):
            await app.client.chat_postMessage(channel=self.channel, thread_ts=self.thread, text=text[i:i + SLACK_LIMIT], **kw)

    async def refresh_status(self, force=False):
        if not self.status_ts or (not force and time.time() - self.last_edit < UPDATE_EVERY):
            return
        self.last_edit = time.time()
        body = "\n".join(self.log[-15:]) or "…"
        await app.client.chat_update(channel=self.channel, ts=self.status_ts, text=f":hourglass: working…\n{body}"[:SLACK_LIMIT])

    async def pump(self):
        try:
            async for msg in self.client.receive_messages():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    sid = msg.data.get("session_id")
                    db.execute("REPLACE INTO sessions VALUES (?,?,?,?)", (self.thread, self.channel, sid, str(self.cwd)))
                    db.commit()
                elif isinstance(msg, AssistantMessage) and not msg.parent_tool_use_id:
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            self.log.append(f"• `{b.name}` {short(b.input, 120)}")
                        elif isinstance(b, TextBlock) and b.text.strip():
                            self.log.append(f"> {short(b.text, 200)}")
                    await self.refresh_status()
                elif isinstance(msg, ResultMessage):
                    if self.status_ts:
                        await self.refresh_status(force=True)
                        icon = ":x:" if msg.is_error else ":white_check_mark:"
                        cost = f" · ${msg.total_cost_usd:.2f}" if msg.total_cost_usd else ""
                        await app.client.chat_update(
                            channel=self.channel, ts=self.status_ts,
                            text=f"{icon} done ({msg.num_turns} turns{cost})\n" + "\n".join(self.log[-15:]))
                    await self.post(msg.result or msg.subtype)
                    self.status_ts, self.log = None, []
        except Exception as e:
            if self.closing:
                return
            await self.post(f":boom: session crashed: `{e}`")
            sessions.pop(self.thread, None)

    async def close(self, quiet=False):
        self.closing = True
        sessions.pop(self.thread, None)
        await self.client.disconnect()
        if not quiet:
            await self.post(":lock: session closed. Reply here to resume it.")

    async def ask(self, key: str, prompt: str, detail: str) -> bool:
        """Post Approve/Deny buttons in the thread and wait for a click."""
        fut = asyncio.get_running_loop().create_future()
        pending[key] = fut
        await app.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread, text=prompt,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{prompt}*\n```{detail}```"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
                     "action_id": "approve", "value": key},
                    {"type": "button", "text": {"type": "plain_text", "text": "Deny"}, "style": "danger",
                     "action_id": "deny", "value": key},
                ]},
            ])
        try:
            return await fut
        finally:
            pending.pop(key, None)

    async def approve(self, tool: str, inp: dict, ctx):
        if tool in SAFE_TOOLS:
            return PermissionResultAllow()
        ok = await self.ask(ctx.tool_use_id, ctx.title or f"Claude wants to use {tool}", short(inp, 1500))
        return PermissionResultAllow() if ok else PermissionResultDeny(message="Denied by user in Slack")

    def slack_tools(self):
        def reply(text):
            return {"content": [{"type": "text", "text": text}]}

        @tool("post_to_thread", "Post a progress update to the Slack thread the user is watching. "
              "Use for milestones in long tasks; your final answer is posted automatically.", {"text": str})
        async def post_to_thread(args):
            await self.post(args["text"])
            return reply("posted")

        @tool("send_message", "Send a Slack message to a channel or person outside this thread. "
              "target is '#channel-name', a channel ID, '@username', or a user ID. "
              "The user must approve every message.", {"target": str, "text": str})
        async def send_message(args):
            target, text = args["target"].strip(), args["text"]
            try:
                channel = await resolve_target(target)
            except LookupError as e:
                return reply(f"error: {e}")
            key = f"send:{time.time()}"
            if not await self.ask(key, f"Claude wants to send a Slack message to {target}", short(text, 2500)):
                return reply("The user denied sending this message.")
            await app.client.chat_postMessage(channel=channel, text=text)
            return reply(f"sent to {target}")

        @tool("upload_file", "Upload a local file (screenshot, image, log, PDF...) to Slack. Path may be relative "
              "to the working directory. Leave target empty to post in this thread; otherwise target is "
              "'#channel-name', a channel ID, '@username', or a user ID and the user must approve.",
              {"path": str, "comment": str, "target": str})
        async def upload_file(args):
            path = (self.cwd / Path(args["path"]).expanduser()).resolve()
            if not path.is_file():
                return reply(f"error: no file at {path}")
            target = (args.get("target") or "").strip()
            dest = {"channel": self.channel, "thread_ts": self.thread}
            if target:
                try:
                    dest = {"channel": await resolve_target(target)}
                except LookupError as e:
                    return reply(f"error: {e}")
                if not await self.ask(f"upload:{time.time()}", f"Claude wants to upload `{path.name}` to {target}",
                                      short(args.get("comment") or str(path), 2500)):
                    return reply("The user denied uploading this file.")
            await app.client.files_upload_v2(file=str(path), filename=path.name,
                                             initial_comment=args.get("comment") or None, **dest)
            return reply(f"uploaded {path.name}" + (f" to {target}" if target else ""))

        return create_sdk_mcp_server("slack", tools=[post_to_thread, send_message, upload_file])


async def resolve_target(target: str) -> str:
    """Turn '#name', '@name', or a raw ID into a channel ID to post to."""
    if target.startswith("#"):
        name, cursor = target[1:], None
        while True:
            r = await app.client.conversations_list(types="public_channel,private_channel", limit=1000, cursor=cursor)
            for c in r["channels"]:
                if c["name"] == name:
                    return c["id"]
            cursor = r.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                raise LookupError(f"no channel named {target}")
    user = target
    if target.startswith("@"):
        name, cursor, user = target[1:].lower(), None, None
        while not user:
            r = await app.client.users_list(limit=1000, cursor=cursor)
            for u in r["members"]:
                names = {u.get("name", ""), u["profile"].get("display_name", ""), u["profile"].get("real_name", "")}
                if name in {n.lower() for n in names}:
                    user = u["id"]
                    break
            cursor = r.get("response_metadata", {}).get("next_cursor")
            if not user and not cursor:
                raise LookupError(f"no user named {target}")
    if user.startswith(("U", "W")):
        return (await app.client.conversations_open(users=user))["channel"]["id"]
    return user  # channel ID


async def get_session(channel: str, thread: str) -> "Session | None":
    if thread in sessions:
        return sessions[thread]
    row = db.execute("SELECT session_id, cwd FROM sessions WHERE thread=?", (thread,)).fetchone()
    if not row:
        return None
    s = sessions[thread] = Session(channel, thread, Path(row[1]), resume=row[0])
    await s.start()
    return s


async def list_sessions() -> str:
    if not sessions:
        return "No open sessions."
    lines = []
    for s in sessions.values():
        link = (await app.client.chat_getPermalink(channel=s.channel, message_ts=s.thread))["permalink"]
        state = ":hourglass: working" if s.status_ts else ":zzz: idle"
        lines.append(f"• <{link}|{s.cwd.name}> {state}")
    return f"*{len(sessions)} open session(s)*\n" + "\n".join(lines)


async def configure(words: list[str], thread: str | None) -> str | None:
    """Handle `config`, `effort <x>`, `model <x>`. Returns a reply, or None if not a config command."""
    if words == ["config"]:
        return (f"model: `{setting('model', thread) or 'default'}` · effort: `{setting('effort', thread) or 'default'}`"
                f" · permissions: `{'skipped' if setting('mode', thread) == 'bypassPermissions' else 'ask'}`")
    if len(words) != 2 or words[0] not in ("effort", "model"):
        return None
    key, value = words[0], words[1]
    if value == "default":
        value = ""
    elif key == "effort" and value not in EFFORTS:
        return f"effort must be one of {', '.join(EFFORTS)} or default"
    save_setting(f"{thread}:{key}" if thread else key, value)
    shown = value or "default"
    if not thread:
        return f"New sessions will use {key} `{shown}`."
    s = sessions.get(thread)
    if s and s.status_ts:
        return f"{key} set to `{shown}`; it applies after the current turn when you `close` and reply again."
    if s:
        await s.close(quiet=True)  # reopened with the new setting on the next reply
    return f"This session will use {key} `{shown}` from your next message."


@app.event("app_mention")
async def on_mention(event, say):
    if event.get("user") != ALLOWED_USER:
        return
    thread = event.get("thread_ts") or event["ts"]
    words = event["text"].split()[1:]  # drop the <@bot> mention
    if words == ["sessions"]:
        await say(text=await list_sessions(), thread_ts=event.get("thread_ts"))
        return
    in_session = bool(event.get("thread_ts")) and (thread in sessions or db.execute(
        "SELECT 1 FROM sessions WHERE thread=?", (thread,)).fetchone())
    reply = await configure(words, thread if in_session else None)
    if reply:
        await say(text=reply, thread_ts=event.get("thread_ts"))
        return
    s = await get_session(event["channel"], thread)
    if not s:
        skip = any(w in SKIP_FLAGS for w in words)
        words = [w for w in words if w not in SKIP_FLAGS]
        cwd, words = resolve_cwd(words)
        if not words:
            await say(text=f"Usage: `@duo [repo] <task>` (default dir {DEFAULT_CWD})", thread_ts=thread)
            return
        if skip:
            save_setting(f"{thread}:mode", "bypassPermissions")
        s = sessions[thread] = Session(event["channel"], thread, cwd)
        await s.start()
        note = " :warning: permissions skipped, nothing will ask for approval" if skip else ""
        await say(text=f":robot_face: session started in `{cwd}`{note}", thread_ts=thread)
    await s.send(" ".join(words))


@app.event("message")
async def on_message(event):
    thread = event.get("thread_ts")
    if not thread or event.get("user") != ALLOWED_USER or event.get("subtype") or "<@" in event.get("text", ""):
        return  # mentions are handled by on_mention
    text = event["text"].strip()
    if text.lower() == "close":
        if thread in sessions:
            await sessions[thread].close()
        return
    if not (thread in sessions or db.execute("SELECT 1 FROM sessions WHERE thread=?", (thread,)).fetchone()):
        return
    reply = await configure(text.split(), thread)
    if reply:
        await app.client.chat_postMessage(channel=event["channel"], thread_ts=thread, text=reply)
        return
    s = await get_session(event["channel"], thread)
    if not s:
        return
    if text.lower() == "stop":
        await s.client.interrupt()
    else:
        await s.send(text)


@app.event("reaction_added")
async def on_reaction(event):
    if event.get("user") != ALLOWED_USER or event["reaction"] not in ("hand", "raised_hand", "octagonal_sign"):
        return
    item = event["item"]
    for s in sessions.values():
        if s.channel == item["channel"] and item["ts"] in (s.thread, s.status_ts):
            await s.client.interrupt()


async def on_button(ack, body, action, ok: bool):
    await ack()
    if body["user"]["id"] != ALLOWED_USER:
        return
    fut = pending.get(action["value"])
    if fut and not fut.done():
        fut.set_result(ok)
    verdict = ":white_check_mark: approved" if ok else ":no_entry: denied"
    await app.client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                                 text=f"{body['message']['text']} — {verdict}", blocks=[])


@app.action("approve")
async def on_approve(ack, body, action):
    await on_button(ack, body, action, True)


@app.action("deny")
async def on_deny(ack, body, action):
    await on_button(ack, body, action, False)


async def main():
    await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()


if __name__ == "__main__":
    asyncio.run(main())
