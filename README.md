# ESM Agentic Ops

A demo app that shows how AI agents can help an IT support team with Enterprise Service Management (ESM):
handling alerts, incidents, service requests and knowledge-base articles.

Everything runs on **made-up data**. Nothing connects to a real ticketing or identity system.

## What it does

Four AI agents each handle one kind of work:

| Agent | What it does |
|---|---|
| **Alert Correlation** | Groups duplicate monitoring alerts into one incident instead of fifty. |
| **Incident Triage** | Sets the priority and team for an incident and suggests a fix from the knowledge base. |
| **SR Fulfilment** | Completes simple service requests (such as a password reset) on its own. |
| **KB Curation** | Scores knowledge-base articles and drafts fixes for out-of-date ones. |

Agents never act on their own. Every action they propose goes through a **Guardrail** first, which
either approves it, holds it for a person to approve, or blocks it. The Guardrail follows fixed rules,
so its decisions are predictable.

## What's in this repo

```
backend/   the server: Python, FastAPI, the agents (LangGraph) and a SQLite database
frontend/  the web console: React + Vite
files/     the sample spreadsheets the database is built from (read-only)
docs/      product, design and architecture docs (start at docs/README.md)
```

## Before you start

You need:

- **Python 3.10 or newer**: check with `python --version`
- **Node.js 18 or newer**: check with `node --version`
- **An Anthropic API key** (optional). Without one, use `rules` mode (see [AI modes](#ai-modes)).

## Run it

You need two terminals: one for the backend and one for the frontend.

### 1. Start the backend

macOS / Linux:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/uvicorn app.api.main:app --port 8000
```

Windows (PowerShell):

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
.venv\Scripts\uvicorn app.api.main:app --port 8000
```

The first start builds the database from the spreadsheets, which takes a moment.
When you see `Application startup complete`, the backend is ready.

### 2. Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

### 3. Open the app

Go to **http://localhost:5173** in your browser.

> **Port already in use?** Start the backend on another port (for example `--port 8001`), then
> start the frontend with `API_TARGET=http://127.0.0.1:8001 npm run dev -- --port 5174`
> (in PowerShell: `$env:API_TARGET="http://127.0.0.1:8001"; npm run dev -- --port 5174`).

## AI modes

Set `LLM_MODE` in `backend/.env`:

| Mode | What happens | Needs an API key? |
|---|---|---|
| `live` (default) | Claude makes each agent decision. The Guardrail still uses fixed rules. | Yes: set `ANTHROPIC_API_KEY` in `backend/.env` |
| `rules` | Each agent uses built-in rules instead of AI. Fast and free. | No |

**No API key? Set `LLM_MODE=rules`.** If a live call fails, the agent falls back to rules automatically.

## Try the demo

1. Start the main outage story (on Windows, type `curl.exe` instead of `curl`):
   ```bash
   curl -X POST http://localhost:8000/api/scenarios/licensecore_outage/start
   ```
   Then open **Live feed** and watch 53 alerts collapse into a single incident in about a minute.
2. Open **Incidents** to see how the agent prioritised it and why.
3. Open **Guardrail queue**. The risky fix is waiting for you. Press **Approve**.
4. Open **Self-service** and try a *Password reset* (done instantly) and *Admin access* (held for approval).
5. Open **Knowledge base**, review a drafted article update, and publish it.
6. Open **KPIs** to compare the results with the starting figures.

To run a scenario again, first press **Controls → Reset demo data**.

For a full walkthrough, see the [simulation guide](docs/guides/simulation-guide.md).

## Run the tests

```bash
cd backend
.venv/bin/python -m pytest        # Windows: .venv\Scripts\python -m pytest
```

Tests use `rules` mode, so they need no API key or internet. To check the frontend, run
`npm run typecheck` in `frontend/`.

## Good to know

- **It's a prototype.** There is no login, it uses mock integrations, and it has not been load-tested.
- **The data is fake.** The spreadsheets are random sample data. KPI "before" figures come from the case study,
  and each "after" figure is labelled *measured* or *modelled*.
- **Time.** The app clock follows the local time in `Asia/Manila`. To replay on the fixed case-study date, set
  `ESM_SIM_START=2025-08-04T08:55:00` before starting the backend.
- **Settings to tweak.** Application tiers, approval rules and the change-freeze calendar are in `backend/config/`.

## Learn more

- [docs/README.md](docs/README.md): index of all docs
- [Product requirements](docs/product/prd.md)
- [Simulation guide](docs/guides/simulation-guide.md)
- [Known gaps and KPI results](docs/product/mvp-requirements.md)

## Video overview

A 30-second explainer of how the app works: alerts grouped into one incident, the four agents, the Guardrail
and human approval.

[Watch the video (MP4)](media/esm-agentic-ops-explainer.mp4)

## License

[MIT](LICENSE)
