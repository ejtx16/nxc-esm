"""KB retrieval and write-back helpers shared by Triage and KB Curation."""
from __future__ import annotations

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app import settings
from app.data import db
from app.data.audit_ctx import audited_tool


@audited_tool
def get(article_id: str) -> dict | None:
    return db.one("SELECT * FROM kb_articles WHERE article_id=?", (article_id,))


def usable(article: dict, threshold: float | None = None) -> bool:
    """KB-4 / IT-4: only articles scored at or above the accuracy threshold are recommended."""
    th = threshold if threshold is not None else settings.cfg()["triage"]["kb_attach_min_accuracy"]
    return article.get("score") is not None and article["score"] >= th and article.get("recommendation") != "archive"


def procedure(article: dict) -> list[str]:
    """The numbered steps under 'Procedure:' in an article body (the solution the SR agent follows)."""
    steps, in_proc = [], False
    for line in (article.get("body") or "").splitlines():
        if line.strip().lower().startswith("procedure"):
            in_proc = True
        elif in_proc and line.strip()[:1].isdigit():
            steps.append(line.strip().split(".", 1)[-1].strip())
    return steps


@audited_tool
def solution_for(text: str, app_id: str | None = None) -> dict | None:
    """Best knowledge document for a request (SR-3): the top usable article if one exists, else the most accurate of
    the close matches, flagged so the agent can say it is below the bar. None when nothing in the KB resembles it."""
    good, bad = search(text, k=3, app_id=app_id)
    a = good[0] if good else max(bad, key=lambda x: x["score"] or 0) if bad else None
    if a is None:
        return None
    return {"article_id": a["article_id"], "title": a["title"], "score": a["score"], "usable": usable(a),
            "similarity": a["similarity"], "steps": procedure(a)}


@audited_tool
def search(text: str, k: int = 3, app_id: str | None = None, min_score: float = 0.12) -> tuple[list[dict], list[dict]]:
    """Return (usable_matches, excluded_matches) ranked by TF-IDF similarity, boosted when the article covers the app.

    Excluded = relevant but below the accuracy threshold (IT-4 / KB-4), kept so the UI can explain why.
    """
    from app.data.db import loads
    arts = db.query("SELECT * FROM kb_articles")
    if not arts or not text.strip():
        return [], []
    corpus = [f"{a['title']} {a['category']} {a['body'] or ''}" for a in arts]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    m = vec.fit_transform(corpus + [text])
    sims = cosine_similarity(m[-1], m[:-1]).ravel()
    ranked = []
    for sim, a in zip(sims, arts):
        bonus = 0.15 if app_id and app_id in loads(a["systems"], []) else 0.0
        if sim >= 0.05 and sim + bonus >= min_score:
            ranked.append((sim + bonus, sim, a))
    ranked.sort(key=lambda x: -x[0])
    good, bad = [], []
    for total, sim, a in ranked:
        a = {**a, "similarity": round(float(sim), 3)}
        (good if usable(a) else bad).append(a)
    return good[:k], bad[:k]
