"""Slack <-> Claude Code bridge (Socket Mode).

@duo [repo] <task>   start a session in a thread (repo = folder under WORK_ROOTS)
reply in thread         steer / continue the session
"stop" or ✋ reaction    interrupt the current turn
"close"                 end the session (reply later to resume it)
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
    TextBlock,
    ToolUseBlock,
)
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

ALLOWED_USER = os.environ["SLACK_ALLOWED_USER"]
WORK_ROOTS = [Path(p).expanduser() for p in os.environ.get("WORK_ROOTS", "~/work/insighture:~/work").split(":")]
DEFAULT_CWD = WORK_ROOTS[0]
SAFE_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch", "WebFetch", "Task", "NotebookRead"}
UPDATE_EVERY = 3  # seconds between status edits
SLACK_LIMIT = 3500

app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
db = sqlite3.connect(Path(__file__).with_name("sessions.db"))
db.execute("CREATE TABLE IF NOT EXISTS sessions (thread TEXT PRIMARY KEY, channel TEXT, session_id TEXT, cwd TEXT)")
sessions: dict[str, "Session"] = {}
pending: dict[str, asyncio.Future] = {}  # tool_use_id -> approval future


def resolve_cwd(words: list[str]) -> tuple[Path, list[str]]:
    if words:
        for root in WORK_ROOTS:
            if (root / words[0]).is_dir():
                return root / words[0], words[1:]
    return DEFAULT_CWD, words


def short(obj, n=300) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj)
    return s if len(s) <= n else s[:n] + "…"


class Session:
    def __init__(self, channel: str, thread: str, cwd: Path, resume: str | None = None):
        self.channel, self.thread, self.cwd = channel, thread, cwd
        self.client = ClaudeSDKClient(ClaudeAgentOptions(
            cwd=str(cwd),
            resume=resume,
            permission_mode="acceptEdits",
            can_use_tool=self.approve,
            setting_sources=["user", "project", "local"],
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

    async def close(self):
        self.closing = True
        sessions.pop(self.thread, None)
        await self.client.disconnect()
        await self.post(":lock: session closed. Reply here to resume it.")

    async def approve(self, tool: str, inp: dict, ctx):
        if tool in SAFE_TOOLS:
            return PermissionResultAllow()
        fut = asyncio.get_running_loop().create_future()
        pending[ctx.tool_use_id] = fut
        prompt = ctx.title or f"Claude wants to use {tool}"
        await app.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread, text=prompt,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{prompt}*\n```{short(inp, 1500)}```"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
                     "action_id": "approve", "value": ctx.tool_use_id},
                    {"type": "button", "text": {"type": "plain_text", "text": "Deny"}, "style": "danger",
                     "action_id": "deny", "value": ctx.tool_use_id},
                ]},
            ])
        try:
            ok = await fut
        finally:
            pending.pop(ctx.tool_use_id, None)
        return PermissionResultAllow() if ok else PermissionResultDeny(message="Denied by user in Slack")


async def get_session(channel: str, thread: str) -> "Session | None":
    if thread in sessions:
        return sessions[thread]
    row = db.execute("SELECT session_id, cwd FROM sessions WHERE thread=?", (thread,)).fetchone()
    if not row:
        return None
    s = sessions[thread] = Session(channel, thread, Path(row[1]), resume=row[0])
    await s.start()
    return s


@app.event("app_mention")
async def on_mention(event, say):
    if event.get("user") != ALLOWED_USER:
        return
    thread = event.get("thread_ts") or event["ts"]
    words = event["text"].split()[1:]  # drop the <@bot> mention
    s = await get_session(event["channel"], thread)
    if not s:
        cwd, words = resolve_cwd(words)
        if not words:
            await say(text=f"Usage: `@duo [repo] <task>` (default dir {DEFAULT_CWD})", thread_ts=thread)
            return
        s = sessions[thread] = Session(event["channel"], thread, cwd)
        await s.start()
        await say(text=f":robot_face: session started in `{cwd}`", thread_ts=thread)
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
