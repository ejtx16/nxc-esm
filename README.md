# NexaCore Agentic Ops Intelligence — prototype

A working multi-agent prototype for NexaCore's Application Managed Services, built to the plan in
[implementation-plan.md](docs/planning/implementation-plan.md) (PRD: [prd.md](docs/product/prd.md), decision: [ADR-0001](docs/adr/0001-prototype-approach.md)).
Four specialist agents (Alert Correlation, Incident Triage, SR Fulfilment, KB Curation) run on a shared SQLite data layer,
coordinated by a LangGraph orchestrator and constrained by a deterministic Guardrail. Everything runs on mock data.

```
backend/   Python 3.10+ — FastAPI, LangGraph graph, agents, mock tools, SQLite, scenario generator, KPI simulation, tests
frontend/  React 18 + Vite + TypeScript console (talks to the backend over /api)
files/     the four source spreadsheets (dummy data, read-only)
docs/      product, architecture, ADRs, design records, plan, guides, validation (index: docs/README.md)
```

## Run it

```bash
# 1. backend (first run creates backend/data/nexacore.db from the spreadsheets and scores the KB)
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # optional: add ANTHROPIC_API_KEY to use live Claude
.venv/bin/uvicorn app.api.main:app --port 8000

# 2. frontend (second terminal)
cd frontend
npm install
npm run dev                     # http://localhost:5173 (proxies /api to :8000)
```

`npm run build` in `frontend/` produces `frontend/dist`, which the API then serves itself at http://localhost:8000.

### LLM modes
| Mode | What runs |
|---|---|
| `live` (default) | Claude Opus 5.5 (`claude-opus-5-5`) makes every agent decision via structured outputs: each alert, triage, service request, KB score and draft. Each call is an `llm` span in the Audit trail with the context the agent collected and Claude's answer. The Guardrail stays deterministic. |
| `rules` | Every agent uses its deterministic path. No API access needed. Used by tests and the KPI simulation. |

Set `LLM_MODE` in `backend/.env`. Credentials: `ANTHROPIC_API_KEY` in `backend/.env`
(or an `ant auth login` profile). If the key is not workspace-scoped, also set `ANTHROPIC_WORKSPACE_ID`.

## Demo script (about 8 minutes)

| # | Page | What to do and show |
|---|---|---|
| 1 | KPIs | The case baselines: 48 hr MTTR, 53% noise, 84% SR breach, $31M. Note the measured/modelled labels. |
| 2 | Live feed | Start *LicenseCore P1 outage*. 53 alerts (47 LicenseCore + 6 BillingEngine) collapse into one cluster and one incident, acknowledged without a human. About 60 s of replay. |
| 3 | Incidents | INC2001: P1, App Support L2, with the rationale. KB4022 and KB4002 are excluded for low accuracy. Escalated immediately. |
| 4 | Guardrail queue | The auto-resolve is held: P1, Tier 0, KB below 90%, 1,800 users, plus the month-end freeze when the local date is the 28th to the 5th. Press *Approve and run*; the runbook executes and surge mode ends. |
| 5 | Self-service | Surge mode: *Password reset* completes in under a second. *Admin access to BillingEngine* is held for approval. *Former employee* is rejected. |
| 5b | Live feed | Start *Self-service portal: two requests* (reset the database first). The password reset is low risk: the Guardrail approves it and the agent completes it from KB4001. The admin-access request is high risk and waits on the Guardrail queue. The Audit trail shows one trace per request. |
| 6 | Knowledge base | KB4022 scores 54%. Open the drafted update, review the diff, *Approve and publish*: 54% to 97%, and triage can now recommend it. Then approve the KB4016 update: the held P3 incident INC2002 re-triages and the runbook runs without a person (outside the 28th-5th freeze). |
| 7 | Controls | Turn the kill switch on, then run *Self-service* again: even a password reset now waits for approval. |
| 8 | KPIs | Before/after, labelled measured or modelled, with assumptions and unmet targets shown. |

Replay a scenario again after *Controls → Reset database*. *Incidents → Fast-forward 30 min* shows SLA-risk escalation at 50% of the window.

## Tests
```bash
cd backend && .venv/bin/python -m pytest        # about 195 tests: unit, integration (API + full replay), BDD for demo items D1-D7
cd frontend && npm run typecheck && npm run build
```
Tests run in `LLM_MODE=rules` with a fake SDK client for the live path, so they need no network or key.

## Known limits (see [implementation-plan.md](docs/planning/implementation-plan.md) §1 and §13)
* Mock integrations only; no authentication, single worker, not load-tested.
* The spreadsheets are random dummy data. KPI baselines are the case-study figures; each "after" value is labelled measured or modelled.
* The 15 triage golden labels (`backend/data/golden/triage_labels.csv`) were written by the prototype team and should be reviewed by a service desk lead.
* Simulation time follows the local clock in `Asia/Manila` (`sim` in `backend/config/settings.yaml`); scenarios start from "now" and the historic seed data is shifted so record ages match. Set `NEXA_SIM_START=2025-08-04T08:55:00` to replay on the case calendar. Tests and the KPI simulation always use the case calendar.
* The MVP requirement check, KPI results and open gaps are in [docs/product/mvp-requirements.md](docs/product/mvp-requirements.md).
* Application tiers, skills matrix and the freeze calendar are assumptions in `backend/config/`.
