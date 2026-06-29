"""压缩: 识别冗余 -> Copilot 结构化抽取为 JSON -> 动态注入 -> 24h 缓存复用."""
import hashlib
import json
import time

import gh_models

_CACHE: dict[str, tuple[float, dict]] = {}
TTL = 24 * 3600

EXTRACT_SYS = (
    "你是结构化抽取器。把长尾自然语言压缩成最小 JSON, 丢掉所有修饰语。"
    "只输出 JSON, 不要解释。字段精炼: name, degree, school, major, year, achievements。"
)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def compress(text: str, fields: list[str] | None = None) -> dict:
    k = _key(text)
    hit = _CACHE.get(k)
    if hit and time.time() - hit[0] < TTL:
        return {**hit[1], "cached": True}
    prompt = f"把下面文本压缩为 JSON:\n{text}"
    if fields:
        prompt += f"\n只保留字段: {fields}"
    raw, _ = await gh_models.ask(prompt, gh_models.TINY, system=EXTRACT_SYS)
    s, e = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[s:e + 1])
    except Exception:
        data = {"raw": raw}
    result = {"json": data, "compact": json.dumps(data, ensure_ascii=False, separators=(",", ":")), "cached": False}
    _CACHE[k] = (time.time(), result)
    return result
