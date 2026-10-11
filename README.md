# Weekend Micro-Adventure

One screen. Pick time, transport, vibe, location. Press **Send me out**. Get one real
adventure card — one place, a short plan, and a leave-by time. No lists, no feed, no login.

Gemma (open-weights) runs through **Backboard**. Places come from SerpApi.
Weather and daylight from Open-Meteo (free, no key).

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the keys
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 . Health check: `GET /api/health` -> `{"status":"ok"}`.

## Run tests

```bash
pytest -q
```

Tests never call a live API — they use saved fixtures and mocks.

## Current model

```
BACKBOARD_MODEL=google/gemma-4-26b-a4b-it
BACKBOARD_PROVIDER=openrouter
```

`scripts/list_models.py` lists every Backboard model matching `gemma` (there are ~1000,
mostly community fine-tunes on the `featherless` provider). Pick the `openrouter` entry
for the Google-namespaced model: 262k context, versus 32k on the `featherless` duplicate.

## Typical model latency

A one-shot `generate_json` call against Gemma returns in about **1.5–2s**. That is the
budget the whole request must fit inside, which is why the plan allows only one model
call plus one retry.

## Deploy

Push to GitHub, create a Render Web Service, set the secrets in the dashboard.
`/api/health` is the health check path. Free services sleep when idle — wake the URL
before a demo. `render.yaml` is added in Step 10.