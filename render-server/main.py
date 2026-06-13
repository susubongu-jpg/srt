import asyncio
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bot import BotSession, sessions

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRequest(BaseModel):
    userId: str
    password: str
    depStation: str
    arrStation: str
    date: str
    depTime: str
    depMinute: str
    endTime: str = "23"
    endMinute: str = "50"
    intervalMs: str = "1000"
    fcmToken: str = ""

@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/bot/start")
async def start_bot(req: StartRequest):
    session_id = str(uuid.uuid4())[:8]
    session = BotSession(session_id, req.model_dump())
    sessions[session_id] = session
    asyncio.create_task(session.run())
    return {"sessionId": session_id}


@app.post("/bot/stop/{session_id}")
async def stop_bot(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.stop()
    return {"ok": True}


@app.get("/bot/status/{session_id}")
async def get_status(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.get_status()

