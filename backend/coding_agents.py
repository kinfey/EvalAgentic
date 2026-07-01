"""Coding scenario: Agent Framework + GitHub Copilot SDK multi-agent pipeline.

References:
  https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/providers/github_copilot
  https://github.com/microsoft/agent-framework/tree/main/python/samples/03-workflows

Scenario (same deliverable): Taobao-like goods listing site, HTML+JS frontend + Flask backend, deployed with Docker.
Two pipeline variants:
    BEFORE: no compression + all GPT-5.5 (LARGE)         -> deploy to 8081
    AFTER:  compressed injection + on-demand routing      -> deploy to 8082

4 agents: requirements -> coding -> testing -> deployment (Docker).
Each agent is token-metered, runs review/self-healing, and contributes to the final comparison.
"""
import json
import os
import re
import shutil
import time
import asyncio

from agent_framework_github_copilot import GitHubCopilotAgent, GitHubCopilotOptions
from copilot.session import PermissionHandler

from token_meter import (
    compare_cost,
    count_tokens,
    estimate_cost_usd,
)

GEN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generated"))
COMPOSE_FILE = os.path.join(GEN, "docker-compose.yml")
# Artifacts to clean before each run (directories/files)
GEN_ARTIFACTS = ("before", "after", "docker-compose.yml", "deploy.sh")

# Agent roles and on-demand routing (BEFORE all LARGE; AFTER complexity-based)
ROLES = {
    "requirements": {"name": "Requirements Agent", "after_model": "gpt-5.4-mini", "after_tier": "MID",
                     "instr": "You are a requirements analyst. Compress the user request into a minimal JSON spec. Output JSON only with fields: title, entity, fields, api, frontend, backend, deploy."},
    "coding": {"name": "Coding Agent", "after_model": "gpt-5.5", "after_tier": "LARGE",
               "instr": "You are a full-stack engineer. Implement the requirement with Python Flask + HTML/JS. Output concise runnable code."},
    "testing": {"name": "Testing Agent", "after_model": "gpt-5.4-mini", "after_tier": "MID",
                "instr": "You are a test engineer. Provide concise smoke-test points and potential defects based on the API."},
    "deploy": {"name": "Deployment Agent (Docker)", "after_model": "mai-code-1-flash-picker", "after_tier": "TINY",
               "instr": "You are a deployment engineer. Provide concise Dockerfile guidance for a Flask app."},
}
BEFORE_MODEL = "gpt-5.5"


async def _emit(emit, ev: dict):
    if emit is not None:
        await emit(ev)


async def _sh(*args: str) -> tuple[int, str]:
    """Run a command and return (returncode, merged_output) without raising."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode, (out or b"").decode(errors="ignore")
    except FileNotFoundError:
        return 127, "command not found"
    except Exception as ex:  # noqa: BLE001
        return 1, str(ex)


async def _docker_available() -> bool:
    rc, _ = await _sh("docker", "version", "--format", "{{.Server.Version}}")
    return rc == 0


async def cleanup_previous(emit=None):
    """Before Tab C starts: remove previous containers and generated code."""
    # 1) Detect and remove existing containers
    if await _docker_available():
        existing: list[str] = []
        if os.path.exists(COMPOSE_FILE):
            rc, out = await _sh("docker", "compose", "-f", COMPOSE_FILE, "ps", "-aq")
            if rc == 0 and out.strip():
                existing += out.split()
        rc2, out2 = await _sh("docker", "ps", "-aq",
                              "--filter", "name=taobao-before", "--filter", "name=taobao-after")
        if rc2 == 0 and out2.strip():
            existing += out2.split()
        ids = list(dict.fromkeys(existing))
        if ids:
            await _emit(emit, {"type": "cleanup", "phase": "containers",
                               "message": f"Detected {len(ids)} old containers. Removing..."})
            if os.path.exists(COMPOSE_FILE):
                await _sh("docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans", "-v")
            await _sh("docker", "rm", "-f", *ids)
            await _emit(emit, {"type": "cleanup", "phase": "containers", "message": "Old containers removed"})
        else:
            await _emit(emit, {"type": "cleanup", "phase": "containers", "message": "No old containers found"})
    else:
        await _emit(emit, {"type": "cleanup", "phase": "containers",
                           "message": "Docker not detected, skipping container cleanup"})

    # 2) Remove previously generated code
    removed: list[str] = []
    for name in GEN_ARTIFACTS:
        p = os.path.join(GEN, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(name)
        elif os.path.isfile(p):
            os.remove(p)
            removed.append(name)
    if removed:
        await _emit(emit, {"type": "cleanup", "phase": "code",
                   "message": f"Removed old generated artifacts: {', '.join(removed)}"})
    else:
        await _emit(emit, {"type": "cleanup", "phase": "code", "message": "No old generated artifacts found"})


    # Retryable transient errors (session/auth/rate-limit/timeout)
_TRANSIENT = ("authorization error", "session error", "you may need to run /login",
              "timeout", "rate limit", "429", "503", "connection reset", "temporarily")

    # Per-model request timeouts (seconds). Some small models can be slower for
    # medium reasoning, so we allow a longer timeout for multi-round flows.
_SLOW_MODELS = ("mai-code-1-flash-picker",)
_DEFAULT_TIMEOUT = 240
_SLOW_TIMEOUT = 600


def _timeout_for(model: str) -> int:
    return _SLOW_TIMEOUT if (model or "").strip().lower() in _SLOW_MODELS else _DEFAULT_TIMEOUT


async def _run(instructions: str, prompt: str, model: str,
               emit=None, mode: str = "", who: str = "") -> tuple[str, int]:
    last_err = None
    timeout = _timeout_for(model)
    for attempt in range(4):
        try:
            agent: GitHubCopilotAgent = GitHubCopilotAgent(
                instructions=instructions,
                default_options=GitHubCopilotOptions(
                    model=model, timeout=timeout, on_permission_request=PermissionHandler.approve_all),
            )
            t0 = time.time()
            async with agent:
                resp = await agent.run(prompt)
            text = getattr(resp, "text", None) or str(resp)
            return text.strip(), int((time.time() - t0) * 1000)
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            msg = str(ex)
            if attempt < 3 and any(t in msg.lower() for t in _TRANSIENT):
                wait = 3 * (attempt + 1)
                await _emit(emit, {"type": "step", "mode": mode, "agent": who, "phase": "retry",
                                   "attempt": attempt + 1, "wait": wait, "error": msg[:80]})
                await asyncio.sleep(wait)
                continue
            raise
    raise last_err


def _build_prompt(role: str, mode: str, ctx: dict) -> str:
    if role == "requirements":
        return f"Requirement: {ctx['req']}\nCompress it into a JSON spec."
    if role == "coding":
        spec = ctx["spec"]
        # Dynamic injection: AFTER injects compressed JSON spec; BEFORE uses full natural language.
        return (f"Implement a Taobao-like goods listing website on port {ctx['port']}.\nSpec: {spec}\n"
                f"Provide key code for Flask app.py and templates/index.html.")
    if role == "testing":
        return f"For the goods listing site (GET /api/goods returns an array), provide smoke-test points and likely defects. Port: {ctx['port']}."
    if role == "deploy":
        return f"Provide concise Dockerfile guidance to deploy this Flask app with listening port {ctx['port']}."
    return ctx.get("req", "")


async def run_role(role_key: str, mode: str, ctx: dict, emit=None) -> dict:
    role = ROLES[role_key]
    model = BEFORE_MODEL if mode == "before" else role["after_model"]
    tier = "LARGE" if mode == "before" else role["after_tier"]
    instr = role["instr"]
    prompt = _build_prompt(role_key, mode, ctx)
    await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"],
                       "phase": "start", "model": model, "tier": tier})

    text, lat = await _run(instr, prompt, model, emit, mode, role["name"])
    await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "generated",
                       "tokens": count_tokens(prompt) + count_tokens(text), "latency_ms": lat})

    # Review + self-healing
    await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "reviewing"})
    review, _ = await _run(
        "You are a strict reviewer. Return only OK if the output is acceptable; otherwise report one concise issue.",
        f"Review whether this output from {role['name']} meets the requirement:\n{text[:1200]}", model, emit, mode, role["name"])
    healed = False
    note = review[:60]
    if "OK" not in review[:6].upper():
        await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "healing", "review": note})
        fixed, _ = await _run(instr, f"Fix and regenerate based on this review:\nReview: {review[:200]}\nOriginal output:\n{text[:2000]}", model, emit, mode, role["name"])
        text, healed = fixed.strip(), True

    pt, ct = count_tokens(prompt), count_tokens(text)
    result = {
        "role": role["name"], "model": model, "tier": tier,
        "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
        "cost_usd": round(estimate_cost_usd(pt, ct, model, tier), 6),
        "latency_ms": lat, "healed": healed, "review": note, "text": text,
    }
    await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "done",
                       "healed": healed, "review": note, "total_tokens": pt + ct,
                       "cost_usd": result["cost_usd"], "model": model, "tier": tier})
    return result


def _parse_spec(text: str) -> str:
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.dumps(json.loads(text[s:e + 1]), ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
    return text[:200]


# ---- Deployable project templates (parameterized by port) ----
APP_PY = '''import os
from flask import Flask, jsonify, render_template

app = Flask(__name__)
GOODS = [
    {"id": 1, "name": "Wireless Bluetooth Earbuds", "price": 199, "img": "🎧", "sales": 1280},
    {"id": 2, "name": "87-Key Mechanical Keyboard", "price": 359, "img": "⌨️", "sales": 860},
    {"id": 3, "name": "27-inch 4K Monitor", "price": 1299, "img": "🖥️", "sales": 420},
    {"id": 4, "name": "Ergonomic Chair", "price": 899, "img": "🪑", "sales": 310},
    {"id": 5, "name": "USB-C Dock", "price": 159, "img": "🔌", "sales": 2100},
    {"id": 6, "name": "20000mAh Power Bank", "price": 129, "img": "🔋", "sales": 3300},
]

@app.route("/")
def index():
    return render_template("index.html", port=os.environ.get("PORT", "__PORT__"))

@app.route("/api/goods")
def goods():
    return jsonify(GOODS)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "__PORT__")))
'''

INDEX_HTML = '''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Goods List</title><style>
body{margin:0;font-family:-apple-system,Segoe UI,sans-serif;background:#f5f5f5}
header{background:linear-gradient(90deg,#ff5000,#ff8a00);color:#fff;padding:16px 24px;font-size:20px;font-weight:700}
.tag{font-size:12px;opacity:.85;font-weight:400;margin-left:8px}
.grid{max-width:1100px;margin:20px auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.emoji{font-size:46px}.name{margin:8px 0 4px;font-size:14px}.price{color:#ff5000;font-weight:700}.sales{color:#999;font-size:12px}
button{margin-top:8px;background:#ff5000;color:#fff;border:0;padding:7px 14px;border-radius:20px;cursor:pointer}
</style></head><body>
<header>StoreLite · Goods List<span class="tag">Flask + HTML/JS · Port {{ port }}</span></header>
<div class="grid" id="grid"></div>
<script>
fetch('/api/goods').then(r=>r.json()).then(gs=>{
 document.getElementById('grid').innerHTML=gs.map(g=>`
  <div class="card"><div class="emoji">${g.img}</div>
    <div class="name">${g.name}</div><div class="price">$${g.price}</div>
    <div class="sales">Sold ${g.sales}</div><button>Add to Cart</button></div>`).join('');
});
</script></body></html>
'''


def _dockerfile(port: int) -> str:
    return (f"FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
            f"RUN pip install --no-cache-dir flask\nENV PORT={port}\nEXPOSE {port}\n"
            f'CMD ["python", "app.py"]\n')


def write_project(mode: str, port: int):
    d = os.path.join(GEN, mode)
    os.makedirs(os.path.join(d, "templates"), exist_ok=True)
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write(APP_PY.replace("__PORT__", str(port)))
    with open(os.path.join(d, "templates", "index.html"), "w") as f:
        f.write(INDEX_HTML)
    with open(os.path.join(d, "requirements.txt"), "w") as f:
        f.write("flask>=3.0\n")
    with open(os.path.join(d, "Dockerfile"), "w") as f:
        f.write(_dockerfile(port))


def write_deploy_assets():
    os.makedirs(GEN, exist_ok=True)
    # Containers still listen on 8081/8082; host maps to 18081/18082 to avoid conflicts.
    compose = (
        "services:\n"
        "  taobao-before:\n    build: ./before\n    ports:\n      - \"18081:8081\"\n"
        "  taobao-after:\n    build: ./after\n    ports:\n      - \"18082:8082\"\n"
    )
    with open(os.path.join(GEN, "docker-compose.yml"), "w") as f:
        f.write(compose)
    sh = ("#!/usr/bin/env bash\nset -e\ncd \"$(dirname \"$0\")\"\n"
          "docker compose up --build -d\n"
          "echo 'BEFORE -> http://localhost:18081  AFTER -> http://localhost:18082'\n")
    p = os.path.join(GEN, "deploy.sh")
    with open(p, "w") as f:
        f.write(sh)
    os.chmod(p, 0o755)


async def run_pipeline(mode: str, user_req: str, port: int, emit=None) -> dict:
    """Run a 4-agent pipeline and return per-agent token and self-healing metrics."""
    await _emit(emit, {"type": "mode", "mode": mode, "port": port, "phase": "start"})
    req = await run_role("requirements", mode, {"req": user_req}, emit)
    spec = _parse_spec(req["text"])
    await _emit(emit, {"type": "spec", "mode": mode, "spec": spec})
    inject = spec if mode == "after" else user_req  # Dynamic injection: AFTER uses compressed JSON.
    code = await run_role("coding", mode, {"spec": inject, "port": port}, emit)
    test = await run_role("testing", mode, {"port": port}, emit)
    deploy = await run_role("deploy", mode, {"port": port}, emit)

    write_project(mode, port)  # Ensure deployable deliverables are always produced.
    agents = [req, code, test, deploy]
    return {
        "mode": mode, "port": port, "spec": spec,
        "agents": [{k: v for k, v in a.items() if k != "text"} for a in agents],
        "total_tokens": sum(a["total_tokens"] for a in agents),
        "total_cost": round(sum(a["cost_usd"] for a in agents), 6),
        "healed_count": sum(1 for a in agents if a["healed"]),
    }


async def run_coding_eval(user_req: str, emit=None) -> dict:
    await cleanup_previous(emit)
    before = await run_pipeline("before", user_req, 8081, emit)
    after = await run_pipeline("after", user_req, 8082, emit)
    write_deploy_assets()
    saved_tok = before["total_tokens"] - after["total_tokens"]
    saved_cost = round(before["total_cost"] - after["total_cost"], 6)
    return {
        "before": before, "after": after,
        "saved_tokens": saved_tok,
        "saved_pct": round(saved_tok / before["total_tokens"] * 100, 1) if before["total_tokens"] else 0,
        "saved_cost": saved_cost,
        "saved_cost_pct": round(saved_cost / before["total_cost"] * 100, 1) if before["total_cost"] else 0,
        "cost": compare_cost(before["total_cost"], after["total_cost"]),
        "deploy": {"before_url": "http://localhost:18081", "after_url": "http://localhost:18082",
                   "cmd": "cd generated && ./deploy.sh", "compose": "generated/docker-compose.yml"},
    }
