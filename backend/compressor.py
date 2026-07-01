"""Compression: detect redundancy -> extract JSON -> dynamic injection -> 24h cache reuse."""
import hashlib
import json
import time

import gh_models

_CACHE: dict[str, tuple[float, dict]] = {}
TTL = 24 * 3600

EXTRACT_SYS = (
    "You are a structured extractor. Compress long-tail natural language into minimal JSON and remove filler wording. "
    "Output JSON only with no explanation. Keep fields concise: name, degree, school, major, year, achievements."
)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def compress(text: str, fields: list[str] | None = None) -> dict:
    k = _key(text)
    hit = _CACHE.get(k)
    if hit and time.time() - hit[0] < TTL:
        return {**hit[1], "cached": True}
    prompt = f"Compress the text below into JSON:\n{text}"
    if fields:
        prompt += f"\nKeep only these fields: {fields}"
    raw, _ = await gh_models.ask(prompt, gh_models.TINY, system=EXTRACT_SYS)
    s, e = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[s:e + 1])
    except Exception:
        data = {"raw": raw}
    result = {"json": data, "compact": json.dumps(data, ensure_ascii=False, separators=(",", ":")), "cached": False}
    _CACHE[k] = (time.time(), result)
    return result
