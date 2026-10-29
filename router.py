"""
Model-affinity router for two llama.cpp (llama-swap / router-mode) backends.
 
Rule per request for model M:
  only backends that HAVE model M are considered (fast first);
  use the first one that is idle, or already serving M with a free slot;
  otherwise wait. If no backend has M -> 404.
 
Model availability:
  - Per-backend "models": explicit list (overrides discovery).
  - Otherwise discovered from each backend's /v1/models at startup and every
    MODELS_REFRESH_SECONDS (default 60), and again when an unknown model is requested.
 
Auth:
  - API_KEY (simplest): one shared key, required from clients AND sent to all backends.
  - Per-backend "api_key": sent to that backend as "Authorization: Bearer <key>".
    If a backend has no api_key, the client's Authorization header is passed through.
  - ROUTER_API_KEY (optional): separate key clients must send to the router.
  - /models, /v1/models and /health are open by default, like llama-server
    (PUBLIC_MODELS=false to require the key there too).
 
Run:
  pip install fastapi uvicorn httpx
  uvicorn router:app --host 0.0.0.0 --port 4000 --workers 1
Point all consumers at http://<router-host>:4000/v1
"""
import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
 
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
 
# Order matters: preferred (fast) server first.
BACKENDS = [
    {"name": "fast", "url": "http://192.168.1.10:8080", "slots": 2},
    {"name": "slow", "url": "http://192.168.1.11:8080", "slots": 2},
]
# Optional override, e.g.
# ROUTER_BACKENDS='[{"name":"fast","url":"http://...","slots":2,"models":["a","b"]}, ...]'
if os.environ.get("ROUTER_BACKENDS"):
    BACKENDS = json.loads(os.environ["ROUTER_BACKENDS"])
 
# API_KEY: one shared key. Clients must send it to the router, and it's also
# sent to every backend that has no api_key of its own.
API_KEY = os.environ.get("API_KEY") or None
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY") or API_KEY
for _b in BACKENDS:
    if not _b.get("api_key"):
        _b["api_key"] = API_KEY
 
REFRESH_SECONDS = int(os.environ.get("MODELS_REFRESH_SECONDS", "60"))
# Like llama-server, /models and /health don't require the key (helps clients
# such as Hermes Agent that probe the model list). Set PUBLIC_MODELS=false to lock it.
PUBLIC_MODELS = os.environ.get("PUBLIC_MODELS", "true").lower() not in ("0", "false", "no")
 
 
KEEP_FIELDS = ("context_length", "architecture", "aliases", "meta",
               "capabilities", "max_model_len", "max_tokens", "max_completion_tokens")
 
 
def model_entry(mid: str, owner: str, src: dict | None = None) -> dict:
    """Clean OpenAI-style model entry (drops server launch args/presets)."""
    src = src or {}
    e = {"id": mid, "object": "model",
         "created": src.get("created", 0), "owned_by": src.get("owned_by", owner)}
    for k in KEEP_FIELDS:
        if k in src:
            e[k] = src[k]
    # llama.cpp reports the runtime context window under meta.n_ctx (not the
    # OpenAI-standard context_length). Preserve it and also expose it as
    # context_length for clients that expect a top-level field.
    meta = src.get("meta") or e.get("meta") or {}
    n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
    if n_ctx is not None and "context_length" not in e:
        e["context_length"] = n_ctx
    if n_ctx is not None and "max_model_len" not in e:
        e["max_model_len"] = n_ctx
    return e
 
 
class Backend:
    def __init__(self, name: str, url: str, slots: int = 1,
                 api_key: str | None = None, models: list[str] | None = None):
        self.name = name
        self.url = url.rstrip("/")
        self.slots = slots              # must match llama-server -np
        self.api_key = api_key          # llama-server --api-key (or llama-swap key)
        self.static = models is not None
        # model id -> OpenAI-style model entry
        self.models: dict[str, dict] = {m: model_entry(m, name) for m in (models or [])}
        self.loaded: str | None = None  # model last sent to this server
        self.active = 0                 # in-flight requests
 
 
backends = [Backend(**b) for b in BACKENDS]
cond = asyncio.Condition()
 
 
# ---------- model discovery ----------
 
def key_headers(key: str | None) -> dict:
    return {"authorization": f"Bearer {key}", "x-api-key": key} if key else {}
 
 
async def discover(b: Backend):
    if b.static:
        return
    url = f"{b.url}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers=key_headers(b.api_key))
        if r.status_code in (401, 403):
            print(f"[router] {b.name}: {url} rejected the API key (HTTP {r.status_code}) "
                  f"- check API_KEY / api_key matches the server's key")
            return
        r.raise_for_status()
        data = [m for m in r.json().get("data", []) if "id" in m]
        b.models = {m["id"]: model_entry(m["id"], b.name, m) for m in data}
        # llama-server router mode reports which model is loaded; sync while idle
        loaded = [m["id"] for m in data
                  if isinstance(m.get("status"), dict) and m["status"].get("value") == "loaded"]
        if loaded and b.active == 0 and b.loaded != loaded[0]:
            print(f"[router] {b.name}: server reports '{loaded[0]}' loaded")
            b.loaded = loaded[0]
    except Exception as e:  # keep the previous list if the backend is unreachable
        print(f"[router] model discovery failed for {b.name} ({url}): "
              f"{type(e).__name__}: {e!r}")
 
 
async def refresh_all():
    await asyncio.gather(*(discover(b) for b in backends))
    for b in backends:
        print(f"[router] {b.name} models: {sorted(b.models)} loaded={b.loaded}")
 
 
async def refresh_loop():
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        await refresh_all()
 
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    for b in backends:
        print(f"[router] backend {b.name}: {b.url} slots={b.slots} "
              f"api_key={'set' if b.api_key else 'NOT SET'}")
    await refresh_all()
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()
 
 
app = FastAPI(lifespan=lifespan)
 
 
# ---------- auth ----------
 
def unauthorized(request: Request) -> JSONResponse | None:
    """Return a 401 response if a router key is set and the client didn't send it."""
    if not ROUTER_API_KEY:
        return None
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if secrets.compare_digest(token, ROUTER_API_KEY):
        return None
    return JSONResponse({"error": "invalid or missing API key"}, status_code=401)
 
 
def backend_headers(b: Backend, request: Request) -> dict:
    headers = {"content-type": "application/json"}
    if b.api_key:
        headers.update(key_headers(b.api_key))
    elif request.headers.get("authorization"):
        headers["authorization"] = request.headers["authorization"]
    return headers
 
 
# ---------- routing ----------
 
def candidates(model: str) -> list[Backend]:
    return [b for b in backends if model in b.models]
 
 
def pick(model: str) -> Backend | None:
    for b in candidates(model):
        if b.active == 0:
            return b  # idle: use it (swapping model if needed)
        if b.loaded == model and b.active < b.slots:
            return b  # same model running and a slot is free: share
    return None       # all candidates busy or full: wait
 
 
async def acquire(model: str) -> Backend | None:
    if not candidates(model):
        await refresh_all()          # maybe a model was just added
        if not candidates(model):
            return None
    async with cond:
        while (b := pick(model)) is None:
            await cond.wait()
        b.active += 1
        b.loaded = model  # set immediately so same-model requests share it during load
        print(f"[router] {model} -> {b.name} (active={b.active})")
        return b
 
 
async def release(b: Backend):
    async with cond:
        b.active -= 1
        cond.notify_all()
 
 
# ---------- endpoints ----------
 
@app.get("/health")
@app.get("/v1/health")
async def health():
    return {"status": "ok"}
 
 
@app.get("/v1/models")
@app.get("/models")
async def models(request: Request):
    if not PUBLIC_MODELS and (err := unauthorized(request)):
        return err
    merged: dict[str, dict] = {}
    for b in backends:
        for mid, entry in b.models.items():
            merged.setdefault(mid, entry)
    return {"object": "list", "data": list(merged.values())}
 
 
@app.get("/status")
async def status(request: Request):
    if (err := unauthorized(request)):
        return err
    return [{"name": b.name, "loaded": b.loaded, "active": b.active,
             "slots": b.slots, "models": sorted(b.models)} for b in backends]
 
 
@app.post("/{path:path}")
async def proxy(path: str, request: Request):
    if (err := unauthorized(request)):
        return err
    # accept both /v1/chat/completions and /chat/completions (like llama-server)
    if path.startswith("v1/"):
        path = path[3:]
    body = await request.body()
    try:
        model = json.loads(body).get("model", "")
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
 
    b = await acquire(model)
    if b is None:
        return JSONResponse(
            {"error": {"message": f"model '{model}' not found on any backend",
                       "type": "invalid_request_error", "code": "model_not_found"}},
            status_code=404)
 
    client = httpx.AsyncClient(timeout=None)  # model loads can take a while
    try:
        req = client.build_request(
            "POST", f"{b.url}/v1/{path}", content=body,
            headers=backend_headers(b, request),
        )
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        await release(b)
        return JSONResponse({"error": f"backend {b.name}: {e}"}, status_code=502)
 
    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:  # runs on completion, error, or client disconnect
            await resp.aclose()
            await client.aclose()
            await release(b)
 
    return StreamingResponse(
        stream(), status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )