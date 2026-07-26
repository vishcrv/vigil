# vigil

An AML (anti-money-laundering) detection agent over the IBM synthetic transaction dataset. Ask
it a question in plain English; it decides which tools to run, scores the accounts involved, and
explains what it found.

- **Agent** — hand-rolled tool-calling loop, provider-agnostic (`LLM_PROVIDER`)
- **Detection** — Isolation Forest + LOF + z-score, plus a rules engine for 8 laundering motifs
- **Interface** — FastAPI backend, React + Vite frontend, SQLite audit trail

---

## Setup

### 1. Dataset

Download the IBM *Synthetic Transaction Data for AML* set (HI-Small) from Kaggle and put three
files in `backend/data/raw/`:

```
backend/data/raw/HI-Small_Trans.csv
backend/data/raw/HI-Small_accounts.csv
backend/data/raw/HI-Small_Patterns.txt
```

Manual browser download, deliberately — no Kaggle credentials to manage, and this is a one-time
fetch rather than a repeatable pipeline step.

### 2. Backend

```bash
cd backend
python -m pip install -r requirements.txt
cp .env.example .env          # then add your key, see below
```

Get a **Gemini** key from <https://aistudio.google.com/apikey>. Not the Google Cloud Console —
a Cloud-issued key lands in a project with zero free-tier quota and fails every call with
`429 RESOURCE_EXHAUSTED` no matter what you enable on it.

```
LLM_PROVIDER=gemini
GOOGLE_API_KEY=...
```

Anthropic is implemented and is the code default, but has never been exercised against the live
API. Groq raises `NotImplementedError`.

### 3. Build the data artifacts

In order — each step consumes the previous one's output. Roughly 10 minutes total.

```bash
cd backend
python scripts/enrich.py            # raw CSV  -> data/HI-Small_Enriched.parquet  (~350 MB)
python scripts/train_models.py      #          -> data/models/*.joblib + metadata.json
python scripts/build_rule_hits.py   #          -> data/HI-Small_Rule_Hits.parquet
```

All outputs are gitignored and reproducible; `enrich.py` is deterministic and reproduces the
same Parquet byte for byte.

### 4. Run

Two terminals:

```bash
cd backend && uvicorn main:app --reload          # http://127.0.0.1:8000  (docs at /docs)
```

```bash
cd frontend && npm install && npm run dev        # http://localhost:5174
```

`frontend/.env` should hold `VITE_API_BASE_URL=http://127.0.0.1:8000` — copy it from
`.env.example`. Use `127.0.0.1`, not `localhost`: uvicorn binds IPv4, browsers resolve
`localhost` to IPv6 `::1` first, and the request is refused before it reaches the server.

> If `npm install` stalls on `plotly.js`, retry with `npm install --maxsockets=1`. Parallel
> fetches of that tarball get reset on some networks; serialising them fixes it.

---

## Try it

| Query | What it exercises |
|---|---|
| `Is customer 1004286A8 suspicious?` | entity lookup → `anomaly → risk → explain`, flags CRITICAL |
| `Find structuring patterns in the last 30 days` | pattern search over a filtered slice |
| `How many customers made 10 or more transactions under $10,000?` | aggregate with a threshold |
| `What data do we have?` | dataset overview |

Three tabs: **Investigate** (chat), **Escalations** (audit trail), **Dashboard** (aggregates
across every session).

---

## API

| Route | Purpose |
|---|---|
| `POST /api/v1/analyze` | run the agent, persist the run, return an `AgentResult` |
| `POST /api/v1/analyze/stream` | same run as Server-Sent Events, reporting each tool as it fires |
| `POST /api/v1/escalate` | record a human escalation (with an optional note) |
| `POST /api/v1/escalate/undo` | withdraw one |
| `GET /api/v1/escalations` | the audit trail |
| `GET /api/v1/stats` | dashboard aggregates |

---

## Tests

```bash
cd backend  && python -m pytest -q     # 423 passed, 7 skipped
cd frontend && npm test                # 10 passed
```

The 7 skips are live-API tests; run them with `VIGIL_LIVE_TESTS=1` and a real key. They spend
quota — one `/analyze` is several model calls.

Optional analysis scripts:

```bash
python scripts/validate_risk_levels.py   # risk-level precision/recall against ground truth
python scripts/tune_blend.py             # search the risk blend constants
python scripts/live_check.py             # one end-to-end run against the live provider
```

---

## Known limitations

Stated plainly rather than discovered during a demo.

- **CRITICAL precision is ~20–100%, not 100%.** No clean account in a 2,000-account sample
  reached CRITICAL, so there are no observed false positives to estimate from; the point
  estimate and the bootstrap both collapse to 100% because the cell is empty. The rule-of-three
  lower bound is ~20%. See `backend/docs/ml_spec.md`.
- **Detection is intrinsically weak.** The detectors reach ~2.3x lift over a 0.1% base rate, and
  only 2 of 8 rules are strong. Recall at HIGH+ is 66%; most laundering in this dataset is not
  caught. The rules carry the signal, not the detectors.
- **`gemini-flash-lite` follows routing instructions imperfectly.** A pattern search may flag
  one account where three were asked for, and some phrasings route to the wrong tool. A larger
  model fixes this at the cost of a much smaller free-tier quota.
- **Free-tier quota is small.** One query is several model calls; a `429` in the UI is the quota,
  not a fault.
- Runs locally only. No auth, no deployment, single user.

## Layout

```
backend/
  agent/      tool-calling loop + provider wrappers
  api/routes/ FastAPI routes
  ml/         the 12 ML modules (5 tools + support)
  tools/      public tool surface, re-exported from ml/
  scripts/    one-time build pipeline + analysis
  docs/       ml_spec.md, phase notes, audit
frontend/src/ React app (chat, escalations, dashboard)
spec.md       the project spec
```
