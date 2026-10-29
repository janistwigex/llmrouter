# llmrouter

`llmrouter` is a small model-affinity reverse proxy for **llama.cpp** servers
(`llama-server` in router/multi-model mode, or `llama-swap`).

It exposes an **OpenAI-compatible** API and routes each request to a backend that
actually serves the requested model, preferring fast/idle backends and reusing
backends that already have that model loaded.

## What it does

For a request for model `M`:

1. Only backends that have `M` are considered.
2. The first **idle** backend is used (backend order matters: put the fast one first).
3. Otherwise, a backend already serving `M` with a free slot is used.
4. Otherwise, the request **waits** until a slot frees up.
5. If no backend has `M`, the router returns `404`.

The router tracks each backend's loaded model and in-flight request count in
memory, so it must run as a **single process** (`--workers 1`).

## Features

- OpenAI-compatible `/v1/*` endpoints (chat completions, completions, embeddings, rerank, etc.)
- Automatic model discovery from each backend's `/v1/models`
- Periodic refresh of model lists
- Model-affinity routing with slot limits
- Shared or per-backend API keys
- Optional router-level API key
- Public or protected `/models` and `/health`
- Streaming responses
- Preserves llama.cpp context metadata (`meta.n_ctx`) and exposes it as
  `context_length` / `max_model_len`

## Quick start

### Run directly

```bash
pip install -r requirements.txt
uvicorn router:app --host 0.0.0.0 --port 4000 --workers 1
```

Point OpenAI-compatible clients at:

```text
http://<router-host>:4000/v1
```

### Run with Docker

```bash
docker compose up -d --build
```

Edit `docker-compose.yaml` to set your backend URLs and API key.

## Configuration

### Backends

Default backends are defined in `router.py`:

```python
BACKENDS = [
    {"name": "fast", "url": "http://192.168.1.10:8080", "slots": 2},
    {"name": "slow", "url": "http://192.168.1.11:8080", "slots": 2},
]
```

Override with the `ROUTER_BACKENDS` environment variable:

```bash
ROUTER_BACKENDS='[
  {"name":"fast","url":"http://192.168.20.9:8080","slots":2},
  {"name":"slow","url":"http://192.168.20.193:8081","slots":2}
]'
```

Backend fields:

| Field | Description |
|---|---|
| `name` | Display name |
| `url` | Backend base URL |
| `slots` | Number of concurrent requests; should match llama-server `-np` |
| `api_key` | Optional per-backend key sent as `Authorization: Bearer <key>` |
| `models` | Optional explicit model list; disables discovery for that backend |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `ROUTER_BACKENDS` | built-in list | JSON list of backends |
| `API_KEY` | none | Shared key used for clients and backends without their own key |
| `ROUTER_API_KEY` | `API_KEY` | Key clients must send to the router |
| `MODELS_REFRESH_SECONDS` | `60` | How often to rediscover models |
| `PUBLIC_MODELS` | `true` | If false, `/models` and `/health` require the router key |

## Authentication

- If `ROUTER_API_KEY` is set, clients must send `Authorization: Bearer <key>`.
- `/models`, `/v1/models`, and `/health` are public by default, like llama-server.
  Set `PUBLIC_MODELS=false` to require the key there too.
- Backends receive `API_KEY` unless they define their own `api_key`.
- If a backend has no key, the client's `Authorization` header is passed through.

## Model discovery

- Models are discovered from each backend's `/v1/models` at startup.
- Discovery repeats every `MODELS_REFRESH_SECONDS`.
- Discovery also runs when an unknown model is requested.
- If a backend defines `models`, discovery is skipped for it.
- llama-server router mode's loaded-model status is tracked while the backend is idle.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health`, `/v1/health` | Router health |
| `GET` | `/models`, `/v1/models` | Merged model list from all backends |
| `GET` | `/status` | Backend status (requires key if set) |
| `POST` | `/{path}` | Proxied to `/v1/{path}` on a selected backend |

OpenAI-compatible POST endpoints that include a `model` field are routed by model
affinity, for example:

- `/v1/chat/completions`
- `/v1/completions`
- `/v1/embeddings`
- `/v1/rerank`

## Context length

llama.cpp exposes the runtime context window under:

```json
{"id":"model.gguf","meta":{"n_ctx":1234,"n_ctx_train":4096}}
```

`llmrouter` preserves `meta` and also exposes:

- `context_length`
- `max_model_len`

so clients that expect either nested or top-level context metadata can read it.

## Status

`GET /status` returns per-backend information:

```json
[
  {
    "name": "fast",
    "loaded": "model.gguf",
    "active": 1,
    "slots": 2,
    "models": ["model.gguf"]
  }
]
```

## Limitations

- This is an **OpenAI-compatible model router**, not a generic llama.cpp reverse proxy.
- Only POST endpoints with a JSON `model` field are routed by model affinity.
- Native llama.cpp endpoints such as `/completion`, `/tokenize`, `/detokenize`,
  `/infill`, and `/apply-template` are not fully supported.
- Backend management endpoints such as `/props`, `/slots`, and `/metrics` are not
  proxied as a single merged endpoint.
- Query parameters and most non-auth headers are not currently forwarded.
- The router must run with one worker because routing state is in memory.

## Troubleshooting

- **Model not found (404):** check that the backend actually lists the model in
  `/v1/models`, or set `models` explicitly.
- **API key rejected:** ensure `API_KEY` / `api_key` matches the backend's
  `--api-key`.
- **Context length wrong or missing:** make sure the backend is llama.cpp with
  `meta.n_ctx` in `/v1/models`; restart the router after backend changes.
- **Slots exhausted:** increase backend `slots` and llama-server `-np`, or add
  another backend.
- **Backend unreachable:** the previous model list is kept; check logs for
  discovery errors.

## Disclaimer

This application and its code were created with the assistance of OpenCode
and AI-based development tools. Review, test, and validate it before using it
in production.