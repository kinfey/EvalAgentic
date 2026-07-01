"""FastAPI server: serves frontend + eval APIs (compression / routing)."""
import os
import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import eval as evalmod
import coding_agents
import gh_models

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
app = FastAPI(title="EvalAgentic")


class CompressReq(BaseModel):
    text: str
    task: str = "Summarize this person's core qualifications in one sentence."
    model: str = gh_models.LARGE_OPUS


class RouteReq(BaseModel):
    prompts: list[str]
    model: str = gh_models.LARGE_GPT


class CodingReq(BaseModel):
    requirement: str = (
        "Build a Taobao-like goods listing website using Python. "
        "Use HTML+JS for the frontend, Flask for the backend, and deploy it with Docker."
    )


@app.post("/api/compression")
async def compression(r: CompressReq):
    return await evalmod.eval_compression(r.text, r.task, r.model)


@app.post("/api/routing")
async def routing(r: RouteReq):
    return await evalmod.eval_routing(r.prompts, r.model)


@app.post("/api/coding")
async def coding(r: CodingReq):
    return await coding_agents.run_coding_eval(r.requirement)


@app.post("/api/coding/stream")
async def coding_stream(r: CodingReq):
    """SSE: stream per-agent execution steps to the frontend in real time."""
    q: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict):
        await q.put(ev)

    async def runner():
        try:
            res = await coding_agents.run_coding_eval(r.requirement, emit=emit)
            await q.put({"type": "result", "data": res})
        except Exception as ex:  # noqa: BLE001
            await q.put({"type": "error", "message": str(ex)})
        finally:
            await q.put(None)

    task = asyncio.create_task(runner())

    async def gen():
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        await task

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.on_event("shutdown")
async def _down():
    await gh_models.shutdown()


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
