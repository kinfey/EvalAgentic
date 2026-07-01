"""Evaluation orchestration: before/after comparisons on the same scenario.

A. Compression comparison: raw prompt to LARGE vs compact JSON injection to LARGE.
B. On-demand routing: all LARGE vs routing tree assignment (TINY/MID/LARGE).
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

    before = token_meter(core, "BEFORE Raw Injection", large_model, "LARGE")(gh_models.ask)
    before_prompt = f"{task}\nContext:\n{text}"
    b_text, b_lat = await before(before_prompt, large_model)

    comp = await compressor.compress(text)
    after = token_meter(core, "AFTER Compressed Injection", large_model, "LARGE")(gh_models.ask)
    after_prompt = f"{task}\nContext(JSON):{comp['compact']}"
    a_text, a_lat = await after(after_prompt, large_model)

    snap = core.snapshot()
    bt = count_tokens(before_prompt)
    at = count_tokens(after_prompt)
    # Savings are computed from input token delta only.
    # Compression directly affects input size; output length is variable and excluded.
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
    """B: same prompts, before (all LARGE) vs after (on-demand routing)."""
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
