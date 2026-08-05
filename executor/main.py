#!/usr/bin/env python3
"""Vibecode executor — runs Claude tasks triggered by Discord bot buttons."""

import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXECUTOR_TOKEN = os.environ["EXECUTOR_TOKEN"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
PROJECT_PLANS_CHANNEL = os.environ.get("DISCORD_PROJECT_PLANS_CHANNEL", "1534627545495113828")
GUILD_ID = "1145020158579052624"
CLAUDE_BIN = "/usr/local/bin/claude"

TASKS: dict[str, str] = {
    "fix_memory": (
        "Review all memory files in ~/.claude/projects/-root/memory/ — "
        "touch any that are older than 90 days but still accurate, update ones that are stale or wrong. "
        "Print a brief summary of what you changed."
    ),
    "investigate_ci": (
        "Check recent failed CI runs across all repos in /root/projects/repos using gh CLI. "
        "For each failure, identify the root cause. "
        "Print a concise summary of findings."
    ),
    "create_doc": (
        "Create a missing notes/projects/{repo}.md file for the repo specified. "
        "Use the /document skill. Print what was created."
    ),
    "docker_cleanup": (
        "Run Docker cleanup: remove dangling images, stopped containers, unused volumes. "
        "Print what was removed."
    ),
    "create_docs": (
        "Find all repos in /root/projects/repos/ missing a notes file in "
        "/root/projects/notes/projects/. For each missing one, create a comprehensive "
        "notes/projects/<repo>.md documenting: what the project is, tech stack, "
        "deployment/hosting, current status, last activity. "
        "Commit each file to /root/projects/notes with message '<repo>: add project notes'. "
        "Print a summary of what was created."
    ),
    "review_projects": (
        "Analyze repos in /root/projects/repos that have health issues: inactive >90 days, "
        "missing CI, or missing tests. For each repo, determine what code changes would help. "
        "Update /root/projects/notes/projects/<repo>.md directly if the file exists and needs updating. "
        "Then write a structured plan for any code changes needed (CI workflows, test setup). "
        "Format the plan as markdown with one section per repo. "
        "Output ONLY the plan markdown — nothing else. If no code changes needed, output: NO_PLAN"
    ),
}

_active_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _active_tasks:
        log.info("Graceful shutdown: waiting for %d active task(s)...", len(_active_tasks))
        await asyncio.gather(*_active_tasks, return_exceptions=True)
        log.info("All tasks completed, shutting down.")


app = FastAPI(title="vibecode-executor", lifespan=lifespan)


class ExecuteRequest(BaseModel):
    task: str
    params: dict = {}


def _auth(request: Request) -> None:
    token = request.headers.get("Authorization", "")
    if token != f"Bearer {EXECUTOR_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _post_discord(message: str, channel_id: str = "") -> str:
    """Post message to Discord channel. Returns message ID or empty string."""
    target = channel_id or DISCORD_CHANNEL_ID
    headers = {"User-Agent": "DiscordBot (https://github.com/Xoudusz/vibecode-bot, 1.0.0)"}
    if DISCORD_BOT_TOKEN and target:
        url = f"https://discord.com/api/v10/channels/{target}/messages"
        headers["Authorization"] = f"Bot {DISCORD_BOT_TOKEN}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"content": message[:2000]}, headers=headers)
            if resp.is_success:
                return resp.json().get("id", "")
    elif DISCORD_WEBHOOK:
        async with httpx.AsyncClient() as client:
            await client.post(DISCORD_WEBHOOK, json={"content": message[:2000]})
    return ""


async def _edit_discord(channel_id: str, message_id: str, content: str) -> None:
    if not (DISCORD_BOT_TOKEN and channel_id and message_id):
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    async with httpx.AsyncClient() as client:
        await client.patch(url, json={"content": content[:2000]}, headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "User-Agent": "DiscordBot (https://github.com/Xoudusz/vibecode-bot, 1.0.0)",
        })


async def _run_claude_streaming(
    prompt: str, channel_id: str, msg_id: str,
    label: str = "Running", timeout: int = 600,
) -> tuple[str, int]:
    """Run Claude with stream-json output, editing Discord message live every 30s."""
    env = os.environ.copy()
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
        "--output-format", "stream-json", "--verbose", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env, cwd="/root",
    )

    recent: list[str] = []
    start = asyncio.get_event_loop().time()
    last_edit = start
    final_output = ""

    async def _read() -> None:
        nonlocal last_edit, final_output
        async for raw in proc.stdout:
            line = raw.decode().strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tool = block.get("name", "")
                        inp = block.get("input", {})
                        detail = inp.get("command", inp.get("file_path", ""))[:50]
                        recent.append(f"{tool}: {detail}" if detail else tool)
                        if len(recent) > 5:
                            recent.pop(0)
            elif ev.get("type") == "result":
                final_output = ev.get("result", "")

            now = asyncio.get_event_loop().time()
            if now - last_edit >= 30 and recent:
                elapsed = int((now - start) / 60)
                lines = "\n".join(f"• {t}" for t in recent[-3:])
                await _edit_discord(
                    channel_id, msg_id,
                    f"⚙️ {label}... ({elapsed}m)\n```\n{lines}\n```",
                )
                last_edit = now

    try:
        await asyncio.wait_for(asyncio.gather(_read(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Timed out after {timeout // 60}min", -1

    if not final_output:
        stderr = (await proc.stderr.read()).decode()
        final_output = stderr or "No output"

    return final_output, proc.returncode if proc.returncode is not None else 0


AUDIT_SCRIPT = "/root/.claude/skills/housekeeping/scripts/audit.py"
AUDIT_CHANNEL = os.environ.get("DISCORD_AUDIT_CHANNEL_ID", "")

_BOT_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "User-Agent": "DiscordBot (https://github.com/Xoudusz/vibecode-bot, 1.0.0)",
    "Content-Type": "application/json",
}


async def _post_plan_thread(plan_content: str) -> str:
    """Post plan to project-plans forum. Returns thread URL or empty string."""
    thread_name = f"Plan {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    payload = {
        "name": thread_name,
        "auto_archive_duration": 10080,
        "message": {
            "embeds": [{"description": plan_content[:4096], "color": 0x3498DB}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "✅ Approve", "custom_id": "approve_plan"},
                {"type": 2, "style": 4, "label": "❌ Reject", "custom_id": "reject_plan"},
            ]}],
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://discord.com/api/v10/channels/{PROJECT_PLANS_CHANNEL}/threads",
            content=json.dumps(payload), headers=_BOT_HEADERS,
        )
        if resp.is_success:
            thread_id = resp.json().get("id", "")
            return f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
    return ""


async def _fetch_thread_content(channel_id: str) -> str:
    """Fetch plan content from a thread (content or embed description)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=5",
            headers=_BOT_HEADERS,
        )
        if resp.is_success:
            msgs = resp.json()
            parts = []
            for m in reversed(msgs):
                if m.get("content"):
                    parts.append(m["content"])
                for e in m.get("embeds", []):
                    if e.get("description"):
                        parts.append(e["description"])
            return "\n\n".join(parts)
    return ""


async def _patch_interaction(app_id: str, token: str, content: str) -> None:
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    headers = {
        "User-Agent": "DiscordBot (https://github.com/Xoudusz/vibecode-bot, 1.0.0)",
    }
    async with httpx.AsyncClient() as client:
        await client.patch(url, json={"content": content, "flags": 64}, headers=headers)


async def _run_housekeeping(reply_channel: str, interaction_token: str = "", interaction_app_id: str = "") -> None:
    log.info("Running housekeeping audit")
    try:
        env = os.environ.copy()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["python3", AUDIT_SCRIPT, "--discord"],
                capture_output=True, text=True, timeout=120, env=env,
                cwd="/root"
            )
        )
        thread_url = result.stdout.strip() if result.returncode == 0 else ""
        if thread_url:
            thread_name = f"Audit {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            msg = f"📋 [{thread_name}]({thread_url})"
        else:
            err = (result.stderr or "unknown error")[:300]
            msg = f"❌ Audit failed: {err}"

        if interaction_token and interaction_app_id:
            await _patch_interaction(interaction_app_id, interaction_token, msg)
        else:
            await _post_discord(msg, reply_channel)
    except Exception as e:
        msg = f"❌ Audit error: {e}"
        if interaction_token and interaction_app_id:
            await _patch_interaction(interaction_app_id, interaction_token, msg)
        else:
            await _post_discord(msg, reply_channel)


async def _run_task(task: str, params: dict) -> None:
    reply_channel = params.pop("reply_channel_id", "")

    if task == "run_housekeeping":
        interaction_token = params.pop("interaction_token", "")
        interaction_app_id = params.pop("app_id", "")
        await _run_housekeeping(reply_channel, interaction_token, interaction_app_id)
        return

    if task == "approve_plan":
        plan = await _fetch_thread_content(reply_channel)
        if not plan:
            await _post_discord("❌ Could not read plan from thread.", reply_channel)
            return
        prompt = f"Implement this plan exactly as described. Commit and push any code changes. Do not post to Discord or send any notifications.\n\n{plan}"
        msg_id = await _post_discord("⚙️ Implementing plan...", reply_channel)
        output, returncode = await _run_claude_streaming(
            prompt, reply_channel, msg_id, label="Implementing plan", timeout=600
        )
        status = "✅" if returncode == 0 else "❌"
        await _edit_discord(reply_channel, msg_id, f"{status} Plan executed:\n```\n{output[:1800]}\n```")
        return

    if task == "reject_plan":
        await _post_discord("❌ Plan rejected.", reply_channel)
        return

    if task == "review_projects":
        prompt = TASKS["review_projects"]
        msg_id = await _post_discord("⚙️ Reviewing projects...", reply_channel)
        plan_content, _ = await _run_claude_streaming(
            prompt, reply_channel, msg_id, label="Reviewing projects", timeout=300
        )
        plan_content = plan_content.strip()
        if not plan_content or plan_content == "NO_PLAN":
            await _edit_discord(reply_channel, msg_id, "✅ Projects reviewed — no code changes needed.")
        else:
            thread_url = await _post_plan_thread(plan_content)
            link = f" → [View plan]({thread_url})" if thread_url else ""
            await _edit_discord(reply_channel, msg_id, f"✅ Plan posted to #project-plans{link}")
        return

    prompt = TASKS[task]
    for k, v in params.items():
        prompt = prompt.replace(f"{{{k}}}", v)

    log.info("Running task: %s (reply_channel: %s)", task, reply_channel)
    await _post_discord(f"⚙️ Running `{task}`...", reply_channel)

    try:
        env = os.environ.copy()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [CLAUDE_BIN, "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep", "-p", prompt],
                capture_output=True, text=True, timeout=300, env=env,
                cwd="/root"
            )
        )
        output = (result.stdout or result.stderr or "No output")[:1800]
        status = "✅" if result.returncode == 0 else "❌"
        await _post_discord(f"{status} `{task}` done:\n```\n{output}\n```", reply_channel)
    except asyncio.TimeoutError:
        await _post_discord(f"⏱️ `{task}` timed out after 5 min", reply_channel)
    except Exception as e:
        await _post_discord(f"❌ `{task}` error: {e}", reply_channel)


@app.post("/execute")
async def execute(request: Request, body: ExecuteRequest):
    _auth(request)

    allowed = set(TASKS) | {"run_housekeeping", "approve_plan", "reject_plan"}
    if body.task not in allowed:
        raise HTTPException(status_code=400, detail=f"Unknown task. Allowed: {sorted(allowed)}")

    task = asyncio.create_task(_run_task(body.task, body.params))
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return {"status": "queued", "task": body.task, "queued_at": datetime.now().isoformat()}


@app.get("/health")
async def health():
    return {"status": "ok", "tasks": list(TASKS)}
