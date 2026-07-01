"""GitHub Copilot SDK wrapper.

Sends a single prompt to a model and returns (text, latency_ms). Reuses one
CopilotClient process for the whole run. Token counting is done by token_meter
via tiktoken so it is provider-independent.
"""
import asyncio
import time

from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.session_events import AssistantMessageData, SessionIdleData

# Models verified available via client.list_models()
LARGE_OPUS = "claude-opus-4.8"
LARGE_GPT = "gpt-5.5"
MID = "gpt-5.4-mini"
TINY = "mai-code-1-flash-picker"

_client: CopilotClient | None = None
_lock = asyncio.Lock()


async def _get_client() -> CopilotClient:
    global _client
    async with _lock:
        if _client is None:
            _client = CopilotClient()
            await _client.start()
        return _client


async def ask(prompt: str, model: str, system: str | None = None) -> tuple[str, int]:
    """Send prompt, return (assistant_text, latency_ms)."""
    client = await _get_client()
    cfg = dict(on_permission_request=PermissionHandler.approve_all, model=model)
    if system:
        cfg["system_message"] = {"mode": "append", "content": system}
    session = await client.create_session(**cfg)
    done = asyncio.Event()
    chunks: list[str] = []

    def on_event(event):
        data = event.data
        if isinstance(data, AssistantMessageData):
            chunks.append(data.content)
        elif isinstance(data, SessionIdleData):
            done.set()

    session.on(on_event)
    t0 = time.time()
    await session.send(prompt)
    await done.wait()
    latency = int((time.time() - t0) * 1000)
    try:
        await session.disconnect()
    except Exception:
        pass
    return ("".join(chunks).strip(), latency)


async def shutdown():
    global _client
    if _client is not None:
        await _client.stop()
        _client = None
