"""Token 计数器架构: INTERCEPTOR(@token_meter 装饰器) -> COUNTER CORE(记账/阈值/触发) -> ACTION HUB.

Non-invasive: decorate any LLM call. Counts tokens with tiktoken, accrues cost,
evaluates a budget, and emits throttle/downgrade/rollback actions.
"""
import functools
import time
from dataclasses import dataclass, field

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")

# Per-tier price per 1K tokens (matches the routing tree)
PRICE = {"TINY": 0.001, "MID": 0.012, "LARGE": 0.030}


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_enc.encode(text))


@dataclass
class CounterCore:
    """计数核心: 记账 -> 阈值评估 -> 触发动作 (内存; 可扩展 Redis 双写)."""
    budget: int = 1_000_000
    total_prompt: int = 0
    total_completion: int = 0
    total_cost: float = 0.0
    events: list = field(default_factory=list)
    actions: list = field(default_factory=list)

    def record(self, label, model, tier, prompt_tok, completion_tok, latency):
        cost = (prompt_tok + completion_tok) / 1000 * PRICE.get(tier, 0.030)
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
    """装饰器: 挂在每个 LLM 调用上, ask(prompt, model)->(text,latency)."""
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
