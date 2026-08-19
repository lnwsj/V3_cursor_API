"""WebSocket router (Phase 2.9)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

from fastapi import APIRouter, Cookie, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app.backend.deps import INTERNAL_TOKEN
from app.backend.services.jobs import get_job_owner
from app.backend.services.users import SESSION_KEYS, is_admin as _is_admin


router = APIRouter()


# In-memory pubsub broker
_JOB_SUBSCRIBERS: Dict[str, set] = {}
_JOB_BROKER_LOCK = asyncio.Lock()
_JOB_LAST_STATE: Dict[str, Dict[str, Any]] = {}


async def _publish_job_update(job_id: str, payload: Dict[str, Any]) -> None:
    """Broadcast a job update to all WebSocket subscribers (FIX 2026-08-19)."""
    payload.setdefault("ts", time.time())
    payload.setdefault("job_id", job_id)
    _JOB_LAST_STATE[job_id] = payload
    async with _JOB_BROKER_LOCK:
        subs = list(_JOB_SUBSCRIBERS.get(job_id, set()))
    dead = []
    for ws in subs:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    if dead:
        async with _JOB_BROKER_LOCK:
            for ws in dead:
                _JOB_SUBSCRIBERS.get(job_id, set()).discard(ws)


@router.websocket("/ws/jobs/{job_id}")
async def ws_job_updates(websocket: WebSocket, job_id: str):
    """Real-time job updates WebSocket (FIX 2026-08-19).

    Auth: pass bearer token in cookie (cutdee_session) or Sec-WebSocket-Protocol header.
    """
    token = None
    cookie_token = websocket.cookies.get("cutdee_session")
    if cookie_token:
        token = cookie_token
    proto = websocket.headers.get("sec-websocket-protocol", "")
    if proto.startswith("bearer."):
        token = proto[7:]
    if not token:
        await websocket.close(code=4401, reason="auth required")
        return
    try:
        user = SESSION_KEYS.get(token)
        if not user:
            # try other paths
            from app.backend.services.users import resolve_token_to_user
            user = resolve_token_to_user(token, (), INTERNAL_TOKEN)
    except HTTPException:
        await websocket.close(code=4401, reason="invalid token")
        return

    # Verify job ownership
    owner = get_job_owner(job_id)
    if not owner or (not _is_admin(user) and owner != user):
        await websocket.close(code=4404, reason="job not found")
        return

    accept_subprotocol = "bearer." + token if proto.startswith("bearer.") else None
    await websocket.accept(subprotocol=accept_subprotocol)
    async with _JOB_BROKER_LOCK:
        _JOB_SUBSCRIBERS.setdefault(job_id, set()).add(websocket)
    try:
        last = _JOB_LAST_STATE.get(job_id)
        await websocket.send_json({
            "type": "hello",
            "job_id": job_id,
            "status": owner,
            "last_state": last,
            "server_time": time.time(),
        })
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=20.0)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        async with _JOB_BROKER_LOCK:
            _JOB_SUBSCRIBERS.get(job_id, set()).discard(websocket)


@router.post("/api/v1/internal/jobs/{job_id}/publish")
async def api_internal_publish(job_id: str, payload: dict, _=None):
    """Internal publish for workers → broker."""
    await _publish_job_update(job_id, payload)
    return {"ok": True, "subscribers": len(_JOB_SUBSCRIBERS.get(job_id, set()))}
