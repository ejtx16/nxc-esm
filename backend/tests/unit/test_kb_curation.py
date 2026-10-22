from app import settings
from app.agents import kb_curation
from app.data import db
from app.runtime import approvals, bus, engine


def art(aid):
    return db.one("SELECT * FROM kb_articles WHERE article_id=?", (aid,))


def test_all_30_articles_scored_on_seed(fresh):
    rows = db.query("SELECT * FROM kb_articles")
    assert len(rows) == 30 and all(r["score"] is not None and r["recommendation"] for r in rows)


def test_formula_components(fresh):
    a = art("KB4005")
    s = kb_curation.score_article(a)
    p = s["parts"]
    k = settings.cfg()["kb"]
    days = p["days_since_update"]
    assert p["freshness"] == round(min(k["freshness_cap"], max(0, days - k["fresh_days"]) * k["freshness_per_day"]), 1)
    ratio = a["escalation_despite_kb"] / a["usage_count_90days"]
    assert p["escalation"] == round(min(k["escalation_cap"], 100 * ratio * k["escalation_weight"]), 1)
    assert s["score"] == round(100 - p["freshness"] - p["escalation"] - p["deprecated"], 1)


def test_deprecated_references_are_penalised(fresh):
    a = art("KB4022")
    assert kb_curation.deprecated_refs(a["body"])
    clean = {**a, "body": "Use the LicenseCore v3 operations console."}
    assert kb_curation.score_article(a)["score"] < kb_curation.score_article(clean)["score"]


def test_ignores_outdated_flag_and_seed_recommendation(fresh):
    rows = db.query("SELECT * FROM kb_articles")
    assert all(r["outdated_flag"] == "Yes" for r in rows)                    # every article is flagged in the dummy data
    assert any(r["recommendation"] != "update" for r in rows)                # ...but the formula discriminates
    assert sum(r["recommendation"] != r["recommended_action_seed"].lower() for r in rows) >= 5


def test_duplicate_title_archives_the_older_article(fresh):
    assert art("KB4001")["recommendation"] == "archive" and art("KB4001")["superseded_by"] == "KB4021"


def test_below_threshold_articles_are_excluded_from_triage_recommendations(fresh):
    from app.tools import kb_store
    assert not kb_store.usable(art("KB4002"))
    good, bad = kb_store.search("LicenseCore slow response health check", k=5, app_id="LicenseCore")
    assert all(a["score"] >= 90 for a in good)
    assert any(a["article_id"] in ("KB4022", "KB4002") for a in bad)


def test_draft_replaces_deprecated_paths_and_needs_human_approval(fresh):
    bus.publish(bus.KB_REVIEW, "KB4022", {"draft": True, "use_llm": False})
    engine.process_pending()
    d = db.one("SELECT * FROM kb_drafts WHERE article_id='KB4022'")
    assert d and d["status"] == "pending"
    assert not kb_curation.deprecated_refs(d["new_body"])
    ap = db.one("SELECT * FROM approvals WHERE action='update_kb_article'")
    assert ap["status"] == "pending" and ap["verdict"] == "RequireApproval"
    assert art("KB4022")["updated_by_agent"] == 0                      # nothing published yet


def test_approving_a_draft_updates_and_rescores_upwards(fresh):
    before = art("KB4022")["score"]
    bus.publish(bus.KB_REVIEW, "KB4022", {"draft": True, "use_llm": False})
    engine.process_pending()
    ap = db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article'")
    res = approvals.decide(ap["approval_id"], True, "km")
    assert res["ok"]
    after = art("KB4022")
    assert after["score"] > before and after["score"] >= 90 and after["updated_by_agent"] == 1
    assert after["recommendation"] == "keep"
    assert db.one("SELECT status FROM kb_drafts WHERE article_id='KB4022'")["status"] == "approved"


def test_rejecting_a_draft_leaves_article_untouched(fresh):
    before = art("KB4022")
    bus.publish(bus.KB_REVIEW, "KB4022", {"draft": True, "use_llm": False})
    engine.process_pending()
    ap = db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article'")
    approvals.decide(ap["approval_id"], False, "km")
    assert art("KB4022")["body"] == before["body"]
    assert db.one("SELECT status FROM kb_drafts WHERE article_id='KB4022'")["status"] == "rejected"


def test_no_duplicate_pending_draft(fresh):
    for _ in range(2):
        bus.publish(bus.KB_REVIEW, "KB4022", {"draft": True, "use_llm": False})
        engine.process_pending()
    assert db.scalar("SELECT COUNT(*) FROM kb_drafts WHERE article_id='KB4022'") == 1


def test_updated_article_becomes_recommendable_to_triage(fresh):
    from app.tools import kb_store
    bus.publish(bus.KB_REVIEW, "KB4022", {"draft": True, "use_llm": False})
    engine.process_pending()
    approvals.decide(db.one("SELECT approval_id FROM approvals WHERE action='update_kb_article'")["approval_id"], True, "km")
    good, _ = kb_store.search("LicenseCore slow response health check failed", k=3, app_id="LicenseCore")
    assert "KB4022" in [a["article_id"] for a in good]
