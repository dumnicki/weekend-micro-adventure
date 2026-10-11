# PLAN.md — Weekend Micro-Adventure

Build plan for coding agents. Read the whole file before you write code.

---

## 1. What we are building

A one-page web app that sends a person outside.

The user picks three things:

- **Time budget:** `1h`, `half_day` (4h), or `full_day` (8h)
- **Transport:** `foot`, `bike`, or `transit`
- **Vibe:** `quiet`, `active`, or `curious`
- **Location:** a city/address text, or the browser's GPS

The user presses **Send me out**. The app returns **one** adventure card: one real place, a short plan, and a leave-by time. There is one **Not feeling it** button for a re-roll. Nothing else.

**Theme:** "Touch Grass". The screen must be the shortest part of the experience. So: no lists, no feed, no login, no settings. One answer.

**AI core:** Gemma (open-weights), called through **Backboard**.

**Other tools:** SerpApi (place search), Open-Meteo (weather and sunset, free, no key), Render (hosting).

### Why the AI is useful here

The model does three jobs that plain code cannot do well:

1. Pick the best place for the user's mood and constraints from a short list.
2. Turn raw search data into a small, readable plan.
3. Be honest. If a fact is not in the data, it must say "check before you go".

---

## 2. Rules for agents (read first)

1. **Code decides, the model writes.** Search, filtering, distance, time math, and daylight limits are done in plain Python. The model only picks one candidate and writes the card text.
2. **One model call per request** (plus one retry if the JSON is bad). Do not build multi-step agent loops. They are slow and fragile.
3. **No invented facts.** The model may only use facts in the `candidates` data we give it. The server must check the output (see section 8).
4. **Isolate Backboard.** Only `app/services/llm.py` may import or call Backboard. If the API changes, only one file changes.
5. **Do not guess API details.** For Backboard, read the official docs at `backboard.io/docs` and use the official `backboard-sdk` if it fits. For SerpApi, read `serpapi.com/google-maps-api`. If a field name in this plan differs from the real API, the real API wins. Update the fixture and the code.
6. **Keep it small.** No database. No auth. No front-end framework. No build step for the front end.
7. **Never commit secrets.** Keys come from environment variables only.
8. **Every step has a check.** Do not move to the next step until the check passes.
9. **Save API budget.** SerpApi has a small free quota. Cache results (section 7.4) and use saved fixtures in tests. Tests must not call live APIs.

---

## 3. Tech stack

| Part | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Simple, good SDK support |
| Web framework | FastAPI + Uvicorn | Small, fast, auto docs at `/docs` |
| HTTP client | `httpx` | Async, timeouts |
| Validation | `pydantic` v2 | Request and card schemas |
| LLM | Gemma via **Backboard** | Open-weights core of the project |
| Place search | **SerpApi** (`google_maps` engine) | Real places with coordinates and ratings |
| Weather and sunset | **Open-Meteo** forecast + geocoding APIs | Free, no key |
| Front end | One `index.html`, plain JS, plain CSS | No build step |
| Tests | `pytest`, `pytest-asyncio`, `respx` | Mock HTTP |
| Hosting | **Render** (Web Service) | Simple deploy from GitHub |

Optional bonus (only after the app is done): Sentry tracing, to show why a place was picked.

---

## 4. Environment variables

Put these in `.env.example` (no real values) and in the Render dashboard.

```
SERPAPI_API_KEY=
BACKBOARD_API_KEY=
BACKBOARD_MODEL=            # exact Gemma model id from the Backboard model library
BACKBOARD_PROVIDER=         # provider name for that model, if Backboard needs it
APP_ENV=dev                 # dev | prod
CACHE_TTL_SECONDS=3600
LLM_TIMEOUT_SECONDS=30
```

Do not hardcode the Gemma model id. Step 1 finds the exact id.

---

## 5. Project structure

```
micro-adventure/
├── PLAN.md                      # this file
├── README.md                    # short: what it is, how to run, how to deploy
├── .env.example
├── .gitignore                   # include .env, __pycache__, .venv
├── .python-version              # 3.12
├── requirements.txt
├── render.yaml                  # Render service definition
│
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, mounts static files, includes routes
│   ├── config.py                # reads env vars (pydantic-settings or os.environ)
│   ├── schemas.py               # Pydantic models: AdventureRequest, Candidate, AdventureCard
│   ├── routes.py                # POST /api/adventure, GET /api/health
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── geocode.py           # city text -> lat/lng (Open-Meteo geocoding)
│   │   ├── weather.py           # forecast + sunset (Open-Meteo)
│   │   ├── places.py            # SerpApi google_maps search + raw result parsing
│   │   ├── candidates.py        # filter, dedupe, distance, time math, scoring
│   │   ├── llm.py               # ONLY file that talks to Backboard
│   │   ├── planner.py           # orchestrates the full pipeline
│   │   ├── validate.py          # checks the model output against the candidates
│   │   └── cache.py             # tiny in-memory TTL cache
│   │
│   ├── prompts/
│   │   ├── system.md            # system prompt for Gemma
│   │   └── user_template.md     # user message template with placeholders
│   │
│   └── static/
│       ├── index.html           # the one page
│       ├── app.js               # form logic, fetch, render card
│       └── style.css
│
├── tests/
│   ├── fixtures/
│   │   ├── serpapi_maps_sample.json   # saved real SerpApi response
│   │   ├── openmeteo_sample.json      # saved real Open-Meteo response
│   │   └── llm_good.json / llm_bad.json  # sample model outputs
│   ├── test_candidates.py
│   ├── test_validate.py
│   ├── test_planner.py          # full pipeline with mocked HTTP and mocked LLM
│   └── test_routes.py
│
└── scripts/
    ├── list_models.py           # prints Backboard models that match "gemma"
    └── smoke_test.py            # calls the running app once with a real city
```

**Why this layout:** every pipeline stage is its own small module, so an agent can build and test one stage at a time. `planner.py` is the only place that wires stages together.

---

## 6. Data contracts

### 6.1 Request — `POST /api/adventure`

```json
{
  "time_budget": "1h | half_day | full_day",
  "transport": "foot | bike | transit",
  "vibe": "quiet | active | curious",
  "location_text": "string or null",
  "lat": "number or null",
  "lng": "number or null",
  "exclude_ids": ["place_id", "..."]
}
```

Rules: provide `location_text` **or** both `lat` and `lng`. `exclude_ids` is used by the re-roll button. Return HTTP 422 on bad input.

### 6.2 Candidate (internal)

```json
{
  "id": "place_id from SerpApi",
  "name": "string",
  "type": "string",
  "address": "string or null",
  "lat": 0.0,
  "lng": 0.0,
  "rating": 4.6,
  "reviews": 1280,
  "description": "string or null",
  "hours_text": "string or null",
  "distance_km": 3.2,
  "travel_minutes_one_way": 40,
  "activity_minutes_available": 120
}
```

### 6.3 Response card — what the model must produce

```json
{
  "candidate_id": "must match one candidate id",
  "title": "short headline, max 8 words",
  "duration_minutes": 150,
  "getting_there": "1-2 sentences",
  "plan": ["step 1", "step 2", "step 3"],
  "tip": "one practical tip",
  "leave_by": "HH:MM (24h, local time)",
  "check_before_you_go": "one sentence about anything not confirmed by the data"
}
```

The API response adds fields from the candidate: `place_name`, `address`, `lat`, `lng`, `maps_link`, and `weather_summary`. These come from code, not from the model.

---

## 7. Pipeline logic (exact rules)

`planner.py` runs these stages in order.

### 7.1 Resolve location

- If `lat`/`lng` are given, use them.
- Else call Open-Meteo geocoding: `https://geocoding-api.open-meteo.com/v1/search?name=<text>&count=1`.
- If no result, return HTTP 404 with the message "Could not find that place".

### 7.2 Get weather and daylight

Call `https://api.open-meteo.com/v1/forecast` with:

- `latitude`, `longitude`
- `daily=sunset,sunrise,precipitation_probability_max,temperature_2m_max,temperature_2m_min`
- `hourly=precipitation_probability,temperature_2m`
- `timezone=auto`

Produce a small object: `{sunset_local, temp_c, rain_chance_percent, summary}`.

`summary` is a short string made by code, for example `"12°C, 20% rain chance, sunset 17:42"`.

Weather failure must **not** fail the request. If it fails, continue without weather and tell the model "weather unknown".

### 7.3 Choose the search radius and travel speed

Use this table. Distances are the maximum straight-line radius in km.

| Transport | 1h | half_day (4h) | full_day (8h) | Speed used for time math |
|---|---|---|---|---|
| foot | 2 | 6 | 12 | 4.5 km/h |
| bike | 6 | 20 | 40 | 14 km/h |
| transit | 5 | 25 | 50 | 20 km/h plus 10 min wait |

Time math for each candidate:

```
travel_minutes_one_way = distance_km / speed * 60   (+10 for transit)
activity_minutes_available = budget_minutes - 2 * travel_minutes_one_way
```

`budget_minutes` is 60, 240, or 480. Straight-line distance is lower than real route distance, so multiply distance by **1.3** before the time math.

Drop any candidate with `activity_minutes_available < 20`.

### 7.4 Search places (SerpApi)

Use the `google_maps` engine. Run **at most 2 queries** per request.

Request shape:

```
GET https://serpapi.com/search.json
  engine=google_maps
  type=search
  q=<query>
  ll=@<lat>,<lng>,14z
  hl=en
  api_key=<key>
```

Query words by vibe:

| Vibe | Query 1 | Query 2 |
|---|---|---|
| quiet | `quiet park` | `nature reserve` |
| active | `hiking trail` | `scenic viewpoint` |
| curious | `botanical garden` | `nature trail` |

Read results from `local_results`. Expected fields: `title`, `place_id`, `gps_coordinates.latitude`, `gps_coordinates.longitude`, `rating`, `reviews`, `type` or `types`, `address`, `description`, `hours`. **Check the real field names against a saved response** and fix the parser if needed.

**Cache:** key = `(round(lat,2), round(lng,2), query)`. TTL from `CACHE_TTL_SECONDS`. This protects the SerpApi quota during testing and re-rolls. A re-roll must **not** call SerpApi again if the cache has the results.

### 7.5 Filter and rank candidates (`candidates.py`)

In this order:

1. Drop items with no coordinates or no `place_id`.
2. Drop duplicate `place_id`.
3. Drop ids in `exclude_ids`.
4. Keep only outdoor-like places. Allow-list by keyword match on `type`/`types` (lowercase): `park`, `trail`, `garden`, `forest`, `lake`, `reserve`, `viewpoint`, `nature`, `hiking`, `beach`, `river`, `woods`. Drop shops, restaurants, hotels.
5. Compute `distance_km` with the haversine formula. Drop anything beyond the radius from 7.3.
6. Do the time math from 7.3. Drop short-activity items.
7. Score: `rating * log(1 + reviews)`. Missing rating counts as 3.5.
8. Keep the **top 8**.

If fewer than 1 candidate remains, return HTTP 200 with `{"empty": true, "message": "Nothing good nearby for that. Try a longer time or another transport."}`. Do not call the model.

### 7.6 Daylight limit

Compute `latest_return_time = sunset - 30 minutes` (today). If the user's budget would run past that, reduce `budget_minutes` so the trip ends by `latest_return_time`. If it is already after that time, use the next day's sunrise-to-sunset window and say "tomorrow" in the card. Keep this simple: pass `now_local`, `latest_return_time`, and `max_trip_minutes` to the model.

### 7.7 Call the model (`llm.py`)

- Send the system prompt (section 9) and the filled user template.
- Ask for **JSON only**.
- Parse the JSON. Strip code fences if present.
- If the JSON is invalid or fails validation: **retry once** with the error message added. If it fails again, use the fallback (7.9).
- Timeout: `LLM_TIMEOUT_SECONDS`.

### 7.8 Validate (`validate.py`)

Reject the model output (and retry/fallback) if any of these is true:

- `candidate_id` is not in the candidate list.
- `leave_by` is not `HH:MM`, or it is later than `latest_return_time`.
- `plan` has fewer than 2 or more than 5 steps.
- `duration_minutes` is more than `activity_minutes_available` of the chosen candidate.
- `title` is longer than 80 characters.
- Any field is empty.

### 7.9 Fallback (no model needed)

If the model fails twice, pick the top-scored candidate and build a plain card from a template:

```
title: "Go to {name}"
plan: ["Head to {name} ({distance_km} km away).", "Walk or relax for about {duration} minutes.", "Head back before {leave_by}."]
tip: "Bring water and a warm layer."
check_before_you_go: "Opening hours and conditions were not checked."
```

The app must **never** show an error screen when it can show a fallback card.

### 7.10 Build the final response

Merge the card with candidate fields (`place_name`, `address`, `lat`, `lng`) and a `maps_link` built as `https://www.google.com/maps/search/?api=1&query=<lat>,<lng>&query_place_id=<id>`. Add `weather_summary`. Return JSON.

---

## 8. Hallucination control (important)

The main risk is a made-up trail or a made-up opening time. Defenses:

1. The model only sees the 8 candidates. It must pick by `candidate_id`.
2. The system prompt forbids facts that are not in the data.
3. The server checks `candidate_id`, time, and duration (7.8).
4. The card always has `check_before_you_go`.
5. The place name, address, and map link come from code, not from the model.

---

## 9. Prompts

### 9.1 `app/prompts/system.md`

```
You are a calm local guide. You send people outside for a short trip.

You receive:
- the user's constraints (time, transport, vibe)
- today's weather and daylight limits
- a list of candidate places in JSON

Your job: choose exactly ONE candidate and write a short plan for it.

Rules:
1. Choose only from the candidates. Return its "id" as candidate_id.
2. Use only facts that appear in the candidate data. Do not invent trails,
   paths, gates, food stops, opening hours, or landmarks.
3. If a useful detail is missing (hours, route, entrances), do not guess.
   Say so in "check_before_you_go".
4. Match the vibe. quiet = calm and low-crowd. active = movement and effort.
   curious = something interesting to notice.
5. Respect the numbers we give you. duration_minutes must not exceed
   activity_minutes_available for the chosen place. leave_by must not be later
   than latest_return_time.
6. If rain chance is above 60 percent, prefer the shorter option and mention
   a rain layer in the tip.
7. Write in plain, simple English. Short sentences. No hype. No emojis.
8. Output JSON only. No text before or after. No code fences.

Output format:
{
  "candidate_id": "string",
  "title": "string, max 8 words",
  "duration_minutes": 0,
  "getting_there": "string, 1-2 sentences",
  "plan": ["string", "string", "string"],
  "tip": "string",
  "leave_by": "HH:MM",
  "check_before_you_go": "string"
}
```

### 9.2 `app/prompts/user_template.md`

```
Constraints:
- time_budget: {time_budget} ({budget_minutes} minutes total, including travel)
- transport: {transport}
- vibe: {vibe}

Now: {now_local}
Latest return time: {latest_return_time}
Weather: {weather_summary}

Candidates (JSON):
{candidates_json}

Pick one and return the JSON card.
```

For a re-roll, the `exclude_ids` are already removed from the candidates, so the prompt does not change.

---

## 10. Front end spec (`app/static/`)

Keep it to one screen. Mobile first.

**Layout (top to bottom):**

1. Title: "Weekend Micro-Adventure"
2. Location field + small "Use my location" button (uses `navigator.geolocation`)
3. Three groups of big buttons (segmented controls): time, transport, vibe
4. Big button: **Send me out**
5. Result area (empty at first)

**Result card shows:** title, place name, duration, weather line, getting there, 3 plan steps, tip, leave-by time (big), `check_before_you_go` (small, muted), "Open in Maps" link.

**Two buttons only under the card:** **Not feeling it** (re-roll, sends `exclude_ids`) and nothing else. Allow **one** re-roll, then disable the button. This keeps screen time short on purpose.

**States:** loading (one short line, e.g. "Finding a place..."), empty message, error message ("Something went wrong. Try again."). No spinners that run for more than 20 seconds.

**Style:** big text, high contrast, green/cream palette, no images needed. Works at 360px width. No external fonts or libraries.

**Footer line:** show "Time on screen: Xs" using a simple timer from page load to the moment the user presses Send me out. This is part of the demo story.

---

## 11. Build steps

Do the steps in order. Each step lists what to build and the check that proves it works.

### Step 0 — Project setup

- Create the folder structure from section 5 with empty files.
- Write `requirements.txt`: `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pydantic-settings`, `python-dotenv`, `backboard-sdk` (if the docs recommend it), `pytest`, `pytest-asyncio`, `respx`.
- Write `.env.example`, `.gitignore`, `.python-version`.
- `config.py` reads the env vars.

**Check:** `pip install -r requirements.txt` works. `uvicorn app.main:app` starts. `GET /api/health` returns `{"status":"ok"}`.

### Step 1 — Find the Gemma model on Backboard

- Read the Backboard docs. Create an API key.
- Write `scripts/list_models.py`: list models and print the ones whose name contains `gemma`.
- Put the exact model id (and provider, if needed) in `.env`.
- Write `llm.py` with one function: `async def generate_json(system: str, user: str) -> dict`.
- Do one real call with a simple test prompt that asks for `{"ok": true}`.

**Check:** `python -c` call to `generate_json` returns a parsed dict from Gemma. Note the typical latency in `README.md`.

> If Backboard needs an assistant and a thread for each call, create them once on startup (or lazily) and reuse them. Keep this logic inside `llm.py`.

### Step 2 — Prompt first, with fake data

- Write `app/prompts/system.md` and `user_template.md` (section 9).
- Build 8 fake candidates by hand in a JSON file.
- Run the prompt through `llm.py` 10 times with different constraints.
- Tune the prompt until the card is valid JSON and has no invented facts in at least 9 of 10 runs.

**Check:** 9/10 outputs pass the rules in 7.8. Save one good and one bad output as test fixtures.

### Step 3 — Schemas and validation

- Write `schemas.py` (`AdventureRequest`, `Candidate`, `AdventureCard`).
- Write `validate.py` (rules from 7.8).
- Write `test_validate.py` with the good and bad fixtures.

**Check:** `pytest tests/test_validate.py` passes.

### Step 4 — Geocoding and weather

- Write `geocode.py` and `weather.py` (7.1, 7.2).
- Save one real Open-Meteo response to `tests/fixtures/openmeteo_sample.json`.
- Weather failure returns `None`, not an exception.

**Check:** A script prints lat/lng for a city and the weather summary string. Test with `respx` mocks passes.

### Step 5 — Place search

- Write `places.py` and `cache.py` (7.4).
- Call SerpApi **once** by hand with a real city and save the full response to `tests/fixtures/serpapi_maps_sample.json`. Do not call it again in tests.
- Fix the parser so it matches the real field names.

**Check:** Parser turns the fixture into a list of raw place objects with id, name, coordinates, and rating. Cache returns the second call without HTTP.

### Step 6 — Candidate filtering

- Write `candidates.py` (7.3, 7.5, 7.6): haversine, time math, allow-list, dedupe, scoring, top 8.
- Write `test_candidates.py` using the SerpApi fixture. Test: radius filter, exclude ids, short-activity drop, empty result.

**Check:** `pytest tests/test_candidates.py` passes. For the fixture, the output has 1–8 sorted candidates.

### Step 7 — Planner (the pipeline)

- Write `planner.py`: location → weather → places → candidates → model → validate → retry once → fallback → merge card (7.1–7.10).
- Write `test_planner.py` with mocked HTTP and a mocked `llm.generate_json`. Cover: good path, bad JSON then good retry, bad twice then fallback, empty candidates, weather down.

**Check:** All planner tests pass. No test calls a live API.

### Step 8 — API route

- Write `routes.py`: `POST /api/adventure` and `GET /api/health`.
- Map errors: 422 bad input, 404 unknown place, 200 with `empty:true`, never 500 for model failures (fallback instead).
- Write `test_routes.py` with FastAPI `TestClient`.

**Check:** `curl` with a real city returns a full card in under 15 seconds (first call) and under 8 seconds (cached search).

### Step 9 — Front end

- Build `index.html`, `app.js`, `style.css` (section 10).
- Mount `static/` in `main.py` and serve `index.html` at `/`.
- Implement the re-roll with `exclude_ids` (one use only).
- Add the "Time on screen" timer.

**Check:** On a phone-width browser window: choose options, press the button, see a card. Re-roll gives a different place. The second re-roll is blocked.

### Step 10 — Deploy to Render

- Write `render.yaml`:

```yaml
services:
  - type: web
    name: micro-adventure
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /api/health
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.3
      - key: SERPAPI_API_KEY
        sync: false
      - key: BACKBOARD_API_KEY
        sync: false
      - key: BACKBOARD_MODEL
        sync: false
      - key: BACKBOARD_PROVIDER
        sync: false
      - key: APP_ENV
        value: prod
```

- Push to GitHub. Create the Render service from the repo. Add secrets in the dashboard.
- Run `scripts/smoke_test.py` against the Render URL.

**Check:** The public URL loads on a phone and returns a real card. (Free Render services sleep when idle. Open the URL once before a demo to wake it.)

### Step 11 — Real-world test

- Use the app on a phone with 3 different locations and 3 different time budgets.
- Fix the top 3 problems (bad places, wrong times, confusing text).
- **Go on one adventure the app suggests.** Record it. This is the demo footage.

**Check:** At least one adventure is completed outside.

---

## 12. Optional extras (only if all steps pass)

Do these in this order. Stop when time runs out.

1. **Backboard memory for past picks.** Store each shown `place_id` against an anonymous session id in a Backboard thread or memory, and exclude them next time. Fallback is the client-side `exclude_ids` that already exists.
2. **Sentry tracing.** Add the Sentry SDK and wrap the planner stages in spans (search, filter, model call, validate). This shows *why* a place was picked.
3. **Surprise challenge.** Add one small optional field to the card, for example "Find three different leaf shapes". Keep it to one sentence.
4. **Shareable link.** `GET /a/{id}` shows the same card. Needs small storage, so only do it if memory from step 1 is already working.

---

## 13. Out of scope

Do **not** build: user accounts, a database, a chat interface, multiple results, maps inside the page, push notifications, an admin page, or multi-agent workflows. These break the "short screen time" idea.

---

## 14. Definition of done

- [ ] Public Render URL works on a phone.
- [ ] One request returns one valid card using Gemma through Backboard.
- [ ] Re-roll works once and returns a different place.
- [ ] Model output is validated. A fallback card appears if the model fails.
- [ ] Weather failure does not break the app.
- [ ] All tests pass without network access.
- [ ] No secrets in the repo.
- [ ] `README.md` explains how to run, test, and deploy in under 30 lines.
- [ ] Demo footage: about 10 seconds on the screen, then the real outing. Caption: "Screen time: 10s. Outside time: 45+ min."

---

## 15. Quick reference

**Open-Meteo geocoding:** `GET https://geocoding-api.open-meteo.com/v1/search?name=Vienna&count=1`

**Open-Meteo forecast:** `GET https://api.open-meteo.com/v1/forecast?latitude=48.2&longitude=16.37&daily=sunrise,sunset,precipitation_probability_max,temperature_2m_max,temperature_2m_min&timezone=auto`

**SerpApi Google Maps:** `GET https://serpapi.com/search.json?engine=google_maps&type=search&q=quiet+park&ll=@48.2,16.37,14z&api_key=KEY`

**Haversine (km):**

```python
from math import radians, sin, cos, asin, sqrt

def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(a))
```

**Run locally:** `uvicorn app.main:app --reload`
**Run tests:** `pytest -q`