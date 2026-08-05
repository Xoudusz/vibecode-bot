import asyncio
import json
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException, Request

from bot.cleanup import HANDLERS
from bot.verify import verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

APP_ID = os.environ["DISCORD_APPLICATION_ID"]
EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "")
EXECUTOR_TOKEN = os.environ.get("EXECUTOR_TOKEN", "")
FOLLOWUP_URL = "https://discord.com/api/v10/webhooks/{app_id}/{token}"
BOT_COMMANDS_CHANNEL = "1534613643533095023"

# Tasks routed to executor (require Claude/local filesystem)
EXECUTOR_TASKS = {"fix_memory", "investigate_ci", "create_doc", "docker_cleanup", "run_housekeeping"}

app = FastAPI(title="vibecode-bot")


async def _call_executor(custom_id: str, params: dict = {}) -> str:
    if not EXECUTOR_URL or not EXECUTOR_TOKEN:
        return "Executor not configured"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EXECUTOR_URL}/execute",
            json={"task": custom_id, "params": params},
            headers={"Authorization": f"Bearer {EXECUTOR_TOKEN}"},
            timeout=10,
        )
        if resp.is_success:
            return f"Queued: `{custom_id}`"
        return f"Executor error: {resp.status_code}"


async def _run_cleanup_and_reply(app_id: str, token: str, custom_id: str, channel_id: str) -> None:
    url = FOLLOWUP_URL.format(app_id=app_id, token=token)

    if custom_id in EXECUTOR_TASKS:
        result = await _call_executor(custom_id, params={"reply_channel_id": channel_id})
    else:
        handler = HANDLERS.get(custom_id)
        if handler is None:
            result = f"Unknown action: {custom_id}"
        else:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, handler)
            except Exception as e:
                result = f"Error: {e}"

    if custom_id == "run_housekeeping":
        payload = {"content": "Audit running — check <#1534601541736988862>", "flags": 64}
    else:
        payload = {"content": f"```\n{result[:1900]}\n```"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        if not resp.is_success:
            log.error("Followup failed: %s %s", resp.status_code, resp.text)


@app.post("/interactions")
async def interactions(request: Request):
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    body = await request.body()

    if not verify_signature(body, signature, timestamp):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(body)
    interaction_type = data.get("type")

    if interaction_type == 1:
        return {"type": 1}

    if interaction_type == 2:
        command = data.get("data", {}).get("name", "")
        token = data.get("token", "")
        app_id = data.get("application_id", APP_ID)
        channel_id = data.get("channel_id", "")

        if command == "housekeeping" and channel_id == BOT_COMMANDS_CHANNEL:
            log.info("Slash command: /housekeeping")
            asyncio.create_task(_run_cleanup_and_reply(app_id, token, "run_housekeeping", channel_id))
            return {"type": 5}

        return {"type": 4, "data": {"content": "Unknown command.", "flags": 64}}

    if interaction_type == 3:
        custom_id = data.get("data", {}).get("custom_id", "")
        token = data.get("token", "")
        app_id = data.get("application_id", APP_ID)

        channel_id = data.get("channel_id", "")
        log.info("Button pressed: %s (channel: %s)", custom_id, channel_id)
        asyncio.create_task(_run_cleanup_and_reply(app_id, token, custom_id, channel_id))
        return {"type": 5}

    return {"type": 1}


@app.get("/health")
async def health():
    return {"status": "ok"}
