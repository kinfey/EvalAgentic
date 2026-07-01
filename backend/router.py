"""On-demand model routing tree.

INCOMING REQUEST
    -> Prompt < 500 tokens? YES -> TINY/SMALL (classify/extract)
                                                  NO -> multi-step reasoning? NO -> MID (dialogue/summary)
                                                                                    YES -> LARGE (agent/code)
"""
from token_meter import count_tokens, get_model_credits_per_1m
import gh_models

MULTISTEP_HINTS = (
    "step by step",
    "multi-step",
    "reasoning",
    "agent",
    "code",
    "plan",
    "debug",
)
SIMPLE_HINTS = ("classify", "extraction", "extract", "label", "sentiment", "yes/no")


def route(prompt: str, large_model: str = gh_models.LARGE_GPT) -> dict:
    tokens = count_tokens(prompt)
    low = prompt.lower()
    if tokens < 500 or any(h in low for h in SIMPLE_HINTS):
        p = get_model_credits_per_1m(gh_models.TINY, "TINY")
        return {"tier": "TINY", "model": gh_models.TINY, "price": 0.001,
                "credits_per_1m_input": p["input"],
            "tokens": tokens,
                "reason": f"prompt {tokens} tok < 500 / simple task", "scenario": "classification/extraction"}
    if any(h in low for h in MULTISTEP_HINTS):
        p = get_model_credits_per_1m(large_model, "LARGE")
        return {"tier": "LARGE", "model": large_model, "price": 0.030,
                "credits_per_1m_input": p["input"],
            "tokens": tokens,
                "reason": "multi-step reasoning needed", "scenario": "agent/code"}
    p = get_model_credits_per_1m(gh_models.MID, "MID")
    return {"tier": "MID", "model": gh_models.MID, "price": 0.012,
            "credits_per_1m_input": p["input"],
            "tokens": tokens,
            "reason": "medium task without multi-step reasoning", "scenario": "dialogue/summary"}
