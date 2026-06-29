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
    task: str = "用一句话总结此人核心资历。"
    model: str = gh_models.LARGE_OPUS


class RouteReq(BaseModel):
    prompts: list[str]
    model: str = gh_models.LARGE_GPT


class CodingReq(BaseModel):
    requirement: str = (
        "用 Python 做一个类似淘宝的网站, 只需要货物列表, 前端用 HTML+JS, "
        "后端用 Flask, 最后部署到 Docker 上。"
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
    """SSE: 逐个 Agent 执行步骤实时推送到前端。"""
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
