# Vera+ — magicpin AI Challenge bot

A FastAPI service implementing the 5 endpoints from `challenge-testing-brief.md`
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`), ready
to deploy on Render with the included `Dockerfile`. Message composition uses
**Google Gemini** via the plain REST API (no SDK dependency beyond `requests`).

---

## 1. Start here: run it locally (5 minutes)

You said you have a Gemini API key already — this is all you need.

```bash
cd magicpin-bot

# 1. Create a virtual environment and install deps
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Set your key
export GEMINI_API_KEY="paste-your-key-here"     # Windows (PowerShell): $env:GEMINI_API_KEY="..."

# 3. Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Leave that running, then in a second terminal:

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

You should get back JSON with `"status":"ok"` and, in metadata, `"model":"gemini-flash-latest"`
(if it instead says `"template-fallback (no API key set)"`, the env var isn't
set in the terminal that launched uvicorn — re-check step 2).

**If you don't want to set an env var by hand every time**, copy `.env.example`
to `.env`, fill in your key, and either `source .env` before running uvicorn,
or install `python-dotenv` and load it — the app reads plain `os.environ`, so
any way of getting the variable into the process works.

### Self-test against the bundled judge simulator

This repo already includes `judge_simulator.py` and the `dataset/` folder
from the challenge zip, so you can dry-run the full flow (context push →
tick → reply, including auto-reply/decline/intent scenarios) before you ever
deploy:

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

Note: `judge_simulator.py` has its **own separate** `LLM_PROVIDER` setting at
the top of the file (it uses an LLM to *play the merchant* and to score your
bot) — that's independent of your bot's own `GEMINI_API_KEY`. Open the file
and set `LLM_PROVIDER = "gemini"` with your same key there too if you want
the simulator itself to use Gemini (it also supports OpenAI, DeepSeek, Groq,
Ollama, OpenRouter).

### Quick manual test (no simulator)

```bash
curl -X POST http://localhost:8080/v1/context -H "Content-Type: application/json" -d '{
  "scope": "category", "context_id": "dentists", "version": 1,
  "payload": '"$(cat dataset/categories/dentists.json)"',
  "delivered_at": "2026-04-26T10:00:00Z"
}'
# ...repeat for a merchant (from dataset/merchants_seed.json) and a trigger
# (from dataset/triggers_seed.json), then:
curl -X POST http://localhost:8080/v1/tick -H "Content-Type: application/json" -d '{
  "now": "2026-04-26T10:05:00Z", "available_triggers": ["trg_001_research_digest_dentists"]
}'
```

---

## 2. How it works

- **`app/store.py`** — in-memory context store (idempotent by `(scope, context_id, version)`)
  and conversation store (turn history, sent-body log, suppression-key dedup).
- **`app/llm.py`** — calls the Gemini REST endpoint
  (`generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`)
  with `temperature=0` and `responseMimeType: application/json`, so replies
  parse straight into a dict. Any failure (missing key, timeout, blocked
  response, bad JSON) raises, and the caller falls back to a template.
- **`app/composer.py`** — builds one grounded prompt per message from the
  four context layers (category / merchant / trigger / customer), enforcing
  the brief's rules (specificity, category voice, single CTA, no fabrication,
  Hindi-English code-mix, anti-repetition). Falls back to a deterministic
  template if the LLM call fails, so the bot never returns malformed/empty
  output.
- **`app/utils.py`** — deterministic, LLM-free classification of incoming
  merchant/customer replies: auto-reply (verbatim-repeat + canned-phrase
  detection), hard decline, "call me later", intent confirmation ("let's do
  it"), and hostility/off-topic. This drives correct behavior on the brief's
  two named production failure modes (auto-reply pollution, intent-handoff
  failures) without waiting on an LLM round trip.
- **`app/main.py`** — wires the above into the 5 endpoints + an optional
  `POST /v1/teardown` that wipes state.

### `/v1/tick` policy
For each id in `available_triggers`: look up the trigger, resolve its
merchant → category (+ customer, if any), skip if its `suppression_key` was
already used or a conversation already exists for that trigger, otherwise
compose one proactive message and start a conversation. Capped at 20
actions/tick per the contract; empty `actions: []` is a valid (and often
correct) reply.

### `/v1/reply` state machine
1. Count verbatim repeats of the incoming message in this conversation.
   3+ → `end` (confirmed auto-reply). Exactly 2, or matches a canned-phrase
   list → probe once (`send`), then `end` if it repeats again.
2. Hard decline phrases → `end`.
3. "Busy / call later" phrases → `wait` (1800s).
4. Intent-confirmation phrases ("let's do it", "go ahead", "chalo") → `send`,
   with an explicit directive telling the composer to skip further
   qualification and move to action — the exact failure mode called out in
   Pattern D of the brief.
5. Hostile/off-topic → `send`, politely declining the tangent and returning
   to the original topic.
6. Otherwise → `send`, continuing naturally toward the trigger's goal.

---

## 3. Deploy on Render (Docker)

1. **Push this folder to a GitHub repo** (create a new repo, commit, push —
   `git init && git add -A && git commit -m "init" && git remote add origin <your-repo-url> && git push -u origin main`).
2. On [render.com](https://render.com): **New +** → **Web Service** → connect
   the repo. Render auto-detects the `Dockerfile`. (Or use **New +** →
   **Blueprint** and point it at this repo to use the included `render.yaml`.)
3. In the service's **Environment** tab, set:
   - `GEMINI_API_KEY` — your key (required for real LLM composition; without
     it the bot still runs and answers every call, using the deterministic
     fallback, so it degrades gracefully rather than failing).
   - `MODEL` — defaults to `gemini-flash-latest` if unset (a rolling alias to
     Google's current flash model; pin to a specific version like
     `gemini-2.5-flash` if you want stability instead of auto-updates).
   - `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, `BOT_VERSION` — shown in
     `GET /v1/metadata`.
4. Health check path is already set to `/v1/healthz` in `render.yaml`.
5. **Important — keep it to a single instance/worker.** State (contexts,
   conversations) is in-memory, exactly like the brief's own reference
   skeleton, so it must live in one process for the whole test window. The
   Dockerfile already runs uvicorn with `--workers 1`; don't scale to
   multiple instances for this challenge.
6. Deploy. Your public URL will be `https://<service-name>.onrender.com`.
   Test it the same way as local (`curl .../v1/healthz`), then submit that
   URL.

**Note on free/starter Render plans:** they can spin down when idle, which
would fail the judge's 60-second health-check polling. Use an always-on plan
for the test window (or ping `/v1/healthz` yourself every ~50s to keep it
warm).

---

## 4. Tradeoffs / what would help most

- **Cadence planning** (open challenge #3) is intentionally simple: one
  action per trigger, deduped by `suppression_key`. A production version
  would model weekly send budgets per merchant and diversify trigger
  *kinds* sent per week (the brief's "diversified conversation portfolio"
  point), not just dedup.
- **Language detection per turn** (open challenge #4) currently reads the
  merchant/customer's static `languages` / `language_pref` field rather than
  detecting a mid-conversation script switch; a real implementation would
  detect Devanagari/Hinglish tokens turn-by-turn.
- The **hostile/off-topic keyword list** in `utils.py` is intentionally
  short — it's a fast pre-filter, not a classifier; final judgment on tone
  is left to the LLM via the `directive` mechanism.
- Post-submission context injection (new digest items, updated performance,
  new triggers, late-arriving `CustomerContext`) is handled for free because
  `/v1/context` always re-reads live state at compose time — no caching of
  stale contexts anywhere.
