# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ESM Agentic Ops: a mock-data multi-agent prototype (FastAPI + LangGraph backend, React/Vite/Chakra console). Docs live in `docs/`, grouped by category with kebab-case names (index: `docs/README.md`): `product/` (PRD, business problem, MVP check), `architecture/`, `adr/`, `design/`, `planning/implementation-plan.md`, `guides/` (simulation guide), `validation/`, `reference/`. `README.md` has the demo script and known limits. `docs/validation/data/alert-log.json` is loaded by code (`app/data/scenarios.py`).

## Commands

Backend (Python 3.10+, run from `backend/`):
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env                                   # optional ANTHROPIC_API_KEY for live mode
.venv/bin/uvicorn app.api.main:app --port 8000         # first run builds data/esm.db from ../files/*.xlsx
.venv/bin/python -m pytest                             # full suite (unit, integration, pytest-bdd)
.venv/bin/python -m pytest tests/unit/test_x.py::test_name   # single test
```

Frontend (from `frontend/`): `npm install`, `npm run dev` (:5173, proxies `/api` to :8000), `npm run typecheck`, `npm run build` (output in `frontend/dist`, which the API serves itself). There is no linter configured.

## Architecture

- **Event-driven LangGraph orchestrator** (`app/graph/`): every event (`app/runtime/bus.py`) enters `route_event`, which maps event type -> one of four agents (`ROUTES` in `graph/nodes.py`): alert_correlation, incident_triage, sr_fulfilment, kb_curation. `SR_SURGE_OFF` goes to `p1_surge`. Agents never act directly: they emit `ActionProposal`s that always pass through the **guardrail** node, then `execute` (mock tools in `app/tools/`), or `human_queue` for approval, then `finalize`. Adding an event type means updating `ROUTES` and the bus.
- **Guardrail is deterministic, even in live LLM mode** (`app/agents/guardrail.py`, policy in `config/guardrail_policy.yaml`). Verdicts are Approve / RequireApproval / Block; a kill switch forces everything to approval. Held items are resolved via `app/runtime/approvals.py`.
- **LLM modes** (`app/llm/`): `live` (default; Claude structured outputs for every agent decision) vs `rules` (each agent's deterministic path, also the fallback on API errors). Tests run in `rules` with a fake SDK client, so no network or key is needed. Keep the rules path working when changing an agent.
- **Runtime** (`app/runtime/`): `engine.py` processes events, `player.py` replays scenarios (`app/data/scenarios.py`), `clock.py` is a simulated clock (defaults to the local clock in `Asia/Manila`; set `ESM_SIM_START` to use the case calendar; tests and `app/kpi` always use the case calendar). Time-dependent policy (month-end freeze, SLA windows) depends on this clock.
- **Data** (`app/data/`): SQLite at `backend/data/esm.db`, seeded from the spreadsheets in `files/` (read-only dummy data). All decisions are written to an audit log (`audit.py`, `audit_ctx.py`). Controls page / API can reset the DB; replaying a scenario needs a reset first.
- **Config** (`backend/config/`): `apps.yaml` (tiers), `entitlements.yaml`, `guardrail_policy.yaml`, plus `settings.yaml`; these are assumptions that drive behavior, so check them before changing agent logic.
- **API** (`app/api/routes/`) feeds the frontend pages in `frontend/src/pages/` (polling hooks in `frontend/src/api/`).
- **KPIs** (`app/kpi/`): baselines are the case-study figures; "after" values are labelled measured vs modelled. Preserve that labelling.

## Tests

`backend/tests/` has `unit/`, `integration/` (API plus full scenario replay), and `features/` + `step_defs/` (BDD for demo items D1-D7). Golden triage labels are in `backend/data/golden/triage_labels.csv`.
