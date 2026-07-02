"""Tab C — Prompt Caching + Deduplication (token-efficiency demo).

Faithful to the VS Code blog "Improving token efficiency for GitHub Copilot"
(2026-06-17). It models the two repeating costs of an agentic coding session and
the two ideas that reduce them:

  * Prompt prefix caching. A large share of every request repeats across turns:
    system instructions, repo context, and core tool metadata. This stable
    prefix is billed at the cached-input rate (up to 10x cheaper) on every turn
    after the first instead of full price each time.

  * Deduplication via tool search. Historically every tool's full JSON schema
    was loaded on every request. Here MAI-Code-1-Flash acts as the dedup /
    tool-search engine: for each turn it selects only the tools that turn needs.
    Unused tools stay deferred as lightweight name+description metadata, removing
    redundant tool-definition tokens from the context window.

The comparison runs for two target models, GPT-5.5 and Claude Opus 4.8, showing
the token and credit savings of BEFORE (no caching, all tool defs every turn) vs
AFTER (prefix cached + tool defs deduplicated). All model traffic goes through
the GitHub Copilot SDK (gh_models); MAI-Code-1-Flash is the primary model that
drives the caching/deduplication step.
"""
import json

import gh_models
from token_meter import (
    ANTHROPIC_CACHE_WRITE_MULTIPLIER,
    CACHE_READ_MULTIPLIER,
    cost_summary,
    count_tokens,
    credits_to_usd,
    get_model_credits_per_1m,
    is_anthropic_model,
    usd_to_ai_credits,
)

# Target models compared side by side.
TARGET_MODELS = [
    {"model": gh_models.LARGE_GPT, "label": "GPT-5.5", "provider": "openai"},
    {"model": gh_models.LARGE_OPUS, "label": "Claude Opus 4.8", "provider": "anthropic"},
]

# ---- Stable prompt prefix (repeats every turn -> ideal caching target) -------
SYSTEM_PROMPT = (
    "You are GitHub Copilot's coding agent operating inside VS Code. Follow the "
    "workspace conventions, make minimal surgical edits, run the existing tests, "
    "and never invent files that do not exist. Prefer the smallest change that "
    "fully satisfies the request and always explain trade-offs succinctly."
)

REPO_CONTEXT = (
    "Repository: taobao-lite (Flask + HTML/JS)\n"
    "Files:\n"
    "  backend/server.py        # Flask app, GOODS seed, /api/goods, /api/categories\n"
    "  backend/app.py           # alt server, pagination + filtering helpers\n"
    "  frontend/index.html      # goods grid, search box, category filter\n"
    "  tests/test_api.py        # pytest smoke tests for the goods API\n"
    "Key handler:\n"
    "  @app.get('/api/goods') -> list_goods(): reads q, category, sort, page, per_page\n"
    "  _filtered_goods(): keyword match over name/description/category/shop\n"
)

# ---- Tool catalog. Full defs are heavy; metadata (name+desc) is lightweight. --
TOOL_CATALOG = [
    {
        "name": "read_file",
        "description": "Read a file's contents by path with an optional line range.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or workspace-relative file path."},
                "start_line": {"type": "integer", "description": "1-based first line to read."},
                "end_line": {"type": "integer", "description": "1-based last line to read."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Apply a precise string replacement inside an existing file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old_str": {"type": "string", "description": "Exact text to replace."},
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "run_terminal",
        "description": "Run a shell command in the workspace and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to execute."},
                "cwd": {"type": "string", "description": "Working directory."},
                "timeout_s": {"type": "integer", "description": "Kill after N seconds."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "search_workspace",
        "description": "Full-text/ripgrep search across the workspace for a pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex or literal to search."},
                "glob": {"type": "string", "description": "Glob filter, e.g. **/*.py."},
                "max_results": {"type": "integer", "description": "Cap the number of hits."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_pytest",
        "description": "Run pytest for a path or node id and return the pass/fail report.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "File, directory, or test node id."},
                "keyword": {"type": "string", "description": "-k expression to filter tests."},
                "verbose": {"type": "boolean", "description": "Enable -v output."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "http_request",
        "description": "Issue an HTTP request against the running dev server for smoke checks.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "GET/POST/PUT/DELETE."},
                "url": {"type": "string", "description": "Target URL."},
                "json_body": {"type": "object", "description": "Optional JSON payload."},
            },
            "required": ["method", "url"],
        },
    },
    {
        "name": "docker_build",
        "description": "Build a Docker image from a Dockerfile and tag it.",
        "parameters": {
            "type": "object",
            "properties": {
                "dockerfile": {"type": "string", "description": "Path to the Dockerfile."},
                "tag": {"type": "string", "description": "Image tag."},
                "build_args": {"type": "object", "description": "Optional build arguments."},
            },
            "required": ["dockerfile", "tag"],
        },
    },
    {
        "name": "git_diff",
        "description": "Show the current staged/unstaged diff for review before committing.",
        "parameters": {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Only staged changes when true."},
                "path": {"type": "string", "description": "Limit the diff to a path."},
            },
            "required": [],
        },
    },
]

# ---- Multi-turn agentic session over the same repo ---------------------------
TURNS = [
    {
        "user": "Add a keyword search endpoint to the goods API.",
        "assistant": "Added GET /api/goods/search that reads ?q= and reuses _filtered_goods() to match name/description/category/shop, returning JSON.",
        "expected_tools": ["read_file", "edit_file", "search_workspace"],
    },
    {
        "user": "Now add page and per_page pagination to that search endpoint.",
        "assistant": "Extended the search handler to parse page/per_page with sane bounds, slice the filtered list, and return a pagination block (page, per_page, total, pages, has_next).",
        "expected_tools": ["read_file", "edit_file"],
    },
    {
        "user": "Write a pytest smoke test for the search endpoint.",
        "assistant": "Created tests/test_api.py::test_search covering a keyword hit, an empty result, and a paginated request; asserts status codes and pagination fields.",
        "expected_tools": ["read_file", "edit_file", "run_pytest"],
    },
    {
        "user": "The test fails on an empty keyword. Fix the handler and re-run the tests.",
        "assistant": "Guarded the empty-keyword branch to return the full page instead of raising, refactored the slice logic, and re-ran pytest -k search: all green.",
        "expected_tools": ["read_file", "edit_file", "run_pytest", "run_terminal"],
    },
    {
        "user": "Smoke-test the running server, then build the Docker image.",
        "assistant": "Issued GET /api/goods/search?q=cotton&page=1 (200 OK, 3 items), then built the image taobao-lite:latest from backend/Dockerfile.",
        "expected_tools": ["http_request", "docker_build", "git_diff"],
    },
]


def _tool_full_def(tool: dict) -> str:
    return json.dumps(
        {"name": tool["name"], "description": tool["description"], "parameters": tool["parameters"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tool_meta(tool: dict) -> str:
    return json.dumps(
        {"name": tool["name"], "description": tool["description"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _catalog_meta_text() -> str:
    return "Deferred tool catalog (name + description only):\n" + "\n".join(
        _tool_meta(t) for t in TOOL_CATALOG
    )


async def _select_tools(user: str, expected: list[str]) -> tuple[list[str], int]:
    """MAI-Code-1-Flash is the dedup / tool-search engine: pick only the tools
    this turn needs. Returns (selected_names, latency_ms). Falls back to the
    turn's expected set if the model output cannot be parsed.
    """
    catalog = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOL_CATALOG)
    prompt = (
        "You are a tool-search router. Given the developer request and the tool "
        "catalog, return ONLY a JSON array of the tool names strictly required "
        "for this single step. Do not include tools that are not needed.\n\n"
        f"Request: {user}\n\nCatalog:\n{catalog}\n\nJSON array:"
    )
    valid = {t["name"] for t in TOOL_CATALOG}
    try:
        raw, latency = await gh_models.ask(prompt, gh_models.TINY)
        s, e = raw.find("["), raw.rfind("]")
        names = json.loads(raw[s : e + 1]) if s >= 0 and e > s else []
        selected = [n for n in names if n in valid]
        if not selected:
            selected = [n for n in expected if n in valid]
        return selected, latency
    except Exception:
        return [n for n in expected if n in valid], 0


def _model_costs(model: str, prefix_tok: int, turns_billing: list[dict]) -> dict:
    """Compute BEFORE/AFTER credits for one target model from per-turn token
    breakdowns. `turns_billing[i]` has keys: before_tokens, fresh_tokens.
    Cost is input-side (prefix + tools + history + user), which is where caching
    and dedup act.
    """
    rate = get_model_credits_per_1m(model, "LARGE")["input"]  # credits per 1M input tokens
    write_mult = ANTHROPIC_CACHE_WRITE_MULTIPLIER if is_anthropic_model(model) else 1.0

    before_credits = 0.0
    after_credits = 0.0
    rows = []
    for i, tb in enumerate(turns_billing):
        before_tok = tb["before_tokens"]
        fresh_tok = tb["fresh_tokens"]
        before_c = before_tok / 1_000_000 * rate

        if i == 0:
            # First turn writes the cache: prefix billed at (write premium) full rate.
            after_prefix_billable = prefix_tok * write_mult
            cache_state = "write"
        else:
            # Later turns read the cached prefix at the cheaper rate.
            after_prefix_billable = prefix_tok * CACHE_READ_MULTIPLIER
            cache_state = "read"
        after_billable_tok = after_prefix_billable + fresh_tok
        after_c = after_billable_tok / 1_000_000 * rate

        before_credits += before_c
        after_credits += after_c
        rows.append(
            {
                "turn": i + 1,
                "cache_state": cache_state,
                "before_tokens": before_tok,
                "after_billable_tokens": round(after_billable_tok),
                "before_credits": round(before_c, 4),
                "after_credits": round(after_c, 4),
            }
        )

    saved_credits = before_credits - after_credits
    return {
        "input_rate_credits_per_1m": rate,
        "cache_write_multiplier": write_mult,
        "before": {
            "credits": round(before_credits, 4),
            "usd": round(credits_to_usd(before_credits), 6),
            **cost_summary(credits_to_usd(before_credits)),
        },
        "after": {
            "credits": round(after_credits, 4),
            "usd": round(credits_to_usd(after_credits), 6),
        },
        "saved": {
            "credits": round(saved_credits, 4),
            "usd": round(credits_to_usd(saved_credits), 6),
            "pct": round(saved_credits / before_credits * 100, 1) if before_credits else 0.0,
        },
        "rows": rows,
    }


async def eval_caching() -> dict:
    """Run the caching + deduplication comparison for GPT-5.5 and Claude Opus 4.8."""
    # Stable prefix: system + repo context + lightweight deferred tool metadata.
    meta_text = _catalog_meta_text()
    prefix_text = f"{SYSTEM_PROMPT}\n\n{REPO_CONTEXT}\n\n{meta_text}"
    prefix_tok = count_tokens(prefix_text)

    # Full tool-definition token cost if ALL tools are loaded (the BEFORE world).
    all_tools_full_tok = sum(count_tokens(_tool_full_def(t)) for t in TOOL_CATALOG)
    catalog_meta_tok = count_tokens(meta_text)

    # Walk the session turn by turn. MAI-Code-1-Flash selects tools each turn.
    history = ""
    turns_billing = []
    turn_details = []
    dedup_before_tools_tok = 0
    dedup_after_tools_tok = 0
    mai_latency_total = 0

    for t in TURNS:
        user_tok = count_tokens(t["user"])
        history_tok = count_tokens(history)

        selected, mai_lat = await _select_tools(t["user"], t["expected_tools"])
        mai_latency_total += mai_lat
        loaded_full_tok = sum(
            count_tokens(_tool_full_def(x)) for x in TOOL_CATALOG if x["name"] in selected
        )

        # BEFORE: prefix core + EVERY tool's full def + history + user, every turn.
        before_tokens = prefix_tok + all_tools_full_tok + history_tok + user_tok
        # AFTER: prefix (incl. deferred tool metadata) is cached; only the
        # selected tools' full schemas are loaded fresh at the end of context.
        fresh_tokens = loaded_full_tok + history_tok + user_tok

        turns_billing.append({"before_tokens": before_tokens, "fresh_tokens": fresh_tokens})
        dedup_before_tools_tok += all_tools_full_tok
        dedup_after_tools_tok += loaded_full_tok

        turn_details.append(
            {
                "turn": len(turn_details) + 1,
                "user": t["user"],
                "selected_tools": selected,
                "loaded_tool_tokens": loaded_full_tok,
                "deferred_tool_tokens": all_tools_full_tok - loaded_full_tok,
                "history_tokens": history_tok,
            }
        )
        history += f"\nUser: {t['user']}\nAssistant: {t['assistant']}\n"

    models = []
    for spec in TARGET_MODELS:
        costs = _model_costs(spec["model"], prefix_tok, turns_billing)
        # One live call through the SDK on the final turn to prove end-to-end.
        final = TURNS[-1]
        loaded = turn_details[-1]["selected_tools"]
        live_prompt = (
            f"{SYSTEM_PROMPT}\n\nRepo context:\n{REPO_CONTEXT}\n\n"
            f"Loaded tools: {', '.join(loaded)}\n\nTask: {final['user']}\n"
            "Answer in two concise sentences."
        )
        try:
            answer, latency = await gh_models.ask(live_prompt, spec["model"])
        except Exception as ex:  # noqa: BLE001
            answer, latency = f"(live call unavailable: {ex})", 0
        models.append(
            {
                **spec,
                **costs,
                "answer": answer,
                "latency_ms": latency,
            }
        )

    dedup_saved_tok = dedup_before_tools_tok - dedup_after_tools_tok
    total_before_credits = sum(m["before"]["credits"] for m in models)
    total_after_credits = sum(m["after"]["credits"] for m in models)

    return {
        "session": {
            "turns": len(TURNS),
            "prefix_tokens": prefix_tok,
            "catalog_meta_tokens": catalog_meta_tok,
            "all_tools_full_tokens": all_tools_full_tok,
            "tool_count": len(TOOL_CATALOG),
            "turn_details": turn_details,
        },
        "dedup": {
            "engine": gh_models.TINY,
            "engine_label": "MAI-Code-1-Flash",
            "latency_ms": mai_latency_total,
            "before_tool_tokens": dedup_before_tools_tok,
            "after_tool_tokens": dedup_after_tools_tok,
            "saved_tokens": dedup_saved_tok,
            "saved_pct": round(dedup_saved_tok / dedup_before_tools_tok * 100, 1)
            if dedup_before_tools_tok
            else 0.0,
        },
        "models": models,
        "totals": {
            "before_credits": round(total_before_credits, 4),
            "after_credits": round(total_after_credits, 4),
            "saved_credits": round(total_before_credits - total_after_credits, 4),
            "saved_usd": round(credits_to_usd(total_before_credits - total_after_credits), 6),
        },
    }
