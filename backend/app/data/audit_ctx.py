"""Audit trail context: traces and spans (ADR-0004, DR-0009).

One *trace* per graph invocation (event) or operator action. Every node, LLM call, tool call, guardrail check,
execution and human decision inside it is a *span*. A context variable carries the active trace and span, so
LLM and tool calls deep inside agents link themselves without passing ids around.

Covers the six audit dimensions: iteration (`iteration`, `attempt`), tools (`span_type='tool'`), tokens
(`llm_calls`, linked by `trace_id`/`span_id`), accuracy (`accuracy_results`), activity (spans + decisions) and
latency (`latency_ms` on every span).
"""
from __future__ import annotations

import functools
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

from app import settings
from app.data import db
from app.runtime.clock import clock

F = TypeVar("F", bound=Callable[..., Any])

DEFAULTS = {"max_payload_chars": 2000, "log_tool_reads": True, "capture_thinking_summary": True,
            "pricing": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def cfg() -> dict:
    return {**DEFAULTS, **(settings.cfg().get("audit") or {})}


@dataclass
class Trace:
    trace_id: str
    run_id: str | None = None
    event_id: int | None = None
    entity_ref: str | None = None
    steps: int = 0                      # node counter -> span.iteration


@dataclass
class Span:
    trace: Trace
    span_type: str
    name: str
    agent: str | None
    parent_span_id: str | None
    iteration: int
    attempt: int | None = None
    entity_ref: str | None = None
    input: dict = field(default_factory=dict)
    output: Any = None
    status: str = "ok"
    error: str | None = None
    decision_id: int | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_sim: str = field(default_factory=clock.now_iso)
    started_real: float = field(default_factory=time.perf_counter)

    def set_output(self, output: Any) -> None:
        self.output = output

    def set_status(self, status: str, error: str | None = None) -> None:
        self.status, self.error = status, error

    def link_decision(self, decision_id: int) -> None:
        if self.decision_id is None:            # first decision written inside the span is the span's decision
            self.decision_id = decision_id


_trace: ContextVar[Trace | None] = ContextVar("audit_trace", default=None)
_span: ContextVar[Span | None] = ContextVar("audit_span", default=None)


def redact(obj: Any) -> str | None:
    """JSON-encode, mask e-mail addresses and truncate to `audit.max_payload_chars`."""
    if obj is None:
        return None
    text = _EMAIL.sub("***@***", obj if isinstance(obj, str) else db.dumps(obj))
    limit = int(cfg()["max_payload_chars"])
    return text if len(text) <= limit else text[:limit] + f"...[+{len(text) - limit} chars]"


def new_trace_id(event_id: int | None = None) -> str:
    return f"TR-{event_id if event_id is not None else 'op'}-{uuid.uuid4().hex[:8]}"


@contextmanager
def start_trace(*, event_id: int | None = None, run_id: str | None = None,
                entity_ref: str | None = None) -> Iterator[Trace]:
    tr = Trace(new_trace_id(event_id), run_id, event_id, entity_ref)
    t_tok, s_tok = _trace.set(tr), _span.set(None)
    try:
        yield tr
    finally:
        _span.reset(s_tok)
        _trace.reset(t_tok)


def current_trace() -> Trace | None:
    return _trace.get()


def current_span() -> Span | None:
    return _span.get()


def ids() -> dict[str, Any]:
    """Linking columns for rows written inside the active span (empty outside a trace)."""
    tr, sp = _trace.get(), _span.get()
    if tr is None:
        return {}
    return {"trace_id": tr.trace_id, "span_id": sp.span_id if sp else None,
            "iteration": sp.iteration if sp else None, "run_id": tr.run_id, "event_id": tr.event_id}


@contextmanager
def span(span_type: str, name: str, agent: str | None = None, *, entity_ref: str | None = None,
         attempt: int | None = None, **inputs: Any) -> Iterator[Span]:
    """Record one unit of agent work. Node spans advance the trace's step counter; child spans inherit it.

    Outside a trace (e.g. an operator approving from the UI) a one-span trace is created on the fly.
    """
    tr = _trace.get()
    own = None
    if tr is None:
        own = start_trace(entity_ref=entity_ref)
        tr = own.__enter__()
    parent = _span.get()
    if span_type == "node":
        tr.steps += 1
        iteration = tr.steps
    else:
        iteration = parent.iteration if parent else tr.steps
    sp = Span(tr, span_type, name, agent or (parent.agent if parent else None),
              parent.span_id if parent else None, iteration, attempt,
              entity_ref or (parent.entity_ref if parent else None) or tr.entity_ref, inputs)
    tok = _span.set(sp)
    try:
        yield sp
    except Exception as exc:
        if sp.status == "ok":                       # keep a more specific status (e.g. timeout) set by the body
            sp.set_status("error", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _span.reset(tok)
        _write(sp)
        if own is not None:
            own.__exit__(None, None, None)


def _write(sp: Span) -> None:
    db.insert("audit_spans", {
        "span_id": sp.span_id, "trace_id": sp.trace.trace_id, "parent_span_id": sp.parent_span_id,
        "run_id": sp.trace.run_id, "event_id": sp.trace.event_id, "entity_ref": sp.entity_ref, "agent": sp.agent,
        "span_type": sp.span_type, "name": sp.name, "iteration": sp.iteration, "attempt": sp.attempt,
        "status": sp.status, "error": sp.error, "decision_id": sp.decision_id, "started_sim": sp.started_sim,
        "started_real": sp.started_real, "latency_ms": round((time.perf_counter() - sp.started_real) * 1000, 3),
        "input_json": redact(sp.input or None), "output_json": redact(sp.output)})


def audited_tool(fn: F) -> F:
    """Log a read-only tool call as a `tool` span when it runs inside a trace (and reads are enabled)."""
    name = f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__name__}"

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _trace.get() is None or not cfg()["log_tool_reads"]:
            return fn(*args, **kwargs)
        with span("tool", name, args=list(args), kwargs=kwargs, side_effect=False) as sp:
            out = fn(*args, **kwargs)
            sp.set_output(_summarise(out))
            return out
    return wrapper  # type: ignore[return-value]


def _summarise(out: Any) -> Any:
    if isinstance(out, (list, tuple)):
        return {"count": len(out), "first": out[0] if out else None}
    return out


def cost_usd(input_tokens: int | None, output_tokens: int | None, cache_read: int | None,
             cache_write: int | None) -> float | None:
    if input_tokens is None and output_tokens is None:
        return None
    p = cfg()["pricing"]
    return round(((input_tokens or 0) * p["input"] + (output_tokens or 0) * p["output"]
                  + (cache_read or 0) * p["cache_read"] + (cache_write or 0) * p["cache_write"]) / 1_000_000, 6)


def record_accuracy(agent: str, metric: str, *, predicted: Any, expected: Any, source: str,
                    correct: bool | None = None, score: float | None = None, confidence: float | None = None,
                    decision_id: int | None = None, scope: str = "decision", mode: str | None = None,
                    run_id: str | None = None) -> int:
    link = ids()
    if correct is None and score is None:
        correct = str(predicted) == str(expected)
    return db.execute(
        """INSERT INTO accuracy_results (trace_id, decision_id, run_id, agent, scope, metric, predicted, expected,
           correct, score, confidence, source, mode, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (link.get("trace_id"), decision_id, run_id or link.get("run_id"), agent, scope, metric,
         None if predicted is None else str(predicted), None if expected is None else str(expected),
         None if correct is None else int(bool(correct)), score, confidence, source, mode, clock.now_iso()))
