"""编码场景: Agent Framework + GitHub Copilot SDK 多 Agent 流水线.

参考:
  https://github.com/microsoft/agent-framework/tree/main/python/samples/02-agents/providers/github_copilot
  https://github.com/microsoft/agent-framework/tree/main/python/samples/03-workflows

场景(相同交付物): 类淘宝网站(仅货物列表), 前端 HTML+JS + 后端 Flask, 部署 Docker.
两个流水线对比:
  BEFORE 处理前: 不压缩 + 全部 GPT-5.5 (LARGE)         -> 项目部署到 8081
  AFTER  处理后: 压缩注入 + 按需路由 (MID/LARGE/TINY)   -> 项目部署到 8082

4 个 Agent: 需求分析 -> 编程 -> 测试 -> 部署(Docker)。
每个 Agent 都计 token、都做 review 自我修复, 最后做整体对比。
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
# 每次运行前需清理的生成物 (目录或文件)
GEN_ARTIFACTS = ("before", "after", "docker-compose.yml", "deploy.sh")

# 每个 Agent 的角色与按需路由 (BEFORE 全 LARGE; AFTER 按复杂度分配)
ROLES = {
    "requirements": {"name": "需求分析 Agent", "after_model": "gpt-5.4-mini", "after_tier": "MID",
                     "instr": "你是需求分析师。把口语化需求压缩成最小 JSON 规格, 只输出 JSON, 字段: title, entity, fields, api, frontend, backend, deploy。"},
    "coding": {"name": "编程 Agent", "after_model": "gpt-5.5", "after_tier": "LARGE",
               "instr": "你是全栈工程师, 用 Python Flask + HTML/JS 实现需求。输出简洁可运行代码。"},
    "testing": {"name": "测试 Agent", "after_model": "gpt-5.4-mini", "after_tier": "MID",
                "instr": "你是测试工程师。基于接口给出最小冒烟测试要点与潜在缺陷, 简短输出。"},
    "deploy": {"name": "部署 Agent(Docker)", "after_model": "gpt-5-mini", "after_tier": "TINY",
               "instr": "你是部署工程师。为 Flask 应用产出 Dockerfile 要点, 简短输出。"},
}
BEFORE_MODEL = "gpt-5.5"


async def _emit(emit, ev: dict):
    if emit is not None:
        await emit(ev)


async def _sh(*args: str) -> tuple[int, str]:
    """运行命令, 返回 (returncode, 合并输出)。不抛异常。"""
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
    """Tab C 启动前: 若项目容器之前已建立则先删除, 再删除之前生成的代码。"""
    # 1) 检查并删除之前建立的容器
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
                               "message": f"检测到 {len(ids)} 个旧容器, 正在删除…"})
            if os.path.exists(COMPOSE_FILE):
                await _sh("docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans", "-v")
            await _sh("docker", "rm", "-f", *ids)
            await _emit(emit, {"type": "cleanup", "phase": "containers", "message": "旧容器已删除"})
        else:
            await _emit(emit, {"type": "cleanup", "phase": "containers", "message": "未检测到旧容器"})
    else:
        await _emit(emit, {"type": "cleanup", "phase": "containers",
                           "message": "未检测到 Docker, 跳过容器删除"})

    # 2) 删除之前生成的代码
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
                           "message": f"已清理旧生成代码: {', '.join(removed)}"})
    else:
        await _emit(emit, {"type": "cleanup", "phase": "code", "message": "无旧生成代码"})


# 可重试的瞬态错误 (会话/授权/限流/超时)
_TRANSIENT = ("authorization error", "session error", "you may need to run /login",
              "timeout", "rate limit", "429", "503", "connection reset", "temporarily")


async def _run(instructions: str, prompt: str, model: str,
               emit=None, mode: str = "", who: str = "") -> tuple[str, int]:
    last_err = None
    for attempt in range(4):
        try:
            agent: GitHubCopilotAgent = GitHubCopilotAgent(
                instructions=instructions,
                default_options=GitHubCopilotOptions(
                    model=model, timeout=240, on_permission_request=PermissionHandler.approve_all),
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
        return f"需求: {ctx['req']}\n压缩为 JSON 规格。"
    if role == "coding":
        spec = ctx["spec"]
        # 动态注入: AFTER 注入压缩后的 JSON 规格; BEFORE 注入完整自然语言需求
        return (f"按规格实现 类淘宝货物列表 网站, 端口 {ctx['port']}。\n规格: {spec}\n"
                f"给出 Flask app.py 与 templates/index.html 关键代码。")
    if role == "testing":
        return f"对货物列表网站(GET /api/goods 返回商品数组)给出冒烟测试要点与缺陷, 端口 {ctx['port']}。"
    if role == "deploy":
        return f"为该 Flask 应用给出部署到 Docker 的 Dockerfile 要点, 监听端口 {ctx['port']}。"
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

    # review + 自我修复
    await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "reviewing"})
    review, _ = await _run(
        "你是严格审查员。产物达标只回 OK; 否则用一句话(30字内)指出问题。",
        f"审查{role['name']}产物是否达标:\n{text[:1200]}", model, emit, mode, role["name"])
    healed = False
    note = review[:60]
    if "OK" not in review[:6].upper() and any(k in review for k in ("问题", "缺", "修复", "建议", "应", "未")):
        await _emit(emit, {"type": "step", "mode": mode, "agent": role["name"], "phase": "healing", "review": note})
        fixed, _ = await _run(instr, f"根据审查意见修复并重新输出:\n意见: {review[:200]}\n原产物:\n{text[:2000]}", model, emit, mode, role["name"])
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


# ---- 保证可部署的项目模板 (按端口参数化) ----
APP_PY = '''import os
from flask import Flask, jsonify, render_template

app = Flask(__name__)
GOODS = [
    {"id": 1, "name": "无线蓝牙耳机", "price": 199, "img": "🎧", "sales": 1280},
    {"id": 2, "name": "机械键盘 87 键", "price": 359, "img": "⌨️", "sales": 860},
    {"id": 3, "name": "4K 显示器 27寸", "price": 1299, "img": "🖥️", "sales": 420},
    {"id": 4, "name": "人体工学椅", "price": 899, "img": "🪑", "sales": 310},
    {"id": 5, "name": "USB-C 扩展坞", "price": 159, "img": "🔌", "sales": 2100},
    {"id": 6, "name": "便携充电宝 20000mAh", "price": 129, "img": "🔋", "sales": 3300},
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
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>淘 · 货物列表</title><style>
body{margin:0;font-family:-apple-system,Segoe UI,sans-serif;background:#f5f5f5}
header{background:linear-gradient(90deg,#ff5000,#ff8a00);color:#fff;padding:16px 24px;font-size:20px;font-weight:700}
.tag{font-size:12px;opacity:.85;font-weight:400;margin-left:8px}
.grid{max-width:1100px;margin:20px auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.emoji{font-size:46px}.name{margin:8px 0 4px;font-size:14px}.price{color:#ff5000;font-weight:700}.sales{color:#999;font-size:12px}
button{margin-top:8px;background:#ff5000;color:#fff;border:0;padding:7px 14px;border-radius:20px;cursor:pointer}
</style></head><body>
<header>淘宝Lite · 货物列表<span class="tag">Flask + HTML/JS · 端口 {{ port }}</span></header>
<div class="grid" id="grid"></div>
<script>
fetch('/api/goods').then(r=>r.json()).then(gs=>{
 document.getElementById('grid').innerHTML=gs.map(g=>`
  <div class="card"><div class="emoji">${g.img}</div>
   <div class="name">${g.name}</div><div class="price">¥${g.price}</div>
   <div class="sales">已售 ${g.sales}</div><button>加入购物车</button></div>`).join('');
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
    # 容器内仍监听 8081/8082; 宿主机映射到 18081/18082 (8081/8082 已被其它容器占用)
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
    """运行一条 4-Agent 流水线, 返回每个 Agent 的 token 与自愈信息。"""
    await _emit(emit, {"type": "mode", "mode": mode, "port": port, "phase": "start"})
    req = await run_role("requirements", mode, {"req": user_req}, emit)
    spec = _parse_spec(req["text"])
    await _emit(emit, {"type": "spec", "mode": mode, "spec": spec})
    inject = spec if mode == "after" else user_req  # 动态注入: AFTER 用压缩 JSON
    code = await run_role("coding", mode, {"spec": inject, "port": port}, emit)
    test = await run_role("testing", mode, {"port": port}, emit)
    deploy = await run_role("deploy", mode, {"port": port}, emit)

    write_project(mode, port)  # 保证可部署交付物
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
