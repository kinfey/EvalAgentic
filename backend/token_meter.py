"""Token counter architecture: INTERCEPTOR (@token_meter decorator) -> COUNTER CORE (accounting/threshold/trigger) -> ACTION HUB.

Non-invasive: decorate any LLM call. Counts tokens with tiktoken, accrues cost,
evaluates a budget, and emits throttle/downgrade/rollback actions.
"""
import functools
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field

import tiktoken

logger = logging.getLogger(__name__)
_enc = None
_fallback_warned = False
_FALLBACK_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7F]|[^\w\s]", re.ASCII)
_CL100K_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
_CL100K_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

# 1 GitHub Copilot AI credit = $0.01 USD.
AI_CREDIT_USD = 0.01

# GitHub Copilot model pricing — Credits per 1M tokens (from the Copilot models table).
# Single unified rate per model; no individual/enterprise distinction.
MODEL_CREDITS_PER_1M = {
    "claude-haiku-4.5": {"input": 100, "output": 500},
    "claude-opus-4.6": {"input": 500, "output": 2500},
    "claude-opus-4.7": {"input": 500, "output": 2500},
    "claude-opus-4.8": {"input": 500, "output": 2500},
    "claude-sonnet-4.5": {"input": 300, "output": 1500},
    "claude-sonnet-4.6": {"input": 300, "output": 1500},
    "claude-sonnet-5": {"input": 200, "output": 1000},
    "gemini-2.5-pro": {"input": 125, "output": 1000},
    "gemini-3-flash": {"input": 50, "output": 300},
    "gemini-3.1-pro": {"input": 200, "output": 1200},
    "gemini-3.5-flash": {"input": 150, "output": 900},
    "gpt-5.3-codex": {"input": 175, "output": 1400},
    "gpt-5.4": {"input": 250, "output": 1500},
    "gpt-5.4-mini": {"input": 75, "output": 450},
    "gpt-5.5": {"input": 500, "output": 3000},
    "mai-code-1-flash-picker": {"input": 75, "output": 450},
}

# Fallback rates when a model name is unknown, keyed by routing tier.
TIER_FALLBACK_CREDITS_PER_1M = {
    "TINY": MODEL_CREDITS_PER_1M["mai-code-1-flash-picker"],
    "MID": MODEL_CREDITS_PER_1M["gpt-5.4-mini"],
    "LARGE": MODEL_CREDITS_PER_1M["gpt-5.5"],
}


def normalize_model_name(model: str) -> str:
    return (model or "").strip().lower()


def credits_to_usd(credits: float) -> float:
    return credits * AI_CREDIT_USD


def usd_to_ai_credits(usd: float) -> float:
    return usd / AI_CREDIT_USD


def get_model_credits_per_1m(model: str, tier: str) -> dict:
    m = normalize_model_name(model)
    if m in MODEL_CREDITS_PER_1M:
        return MODEL_CREDITS_PER_1M[m]
    return TIER_FALLBACK_CREDITS_PER_1M.get(tier, TIER_FALLBACK_CREDITS_PER_1M["LARGE"])


def estimate_cost_credits(prompt_tok: int, completion_tok: int, model: str, tier: str) -> float:
    c = get_model_credits_per_1m(model, tier)
    return (prompt_tok / 1_000_000 * c["input"]) + (completion_tok / 1_000_000 * c["output"])


def estimate_cost_usd(prompt_tok: int, completion_tok: int, model: str, tier: str) -> float:
    return credits_to_usd(estimate_cost_credits(prompt_tok, completion_tok, model, tier))


def estimate_input_cost_usd(prompt_tok: int, model: str, tier: str) -> float:
    c = get_model_credits_per_1m(model, tier)
    return credits_to_usd(prompt_tok / 1_000_000 * c["input"])


def cost_summary(cost_usd: float) -> dict:
    return {
        "usd": round(cost_usd, 6),
        "credits": round(usd_to_ai_credits(cost_usd), 2),
    }


def compare_cost(before_usd: float, after_usd: float) -> dict:
    saved = before_usd - after_usd
    return {
        "before": cost_summary(before_usd),
        "after": cost_summary(after_usd),
        "saved_usd": round(saved, 6),
        "saved_credits": round(usd_to_ai_credits(saved), 2),
    }


def _get_encoder():
    global _enc, _fallback_warned
    if _enc is not None:
        return _enc
    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR")
    if cache_dir is None:
        cache_dir = os.environ.get("DATA_GYM_CACHE_DIR")
    if cache_dir is None:
        cache_dir = os.path.join(tempfile.gettempdir(), "data-gym-cache")
    cache_key = hashlib.sha1(_CL100K_URL.encode()).hexdigest()
    cache_path = os.path.join(cache_dir, cache_key) if cache_dir else ""
    if not cache_path or not os.path.exists(cache_path):
        if not _fallback_warned:
            logger.warning(
                "tiktoken cache for cl100k_base not found, using approximate token counts"
            )
            _fallback_warned = True
        return None
    try:
        with open(cache_path, "rb", buffering=0) as f:
            cached = f.read()
        if hashlib.sha256(cached).hexdigest() != _CL100K_HASH:
            raise ValueError("cached cl100k_base hash mismatch")
        _enc = tiktoken.get_encoding("cl100k_base")
    except (OSError, ValueError) as ex:
        if not _fallback_warned:
            logger.warning("tiktoken encoder unavailable, using approximate token counts: %s", ex)
            _fallback_warned = True
        return None
    return _enc


def _count_tokens_fallback(text: str) -> int:
    total = 0
    for chunk in _FALLBACK_TOKEN_RE.findall(text):
        if chunk.isascii() and (chunk[0].isalnum() or chunk[0] == "_"):
            total += max(1, math.ceil(len(chunk) / 4))
        else:
            total += 1
    return total


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return _count_tokens_fallback(text)
    return len(enc.encode(text))


@dataclass
class CounterCore:
    """Counter core: accounting -> threshold evaluation -> action triggering (in-memory; can be extended to Redis)."""
    budget: int = 1_000_000
    total_prompt: int = 0
    total_completion: int = 0
    total_cost: float = 0.0
    events: list = field(default_factory=list)
    actions: list = field(default_factory=list)

    def record(self, label, model, tier, prompt_tok, completion_tok, latency):
        cost = estimate_cost_usd(prompt_tok, completion_tok, model, tier)
        self.total_prompt += prompt_tok
        self.total_completion += completion_tok
        self.total_cost += cost
        ev = {
            "label": label, "model": model, "tier": tier,
            "prompt_tokens": prompt_tok, "completion_tokens": completion_tok,
            "total_tokens": prompt_tok + completion_tok,
            "cost_usd": round(cost, 6), "latency_ms": latency,
        }
        self.events.append(ev)
        used = self.total_prompt + self.total_completion
        if used > self.budget:
            self.actions.append({"action": "rollback", "used": used, "budget": self.budget})
        elif used > self.budget * 0.8:
            self.actions.append({"action": "throttle", "used": used, "budget": self.budget})
        return ev

    def snapshot(self):
        return {
            "total_prompt": self.total_prompt, "total_completion": self.total_completion,
            "total_tokens": self.total_prompt + self.total_completion,
            "total_cost_usd": round(self.total_cost, 6),
            "events": self.events, "actions": self.actions,
        }


def token_meter(core: CounterCore, label: str, model: str, tier: str):
    """Decorator: wrap each LLM call, ask(prompt, model) -> (text, latency)."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrap(prompt, *a, **kw):
            t0 = time.time()
            text, latency = await fn(prompt, *a, **kw)
            core.record(label, model, tier, count_tokens(prompt), count_tokens(text),
                        latency or int((time.time() - t0) * 1000))
            return text, latency
        return wrap
    return deco
