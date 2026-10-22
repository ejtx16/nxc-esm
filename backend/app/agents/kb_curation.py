"""KB Curation Agent (KB-1..KB-4): formula-based scoring + LLM judgment near the threshold + drafted updates."""
from __future__ import annotations

import re
import time
from datetime import date

from app import settings
from app.agents.common import log_decision, prop
from app.data import audit_ctx, db
from app.graph.state import OpsState
from app.llm import client as llm, prompts
from app.llm.schemas import KBAssessment, KBDraft
from app.runtime import bus
from app.runtime.clock import clock, parse
from app.tools import runbooks

AGENT = "kb_curation"


def deprecated_refs(body: str) -> list[str]:
    paths = settings.cfg()["kb"]["deprecated_paths"]
    return [p for p in paths if p.lower() in (body or "").lower()]


def score_article(a: dict) -> dict:
    """accuracy = 100 - freshness_penalty - escalation_penalty - deprecated_ref_penalty (docs/planning/implementation-plan.md §7.4)."""
    k = settings.cfg()["kb"]
    today = clock.now().date()
    days = (today - date.fromisoformat(str(a["last_updated"])[:10])).days
    fresh = min(k["freshness_cap"], max(0, days - k["fresh_days"]) * k["freshness_per_day"])
    usage = max(1, a["usage_count_90days"] or 0)
    ratio = (a["escalation_despite_kb"] or 0) / usage
    factor = k["post_update_escalation_factor"] if a.get("updated_by_agent") else 1.0
    esc = min(k["escalation_cap"], 100 * ratio * k["escalation_weight"] * factor)
    refs = deprecated_refs(a["body"])
    dep = min(k["deprecated_cap"], k["deprecated_each"] * len(refs))
    score = round(max(0.0, 100 - fresh - esc - dep), 1)
    issues = []
    if fresh > 0:
        issues.append(f"stale: last updated {days} days ago")
    if ratio >= 0.1:
        issues.append(f"{a['escalation_despite_kb']} of {a['usage_count_90days']} uses still escalated ({ratio:.0%})")
    if refs:
        issues.append("references deprecated: " + ", ".join(refs))
    return {"score": score, "parts": {"freshness": round(fresh, 1), "escalation": round(esc, 1), "deprecated": round(dep, 1),
                                      "days_since_update": days, "escalation_ratio": round(ratio, 3)},
            "issues": issues, "deprecated": refs}


def recommend(a: dict, score: float, dup_of: str | None) -> tuple[str, str]:
    k = settings.cfg()["kb"]
    if dup_of:
        return "archive", f"duplicate title; superseded by {dup_of} (more recently updated)"
    if score >= k["accuracy_threshold"]:
        return "keep", "meets the accuracy threshold"
    if score < k["archive_below"] and (a["usage_count_90days"] or 0) <= k["archive_max_usage"]:
        return "archive", f"low score ({score:.0f}%) and rarely used ({a['usage_count_90days']} uses in 90 days)"
    return "update", f"score {score:.0f}% is below {k['accuracy_threshold']}%"


def _duplicate_of(a: dict, all_articles: list[dict]) -> str | None:
    newer = [b for b in all_articles if b["title"] == a["title"] and b["article_id"] != a["article_id"]
             and str(b["last_updated"]) > str(a["last_updated"])]
    return max(newer, key=lambda b: str(b["last_updated"]))["article_id"] if newer else None


def rescore(article_id: str, live: bool = True) -> dict:
    a = db.one("SELECT * FROM kb_articles WHERE article_id=?", (article_id,))
    allarts = db.query("SELECT article_id, title, last_updated FROM kb_articles")
    s = score_article(a)
    dup = _duplicate_of(a, allarts)
    action, why = recommend(a, s["score"], dup)
    mode = "rules"
    issues, rationale = s["issues"], why
    if live and llm.is_live():
        res = llm.parse(KBAssessment, agent=AGENT, system=prompts.KB_ASSESS_SYSTEM, context="",
                        prompt=(f"Article {a['article_id']}: {a['title']} ({a['category']})\nFormula score: {s['score']}\n"
                                f"Signals: {s['issues']}\nSuperseded by newer duplicate: {dup or 'none'}\nBody:\n{a['body']}"), effort="medium", max_tokens=2048)
        if res is not None:
            action, issues, rationale, mode = res.recommended_action, list(dict.fromkeys(issues + res.issues)), res.rationale, "llm"
    db.update("kb_articles", "article_id", article_id, {
        "score": s["score"], "recommendation": action, "issues": db.dumps(issues), "rationale": rationale,
        "scored_ts": clock.now_iso(), "score_parts": db.dumps(s["parts"]), "assessment_mode": mode, "superseded_by": dup})
    return {"article_id": article_id, "score": s["score"], "recommendation": action, "issues": issues,
            "rationale": rationale, "mode": mode}


def run(state: OpsState) -> dict:
    t = state["event"]["type"]
    if t == bus.KB_VALIDATE_RELATED:
        return _validate_related(state)
    return _review(state)


def _review(state: OpsState) -> dict:
    ev = state["event"]
    payload = ev.get("payload", {})
    ids = ([r["article_id"] for r in db.query("SELECT article_id FROM kb_articles ORDER BY article_id")]
           if ev["entity_ref"] == "ALL" else [ev["entity_ref"]])
    proposals, trace, results = [], [], []
    for aid in ids:
        if bus.cancel.is_set():
            break
        t0 = time.perf_counter()
        r = rescore(aid, live=not payload.get("rules"))
        results.append(r)
        msg = f"{aid}: {r['score']:.0f}% -> {r['recommendation']} ({r['rationale']})"
        new = _make_draft(state, aid) if payload.get("draft") and r["recommendation"] == "update" else []
        log_decision(state, AGENT, aid, msg + (" Drafted an update for approval." if new else ""),
                     input_summary="scheduled KB review", confidence=1.0, proposals=new, mode=r["mode"], t0=t0)
        proposals += new
    scores = [r["score"] for r in results] or [0.0]
    below = sum(1 for r in results if r["score"] < settings.cfg()["kb"]["accuracy_threshold"])
    trace.append(f"KB review: scored {len(results)} article(s), mean {sum(scores) / len(scores):.1f}%, {below} below threshold "
                 f"(excluded from triage recommendations)")
    return {"entity_ref": ev["entity_ref"], "proposals": proposals, "trace": trace}


def _validate_related(state: OpsState) -> dict:
    """OR-2: on a P1, re-validate every article tied to the affected system and draft fixes for stale ones."""
    ev = state["event"]
    system = ev["payload"].get("system") or ev["entity_ref"]
    related = {a["article_id"] for a in db.query("SELECT article_id, systems FROM kb_articles")
               if system in db.loads(a["systems"], [])}
    related |= {rb["kb_article"] for rb in runbooks.RUNBOOKS.values() if system in rb["systems"] and rb.get("kb_article")}
    proposals, lines = [], []
    for aid in sorted(related):
        t0 = time.perf_counter()
        r = rescore(aid)
        drafted, new = "", []
        if r["recommendation"] not in ("keep", "archive"):
            new = _make_draft(state, aid)
            drafted = " - drafted an update for approval" if new else " - draft already pending"
        msg = f"{aid}: {r['score']:.0f}% -> {r['recommendation']}{drafted}"
        lines.append(msg)
        log_decision(state, AGENT, aid, f"P1 validation for {system}: {msg}. {'; '.join(r['issues'])}",
                     input_summary=f"kb.validate_related {system}", confidence=1.0, proposals=new, mode=r["mode"], t0=t0)
        proposals += new
    trace = [f"KB validated {len(related)} article(s) related to {system}: " + " | ".join(lines)]
    return {"entity_ref": system, "proposals": proposals, "trace": trace}


# --- drafts (KB-3) -----------------------------------------------------------------------------------
def _evidence(a: dict) -> list[str]:
    steps: list[str] = []
    for rid, rb in runbooks.RUNBOOKS.items():
        if rb.get("kb_article") == a["article_id"]:
            steps += [f"{s} (validated in {rid})" for s in rb["steps"]]
    systems = db.loads(a["systems"], [])
    notes = db.query("SELECT DISTINCT resolution_notes FROM incidents WHERE status='Resolved' AND origin!='seed' "
                     "AND resolution_notes IS NOT NULL ORDER BY incident_id DESC LIMIT 5")
    for n in notes:
        if any(s in (n["resolution_notes"] or "") for s in systems):
            steps.append(n["resolution_notes"])
    return steps


def rules_draft_body(a: dict, evidence: list[str]) -> tuple[str, str]:
    body = a["body"] or f"# {a['title']}"
    replaced = []
    for old, new in settings.cfg()["kb"]["deprecated_paths"].items():
        pat = re.compile(re.escape(old), re.IGNORECASE)
        if pat.search(body):
            body = pat.sub(new, body)
            replaced.append(old)
    today = clock.now().date().isoformat()
    body += f"\n\nReviewed {today} by the KB Curation Agent."
    if evidence:
        body += "\nValidated resolution steps:\n" + "\n".join(f"- {e}" for e in evidence)
    summary = (f"Replaced {len(replaced)} deprecated reference(s) ({', '.join(replaced) or 'none'}); "
               f"added {len(evidence)} validated step(s); refreshed review date.")
    return body, summary


def _make_draft(state: OpsState, article_id: str) -> list:
    """Create a pending draft + an update_kb_article proposal (always human-approved)."""
    if db.scalar("SELECT COUNT(*) FROM kb_drafts WHERE article_id=? AND status='pending'", (article_id,), default=0):
        return []
    a = db.one("SELECT * FROM kb_articles WHERE article_id=?", (article_id,))
    evidence = _evidence(a)
    body, summary = rules_draft_body(a, evidence)
    mode = "rules"
    if llm.is_live():
        res = llm.parse(KBDraft, agent=AGENT, system=prompts.KB_DRAFT_SYSTEM,
                        context="Deprecated -> current: " + "; ".join(f"{k} -> {v}" for k, v in settings.cfg()["kb"]["deprecated_paths"].items()),
                        prompt=(f"Article {a['article_id']}: {a['title']}\nCurrent body:\n{a['body']}\n"
                                f"Validated resolution evidence:\n" + ("\n".join(evidence) or "none")),
                        effort="medium", max_tokens=3000)
        if res is not None and not deprecated_refs("\n".join(res.updated_steps)):
            body = (f"# {a['title']}\nCategory: {a['category']}\nOwner: {a['author']}\n\nProcedure:\n" +
                    "\n".join(f"{i}. {s}" for i, s in enumerate(res.updated_steps, 1)) +
                    f"\n\nReviewed {clock.now().date().isoformat()} by the KB Curation Agent.")
            summary, mode = res.change_summary, "llm"
    did = db.execute("INSERT INTO kb_drafts (article_id, new_body, change_summary, sources, status, created_ts, mode) "
                     "VALUES (?,?,?,?,?,?,?)", (article_id, body, summary, db.dumps(evidence), "pending", clock.now_iso(), mode))
    return [prop(AGENT, "update_kb_article", article_id, f"Publish drafted update to {article_id}: {summary}",
                 confidence=0.9, params={"draft_id": did}, context={"kb_article": article_id})]


def request_draft(article_id: str) -> int:
    bus.publish(bus.KB_REVIEW, article_id, {"draft": True})
    return 0


def apply_draft(draft_id: int, actor: str) -> dict:
    d = db.one("SELECT * FROM kb_drafts WHERE draft_id=?", (draft_id,))
    if d is None or d["status"] != "pending":
        return {"ok": False, "message": f"draft {draft_id} is not pending"}
    before = db.one("SELECT score FROM kb_articles WHERE article_id=?", (d["article_id"],))["score"]
    db.update("kb_articles", "article_id", d["article_id"], {
        "body": d["new_body"], "last_updated": clock.now().date().isoformat(), "updated_by_agent": 1})
    r = rescore(d["article_id"])
    db.update("kb_drafts", "draft_id", draft_id, {"status": "approved", "decided_ts": clock.now_iso()})
    released = _release_blocked_incidents(d["article_id"], r["score"])
    audit_ctx.record_accuracy(AGENT, "kb_accuracy_after_update", predicted=round(before or 0, 1),
                              expected=round(r["score"], 1), score=r["score"], source="kb_rescore",
                              correct=r["score"] >= settings.cfg()["kb"]["accuracy_threshold"], mode=d["mode"])
    return {"ok": True, "message": f"{d['article_id']} updated by {actor}: score {before:.0f}% -> {r['score']:.0f}%",
            "before": before, "after": r["score"], "recommendation": r["recommendation"],
            "retriaged": released}


def _release_blocked_incidents(article_id: str, score: float) -> list[str]:
    """OR-2/OR-3: a refreshed runbook article can unlock auto-resolution, so open incidents using it are re-triaged.
    Their old approval is superseded; the Guardrail decides again on the fresh facts (it may still require a human)."""
    if score < settings.cfg()["kb"]["accuracy_threshold"]:
        return []
    rb_ids = [rid for rid, rb in runbooks.RUNBOOKS.items() if rb.get("kb_article") == article_id]
    if not rb_ids:
        return []
    marks = ",".join("?" * len(rb_ids))
    from app.agents import guardrail
    allowed = guardrail.policy()["approve_only_if"]["allowed_priorities"]      # P1/P2 stay with a human whatever the KB says
    rows = db.query(f"SELECT incident_id, run_id FROM incidents WHERE origin!='seed' AND auto_resolvable=1 "
                    f"AND status NOT IN ('Resolved','Closed') AND runbook_id IN ({marks}) "
                    f"AND priority IN ({','.join('?' * len(allowed))})", (*rb_ids, *allowed))
    for r in rows:
        db.execute("UPDATE approvals SET status='superseded', decided_ts=?, note=? WHERE entity_ref=? AND action='resolve' "
                   "AND status='pending'", (clock.now_iso(), f"{article_id} refreshed to {score:.0f}%", r["incident_id"]))
        bus.publish(bus.INCIDENT_PRIORITY_CHANGED, r["incident_id"], {"reason": f"{article_id} refreshed to {score:.0f}%"},
                    run_id=r["run_id"])
    return [r["incident_id"] for r in rows]


def reject_draft(draft_id: int) -> None:
    db.update("kb_drafts", "draft_id", draft_id, {"status": "rejected", "decided_ts": clock.now_iso()})
