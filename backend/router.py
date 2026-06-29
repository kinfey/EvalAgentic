"""按需调用 / 模型路由树.

INCOMING REQUEST
  -> Prompt < 500 tokens?  YES -> TINY/SMALL  (分类/抽取, $0.001/K)
                           NO  -> 需要多步推理?  NO  -> MID 70B   (对话/摘要, $0.012/K)
                                                YES -> LARGE GPT-4 (Agent/代码, $0.030/K)
"""
from token_meter import count_tokens
import gh_models

MULTISTEP_HINTS = ("step by step", "多步", "推理", "agent", "代码", "code", "plan", "调试", "debug")
SIMPLE_HINTS = ("分类", "classify", "抽取", "extract", "标签", "label", "情感", "yes/no", "是否")


def route(prompt: str, large_model: str = gh_models.LARGE_GPT) -> dict:
    tokens = count_tokens(prompt)
    low = prompt.lower()
    if tokens < 500 or any(h in low for h in SIMPLE_HINTS):
        return {"tier": "TINY", "model": gh_models.TINY, "price": 0.001,
                "reason": f"prompt {tokens} tok < 500 / 简单任务", "scenario": "分类·抽取"}
    if any(h in low for h in MULTISTEP_HINTS):
        return {"tier": "LARGE", "model": large_model, "price": 0.030,
                "reason": "需要多步推理", "scenario": "Agent·代码"}
    return {"tier": "MID", "model": gh_models.MID, "price": 0.012,
            "reason": "中等任务, 无多步推理", "scenario": "对话·摘要"}
