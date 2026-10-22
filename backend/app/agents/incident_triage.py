"""Incident Triage Agent (IT-1..IT-6): LLM + rules. Classifies, explains, routes, attaches KB, watches SLA risk."""
from __future__ import annotations

import time

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import settings
from app.agents.common import app_facts, log_decision, prop
from app.data import audit_ctx, db
from app.data.audit_ctx import audited_tool
from app.graph.state import OpsState
from app.llm import client as llm, prompts
from app.llm.schemas import TriageDecision
from app.runtime import bus
from app.runtime.clock import clock, parse
from app.tools import kb_store, mock_servicenow as snow, runbooks

AGENT = "incident_triage"
PRIORITIES = ["P1", "P2", "P3", "P4"]


# --- rules ---------------------------------------------------------------------------------------
def guess_app(text: str) -> str | None:
    best, hits_best = None, 0
    for app, kws in settings.load_yaml("apps")["keywords"].items():
        hits = sum(1 for k in kws if k in text.lower())
        if hits > hits_best:
            best, hits_best = app, hits
    return best


def rules_category(text: str) -> tuple[str, float, list[str]]:
    t = text.lower()
    scores: dict[str, list[str]] = {}
    for cat, spec in settings.load_yaml("taxonomy")["categories"].items():
        scores[cat] = [str(k) for k in spec["keywords"] if str(k).lower() in t]
    ranked = sorted(scores.items(), key=lambda kv: -len(kv[1]))
    top, second = ranked[0], ranked[1]
    if not top[1]:
        return "Application Error", 0.4, []
    margin = len(top[1]) - len(second[1])
    return top[0], min(0.9, 0.55 + 0.12 * margin + 0.04 * len(top[1])), top[1]


def rules_priority(text: str, app_id: str | None, cluster: dict | None) -> tuple[str, str]:
    tx = settings.load_yaml("taxonomy")
    tier = snow.app_tier(app_id)
    bump = tx["tier_bump"].get(tier, 0)
    if cluster:
        base = PRIORITIES.index(tx["severity_priority"].get(cluster["max_severity"], "P3"))
        prio = PRIORITIES[max(0, base - bump)]
        return prio, f"{cluster['max_severity']} alerts on Tier {tier} {app_id}"
    t = text.lower()
    for level in PRIORITIES:
        hit = [str(k) for k in tx["priority_keywords"][level] if str(k).lower() in t]
        if hit:
            idx = PRIORITIES.index(level)
            if bump and idx > 1:                               # Tier 0/1 apps raise ticket-only priority, never to P1
                idx -= 1
            return PRIORITIES[idx], f"impact wording '{hit[0]}'" + (f" on Tier {tier} {app_id}" if bump else "")
    return "P3", "no strong impact wording; default"


def open_workload(team: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM incidents WHERE assigned_team=? AND status NOT IN ('Resolved','Closed')",
                     (team,), default=0)


def pick_team(category: str, app_id: str | None, priority: str) -> tuple[str, str]:
    teams = settings.load_yaml("skills")["teams"]
    owner = (snow.app_info(app_id) or {}).get("owner_team")
    best, best_key, why = None, None, ""
    for t in teams:
        score = (3 * (category in t["categories"]) + 2 * (app_id in t["apps"]) + (priority in t["priorities"])
                 + (2 if owner == t["name"] else 0))
        load = open_workload(t["name"]) / max(1, t["capacity"])
        key = (-score, load)
        if best_key is None or key < best_key:
            best, best_key = t["name"], key
            why = f"skills score {score}, workload {open_workload(t['name'])}/{t['capacity']}"
    return best, why


_sim_index: tuple[TfidfVectorizer, object, list[dict]] | None = None
_sim_sig: int = -1


@audited_tool
def similar_incidents(text: str, k: int, exclude: str | None = None) -> list[dict]:
    """Simple TF-IDF similarity over past incidents (no vector DB)."""
    global _sim_index, _sim_sig
    rows = db.query("SELECT incident_id, short_description, category, priority, assigned_team, resolution_notes "
                    "FROM incidents WHERE origin='seed'")
    if not rows:
        return []
    sig = len(rows)
    if _sim_index is None or _sim_sig != sig:
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
        _sim_index = (vec, vec.fit_transform([r["short_description"] for r in rows]), rows)
        _sim_sig = sig
    vec, matrix, rows = _sim_index
    sims = cosine_similarity(vec.transform([text]), matrix).ravel()
    seen, out = set(), []
    for i in sims.argsort()[::-1]:
        r = rows[i]
        if r["incident_id"] == exclude or sims[i] < 0.08 or r["short_description"] in seen:
            continue
        seen.add(r["short_description"])
        out.append({**r, "similarity": round(float(sims[i]), 3)})
        if len(out) >= k:
            break
    return out


# --- decision (pure: used by run() and by the golden-set evaluation) ----------------------------------------
def decide(text: str, *, app_id: str | None = None, cluster: dict | None = None, alert_text: str = "",
           incident_id: str | None = None, allow_llm: bool = True) -> dict:
    cfg = settings.cfg()["triage"]
    full_text = f"{text} {alert_text}".strip()
    app_id = app_id or guess_app(full_text)
    category, cat_conf, hits = rules_category(full_text)
    priority, prio_reason = rules_priority(full_text, app_id, cluster)
    sims = similar_incidents(text, cfg["similar_k"], exclude=incident_id)
    rb = runbooks.match(app_id, full_text)
    kb_good, kb_bad = kb_store.search(full_text, k=2, app_id=app_id)
    team, team_why = pick_team(category, app_id, priority)
    conf = round(min(cat_conf, 0.9), 2)
    auto = bool(rb)
    rationale = (f"Category '{category}' from keywords {hits[:3] or 'none'}; priority {priority} ({prio_reason}); "
                 f"routed to {team} ({team_why}).")
    mode = "rules"

    if allow_llm and llm.is_live():
        teams = settings.load_yaml("skills")["teams"]
        tx = settings.load_yaml("taxonomy")
        ref = ("Taxonomy keywords: " + "; ".join(f"{c}: {', '.join(v['keywords'][:8])}" for c, v in tx["categories"].items())
               + "\nSkills matrix:\n" + "\n".join(
                   f"- {t['name']}: categories={t['categories']}, apps={t['apps']}, priorities={t['priorities']}"
                   for t in teams))
        item = (f"Incident {incident_id or '(new)'}: {text}\nSource alerts: {alert_text or 'none (ticket)'}\n"
                f"Application: {app_id} (Tier {snow.app_tier(app_id)})\n"
                f"Rule-based suggestion: {category} / {priority} / {team}\n"
                "Similar past incidents:\n" + ("\n".join(
                    f"- {s['incident_id']} ({s['similarity']}): {s['short_description']} -> {s['category']}, "
                    f"{s['priority']}, {s['assigned_team']}" for s in sims) or "- none") +
                "\nKB articles meeting the accuracy threshold:\n" + ("\n".join(
                    f"- {a['article_id']}: {a['title']} (accuracy {a['score']:.0f}%)" for a in kb_good) or "- none") +
                "\nMatching runbook: " + (_runbook_line(rb) if rb else "none"))
        res = llm.parse(TriageDecision, agent=AGENT, system=prompts.TRIAGE_SYSTEM, context=ref, prompt=item,
                        effort="medium", max_tokens=4096)
        if res is not None:
            valid_teams = {t["name"] for t in teams}
            category, priority = res.category, res.priority
            team = res.team if res.team in valid_teams else team
            conf = round(max(0.0, min(1.0, res.confidence)), 2)
            auto = bool(rb) and res.auto_resolvable
            good_ids = {a["article_id"] for a in kb_good}
            kb_good = [a for a in kb_good if a["article_id"] in set(res.kb_article_ids) & good_ids] or kb_good
            rationale = res.rationale
            mode = "llm"

    return {"category": category, "priority": priority, "team": team, "confidence": conf, "rationale": rationale,
            "similar": sims, "kb_good": kb_good, "kb_excluded": kb_bad, "runbook": rb, "auto_resolvable": auto,
            "app_id": app_id, "mode": mode}


def _runbook_line(rb: dict) -> str:
    art = kb_store.get(rb["kb_article"]) if rb.get("kb_article") else None
    score = f"{art['score']:.0f}%" if art and art["score"] is not None else "not scored"
    return (f"{rb['id']} - {rb['name']} (steps: {'; '.join(rb['steps'])}; reversible: {rb['reversible']}; "
            f"backed by KB {rb.get('kb_article') or 'none'}, accuracy {score})")


# --- node ------------------------------------------------------------------------------------------
def run(state: OpsState) -> dict:
    ev = state["event"]
    t = ev["type"]
    if t == bus.INCIDENT_SLA_CHECK:
        return _sla_check(state)
    if t == bus.TRIAGE_HOLD:
        return _set_hold(state, True)
    if t == bus.TRIAGE_RELEASE:
        return _set_hold(state, False)
    return _triage(state)


def _triage(state: OpsState) -> dict:
    t0 = time.perf_counter()
    ev = state["event"]
    inc = db.one("SELECT * FROM incidents WHERE incident_id=?", (ev["entity_ref"],))
    if inc is None:
        raise ValueError(f"unknown incident {ev['entity_ref']}")
    cluster = db.one("SELECT * FROM clusters WHERE cluster_id=?", (inc["source_cluster_id"],)) if inc["source_cluster_id"] else None
    alert_text = ""
    if cluster:
        names = db.query("SELECT DISTINCT alert_name FROM alerts WHERE cluster_id=?", (cluster["cluster_id"],))
        alert_text = "; ".join(n["alert_name"] for n in names)
    d = decide(inc["short_description"], app_id=inc["app_id"], cluster=cluster, alert_text=alert_text,
               incident_id=inc["incident_id"])
    iid, prio, app_id = inc["incident_id"], d["priority"], d["app_id"]
    handed = [a for a in map(kb_store.get, ev.get("payload", {}).get("kb_article_ids", [])) if a and kb_store.usable(a)]
    if handed:                                           # the Alert agent's KB pick goes first
        d["kb_good"] = handed + [a for a in d["kb_good"] if a["article_id"] not in {h["article_id"] for h in handed}]
    triage_ms = round((time.perf_counter() - (ev.get("ts_real") or t0)) * 1000, 1)
    facts = app_facts(app_id)
    cfg = settings.cfg()
    held_mode = bool(db.get_setting("triage_hold")) and prio in ("P3", "P4")

    kb_ids = [a["article_id"] for a in d["kb_good"]]
    excluded = [f"{a['article_id']} ({a['score'] if a['score'] is not None else 'unscored'}"
                f"{'%' if a['score'] is not None else ''})" for a in d["kb_excluded"]]
    rationale = d["rationale"]
    if d["similar"]:
        rationale += " Similar past incidents: " + ", ".join(f"{s['incident_id']} ({s['similarity']})" for s in d["similar"]) + "."
    if excluded:
        rationale += f" KB excluded below the {cfg['triage']['kb_attach_min_accuracy']}% accuracy threshold: {', '.join(excluded)}."

    proposals = [prop(AGENT, "classify", iid, f"Classify {iid}: {d['category']} / {prio}", confidence=d["confidence"],
                      params={"category": d["category"], "priority": prio, "confidence": d["confidence"],
                              "rationale": rationale, "similar_ids": [s["incident_id"] for s in d["similar"]],
                              "auto_resolvable": d["auto_resolvable"], "runbook_id": (d["runbook"] or {}).get("id"),
                              "triage_ms": triage_ms, "mode": d["mode"], "app_id": app_id},
                      context={"priority": prio, **facts})]
    if held_mode:
        proposals.append(prop(AGENT, "hold_reassignment", iid, f"Hold {iid} reassignment during P1 surge",
                              context={"priority": prio, **facts}))
    else:
        proposals.append(prop(AGENT, "route", iid, f"Route {iid} to {d['team']}", params={"team": d["team"]},
                              confidence=d["confidence"], context={"priority": prio, **facts}))
    proposals.append(prop(AGENT, "attach_kb", iid, f"Attach {len(kb_ids)} KB article(s) to {iid}",
                          params={"article_ids": kb_ids}, context=facts))
    if prio in ("P1", "P2"):
        proposals.append(prop(AGENT, "escalate", iid, f"Escalate {iid} immediately to {d['team']}",
                              params={"team": d["team"], "reason": f"{prio} on Tier {facts.get('app_tier')} {app_id}"},
                              context={"priority": prio, **facts}))
    if d["runbook"]:                                     # every matching fix goes to the Guardrail, which judges the risk
        rb = d["runbook"]
        kb_art = kb_store.get(rb["kb_article"]) if rb.get("kb_article") else None
        if kb_art and kb_art["score"] is not None and kb_art["score"] < cfg["kb"]["accuracy_threshold"]:
            # The runbook's own article is stale: ask the KB Agent for a refresh so the next run can be trusted.
            bus.publish(bus.KB_REVIEW, rb["kb_article"], {"draft": True,
                                                         "reason": f"runbook {rb['id']} for {iid} needs a trusted article"},
                        run_id=inc["run_id"])
            rationale += (f" Runbook article {rb['kb_article']} scores {kb_art['score']:.0f}% - asked the KB Agent for "
                          "an update; auto-resolution unlocks once it is approved.")
        vouched = d["auto_resolvable"]
        conf = (0.92 if d["mode"] == "rules" else max(d["confidence"], 0.0)) if vouched else min(d["confidence"], 0.5)
        proposals.append(prop(
            AGENT, "resolve", iid, f"Auto-resolve {iid} with runbook {rb['id']} ({rb['name']})"
            + ("" if vouched else " - Claude is not sure it fits, so a person should confirm"),
            confidence=conf,
            params={"runbook_id": rb["id"]},
            context={"priority": prio, "reversible": rb["reversible"], "kb_article": rb["kb_article"],
                     "kb_accuracy": kb_art["score"] if kb_art else None, **facts}))

    signals = ["p1_surge"] if prio == "P1" else []
    pickup = (f"Picked up {iid} from the Alert agent with KB {', '.join(a['article_id'] for a in handed)} attached. "
              if handed else f"Picked up {iid} from the Alert agent. " if inc["source_cluster_id"] else "")
    reasoning = (f"{pickup}{iid} -> {d['category']} / {prio} / {d['team']} (confidence {d['confidence']:.2f}). {rationale}"
                 + ((f" Runbook {d['runbook']['id']} matches - proposing auto-resolution for the Guardrail."
                     if d["auto_resolvable"] else
                     f" Runbook {d['runbook']['id']} might fit but I am not sure, so I am sending it to the Guardrail "
                     "with low confidence and a person will decide.") if d["runbook"] else ""))
    did = log_decision(state, AGENT, iid, reasoning, input_summary=inc["short_description"],
                       confidence=d["confidence"], proposals=proposals, mode=d["mode"], t0=t0)
    _record_golden(inc["short_description"], d, did)
    return {"entity_ref": iid, "proposals": proposals, "signals": signals, "trace": [reasoning]}


_golden: dict[str, dict] | None = None


def _record_golden(description: str | None, d: dict, decision_id: int) -> None:
    """When the incident is in the hand-labelled golden set, score the decision against it (AU-4)."""
    global _golden
    if _golden is None:
        import csv
        with open(settings.GOLDEN_PATH) as fh:
            _golden = {r["description"].strip().lower(): r for r in csv.DictReader(fh)}
    g = _golden.get((description or "").strip().lower())
    if not g:
        return
    audit_ctx.record_accuracy(AGENT, "triage_category", predicted=d["category"], expected=g["category"],
                              source="golden", confidence=d["confidence"], decision_id=decision_id, mode=d["mode"])
    audit_ctx.record_accuracy(AGENT, "triage_priority_within_1", predicted=d["priority"], expected=g["priority"],
                              correct=abs(int(d["priority"][1]) - int(g["priority"][1])) <= 1, source="golden",
                              confidence=d["confidence"], decision_id=decision_id, mode=d["mode"])


def _sla_check(state: OpsState) -> dict:
    t0 = time.perf_counter()
    iid = state["event"]["entity_ref"]
    inc = db.one("SELECT * FROM incidents WHERE incident_id=?", (iid,))
    if inc is None or inc["status"] in ("Resolved", "Closed"):
        return {"trace": [f"{iid}: SLA check skipped (resolved)"]}
    window = inc["sla_hours"] or snow.sla_hours(inc["priority"])
    elapsed = max(0.0, (clock.now() - parse(inc["opened_ts"] or inc["reported_date"])).total_seconds() / 3600)
    frac = elapsed / window if window else 0.0
    db.update("incidents", "incident_id", iid, {"breach_risk": round(frac, 3)})
    frac_limit = settings.cfg()["escalate_at_fraction"]
    proposals = []
    if frac >= frac_limit and not inc["escalated"]:
        proposals.append(prop(AGENT, "escalate", iid, f"Escalate {iid}: {frac:.0%} of {window:g}h SLA elapsed",
                              params={"team": inc["assigned_team"], "reason": f"SLA breach risk {frac:.0%} of window"},
                              context={"priority": inc["priority"], **app_facts(inc["app_id"])}))
    reasoning = (f"{iid} SLA check: {elapsed:.1f}h of {window:g}h elapsed ({frac:.0%}); "
                 + ("escalating before breach." if proposals else "no action needed."))
    log_decision(state, AGENT, iid, reasoning, input_summary="SLA timer", confidence=1.0, proposals=proposals, t0=t0)
    return {"entity_ref": iid, "proposals": proposals, "trace": [reasoning]}


def _set_hold(state: OpsState, hold: bool) -> dict:
    t0 = time.perf_counter()
    db.set_setting("triage_hold", hold)
    msg = ("Holding non-urgent (P3/P4) reassignments while the P1 is active" if hold
           else "Releasing held reassignments - re-triaging held incidents")
    if not hold:
        for r in db.query("SELECT incident_id, run_id FROM incidents WHERE held=1"):
            bus.publish(bus.INCIDENT_PRIORITY_CHANGED, r["incident_id"], {"reason": "hold released"}, run_id=r["run_id"])
    log_decision(state, AGENT, state["event"]["entity_ref"], msg, input_summary=state["event"]["type"],
                 confidence=1.0, t0=t0)
    return {"trace": [msg]}
