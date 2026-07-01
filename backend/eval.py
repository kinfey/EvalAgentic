"""评估编排: 同场景下做 处理前/处理后 对比.

A. 压缩对比: raw prompt 直送 LARGE  vs  压缩为 JSON 注入后送 LARGE
B. 按需调用: 全部走 LARGE       vs  路由树按需分配 (TINY/MID/LARGE)
"""
from token_meter import (
    CounterCore,
    compare_cost,
    count_tokens,
    estimate_input_cost_usd,
    token_meter,
)
import gh_models
import router
import compressor


async def eval_compression(text: str, task: str, large_model: str):
    """A: same scenario, before(raw->LARGE) vs after(compress->inject->LARGE)."""
    core = CounterCore()

    before = token_meter(core, "BEFORE 原始注入", large_model, "LARGE")(gh_models.ask)
    before_prompt = f"{task}\n背景:\n{text}"
    b_text, b_lat = await before(before_prompt, large_model)

    comp = await compressor.compress(text)
    after = token_meter(core, "AFTER 压缩注入", large_model, "LARGE")(gh_models.ask)
    after_prompt = f"{task}\n背景(JSON):{comp['compact']}"
    a_text, a_lat = await after(after_prompt, large_model)

    snap = core.snapshot()
    bt = count_tokens(before_prompt)
    at = count_tokens(after_prompt)
    # 成本节省按"输入 token 差"计 (压缩只作用于输入; 输出长度非确定, 不计入对比)
    saved_cost = round(
        estimate_input_cost_usd(max(0, bt - at), large_model, "LARGE"),
        6,
    )
    before_cost = snap["events"][0]["cost_usd"]
    after_cost = snap["events"][1]["cost_usd"]
    return {
        "before": {"prompt_tokens": bt, "latency_ms": b_lat, "answer": b_text, "event": snap["events"][0]},
        "after": {"prompt_tokens": at, "latency_ms": a_lat, "answer": a_text, "compact": comp["compact"],
                  "cached": comp["cached"], "event": snap["events"][1]},
        "saved_tokens": bt - at,
        "saved_pct": round((bt - at) / bt * 100, 1) if bt else 0,
        "saved_cost": saved_cost,
        "cost": compare_cost(before_cost, after_cost),
    }


async def eval_routing(prompts: list[str], large_model: str):
    """B: same prompts, before(全 LARGE) vs after(按需路由)."""
    before_core, after_core = CounterCore(), CounterCore()
    rows = []
    for p in prompts:
        bf = token_meter(before_core, "ALL-LARGE", large_model, "LARGE")(gh_models.ask)
        bt, _ = await bf(p, large_model)
        r = router.route(p, large_model)
        af = token_meter(after_core, r["tier"], r["model"], r["tier"])(gh_models.ask)
        at, _ = await af(p, r["model"])
        rows.append({"prompt": p[:60], "route": r,
                     "tokens": r.get("tokens", 0),
                     "before_tier": "LARGE", "after_tier": r["tier"]})
    return {
        "rows": rows,
        "before": before_core.snapshot(),
        "after": after_core.snapshot(),
        "saved_cost": round(before_core.total_cost - after_core.total_cost, 6),
        "cost": compare_cost(before_core.total_cost, after_core.total_cost),
    }
