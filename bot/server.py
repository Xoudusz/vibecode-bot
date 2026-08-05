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
FOLLOWUP_URL = "https://discord.com/api/v10/webhooks/{app_id}/{token}"

app = FastAPI(title="vibecode-bot")


async def _run_cleanup_and_reply(app_id: str, token: str, custom_id: str) -> None:
    handler = HANDLERS.get(custom_id)
    if handler is None:
        result = f"Unknown action: {custom_id}"
    else:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, handler)
        except Exception as e:
            result = f"Error: {e}"

    url = FOLLOWUP_URL.format(app_id=app_id, token=token)
    payload = {"content": f"```\n{result[:1900]}\n```", "flags": 64}  # ephemeral
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

    # Discord PING
    if interaction_type == 1:
        return {"type": 1}

    # Button component interaction
    if interaction_type == 3:
        custom_id = data.get("data", {}).get("custom_id", "")
        token = data.get("token", "")
        app_id = data.get("application_id", APP_ID)

        log.info("Button pressed: %s", custom_id)
        asyncio.create_task(_run_cleanup_and_reply(app_id, token, custom_id))
        return {"type": 5}  # deferred update

    return {"type": 1}


@app.get("/health")
async def health():
    return {"status": "ok"}
