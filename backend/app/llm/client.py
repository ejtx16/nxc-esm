"""Thin wrapper over the official `anthropic` SDK (ADR-0001).

* Structured outputs via `client.beta.messages.parse()` with Pydantic models.
* Effort is set explicitly per call (Opus 5.5 defaults to `medium`); thinking is left at its adaptive default.
* Tool choice is never forced; the stable system prompt is cached.
* Server-side refusal fallbacks are enabled (beta `server-side-fallback-2026-07-01`).
* `LLM_MODE=live` is the default; `LLM_MODE=rules` (tests, KPI simulation) makes `parse()` return None so callers use their deterministic path.
* Audit (ADR-0004): every attempt is an `llm` span and an `llm_calls` row linked by trace/span id, with tokens,
  cost, attempt number and the summarized thinking (the raw chain of thought is never returned by the API).
  Retries are done here (SDK `max_retries=0`) so each attempt is visible.
"""
from __future__ import annotations

import os
import time
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from app import settings
from app.data import audit_ctx, db
from app.runtime.clock import clock

T = TypeVar("T", bound=BaseModel)

_client: Any = None
_client_error: str | None = None


class LLMTimeout(Exception):
    """The model call timed out; the orchestrator routes the item to the human queue (OR-4)."""


def mode() -> str:
    override = db.get_setting("llm_mode")
    return (override or settings.env_llm_mode()) if (override or settings.env_llm_mode()) in ("rules", "live") else "rules"


def is_live() -> bool:
    return mode() == "live"


def set_client(client: Any) -> None:
    """Inject a client (tests) or None to reset."""
    global _client, _client_error
    _client, _client_error = client, None


def _get_client() -> Any:
    global _client, _client_error
    if _client is None:
        try:
            headers = {}
            if os.getenv("ANTHROPIC_WORKSPACE_ID"):      # needed when the API key is not workspace-scoped
                headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"].strip()
            _client = anthropic.Anthropic(timeout=60.0, max_retries=0, default_headers=headers or None)
        except Exception as exc:  # noqa: BLE001 - missing credentials etc.
            _client_error = str(exc)
            raise
    return _client


_sleep = time.sleep                                   # patched to a no-op in tests


def _thinking_summary(resp: Any) -> str | None:
    blocks = getattr(resp, "content", None) or []
    text = "\n".join(getattr(b, "thinking", "") or "" for b in blocks if getattr(b, "type", None) == "thinking")
    return text.strip() or None


def _log(agent: str, schema: str, effort: str, started: float, resp: Any = None, error: str | None = None,
         attempt: int = 1) -> dict:
    usage = getattr(resp, "usage", None)
    tokens = {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None),
              "cache_read": getattr(usage, "cache_read_input_tokens", None),
              "cache_write": getattr(usage, "cache_creation_input_tokens", None)}
    link = audit_ctx.ids()
    row = {"ts": clock.now_iso(), "agent": agent, "schema": schema, "effort": effort, **tokens,
           "latency_ms": round((time.perf_counter() - started) * 1000, 1),
           "stop_reason": getattr(resp, "stop_reason", None), "error": error,
           "trace_id": link.get("trace_id"), "span_id": link.get("span_id"), "run_id": link.get("run_id"),
           "event_id": link.get("event_id"), "entity_ref": getattr(audit_ctx.current_span(), "entity_ref", None),
           "model": settings.MODEL, "attempt": attempt, "cost_usd": audit_ctx.cost_usd(*tokens.values()),
           "thinking_summary": _thinking_summary(resp)}
    db.insert("llm_calls", row)
    return row


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APITimeoutError):    # a timeout goes to the human queue at once (OR-4)
        return False
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", 0) >= 500


def parse(schema: type[T], *, agent: str, system: str, context: str, prompt: str, effort: str = "low",
          max_tokens: int = 4096) -> T | None:
    """Return a validated `schema` instance, or None when the caller should use its rules path."""
    if not is_live():
        return None
    llm_cfg = settings.cfg().get("llm") or {}
    attempts = int(llm_cfg.get("max_attempts", 3))
    backoff = float(llm_cfg.get("retry_backoff_s", 0.5))
    for attempt in range(1, attempts + 1):
        with audit_ctx.span("llm", schema.__name__, agent, attempt=attempt, effort=effort,
                            model=settings.MODEL, context=context, prompt=prompt) as sp:
            out, retry = _attempt(schema, sp, agent, system, context, prompt, effort, max_tokens, attempt)
        if not retry or attempt == attempts:
            return out
        _sleep(backoff * 2 ** (attempt - 1))
    return None


def _attempt(schema: type[T], sp: Any, agent: str, system: str, context: str, prompt: str, effort: str,
             max_tokens: int, attempt: int) -> tuple[T | None, bool]:
    """One model call. Returns (result, should_retry) and records the outcome on the span."""
    started = time.perf_counter()
    kwargs: dict[str, Any] = {}
    if audit_ctx.cfg()["capture_thinking_summary"]:  # display only; thinking is billed the same either way
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
    try:
        client = _get_client()
        resp = client.beta.messages.parse(
            model=settings.MODEL,
            max_tokens=max_tokens,
            betas=[settings.FALLBACK_BETA],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [  # the API rejects cache_control on an empty block
                *([{"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}] if context else []),
                {"type": "text", "text": prompt}]}],
            output_format=schema,
            output_config={"effort": effort},
            **kwargs,
        )
    except anthropic.APITimeoutError as exc:
        _log(agent, schema.__name__, effort, started, error=f"timeout: {exc}", attempt=attempt)
        sp.set_status("timeout", str(exc))
        raise LLMTimeout(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API errors, credentials, validation -> retry or rules fallback
        err = f"{type(exc).__name__}: {exc}"
        _log(agent, schema.__name__, effort, started, error=err, attempt=attempt)
        retry = isinstance(exc, anthropic.APIError) and _retryable(exc)
        sp.set_status("error" if retry else "fallback", err)
        return None, retry
    # Always check stop_reason before reading content (safety classifiers can decline).
    if getattr(resp, "stop_reason", None) == "refusal":
        row = _log(agent, schema.__name__, effort, started, resp, error="refusal", attempt=attempt)
        sp.set_status("refusal", "refusal")
        sp.set_output(_span_tokens(row))
        return None, False
    out = getattr(resp, "parsed_output", None)
    row = _log(agent, schema.__name__, effort, started, resp,
               error=None if out is not None else "no_parsed_output", attempt=attempt)
    if out is None:
        sp.set_status("fallback", "no_parsed_output")
    sp.set_output({**_span_tokens(row), "result": out.model_dump() if out is not None else None})
    return out, False


def _span_tokens(row: dict) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read", "cache_write", "cost_usd", "stop_reason", "thinking_summary")
    return {k: row[k] for k in keys if row.get(k) is not None}


def health_check() -> dict:
    """Used by Controls to verify credentials without touching business data."""
    info: dict[str, Any] = {"mode": mode(), "model": settings.MODEL}
    try:
        client = _get_client()
        resp = client.messages.create(model=settings.MODEL, max_tokens=64, output_config={"effort": "low"},
                                      messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        info.update(ok=True, stop_reason=resp.stop_reason,
                    text="".join(b.text for b in resp.content if b.type == "text")[:40])
    except Exception as exc:  # noqa: BLE001
        info.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    return info
