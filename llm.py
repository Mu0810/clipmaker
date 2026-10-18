"""Cloud LLM via Kimchi Inference (OpenAI-compatible).

Kimchi exposes an OpenAI-style chat completions endpoint, so we just POST with
httpx — no extra SDK. The API key is read from the environment (KIMCHI_API_KEY)
or a small local config file written by the settings panel.

When a key is configured this is the primary engine for clip titles, captions,
hashtags and suggestions; highlights.py falls back to ollama, then a keyword
heuristic, when it isn't.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

import httpx

import paths


def _config_file() -> Path:
    return paths.data_file("llm_config.json")

BASE_URL = "https://llm.kimchi.dev/openai/v1"
DEFAULT_MODEL = "kimi-k2.6"      # strong general model from the Kimchi lineup
# Tried in order when a model is out of credits / unavailable.
MODELS = ["kimi-k2.6", "minimax-m3", "minimax-m2.7", "nemotron-3-ultra-fp4"]


def _config() -> dict:
    if _config_file().exists():
        try:
            return json.loads(_config_file().read_text())
        except Exception:
            return {}
    return {}


# Per-request override (thread-local): when set — e.g. during a job or a
# /suggest call on behalf of a logged-in user — it takes precedence over the
# global config file, so each user's Kimchi key is used in isolation.
_req = threading.local()


class CallCounter:
    """AI engine calls made on this thread while a count_calls() block is open."""

    def __init__(self) -> None:
        self.count = 0


@contextlib.contextmanager
def count_calls():
    """Count each llm.chat() call and each local Ollama generate call made on
    this thread inside the block (Requirement N8.1). Blocks may nest; every
    open counter sees the call."""
    counter = CallCounter()
    stack = getattr(_req, "counters", None)
    if stack is None:
        stack = _req.counters = []
    stack.append(counter)
    try:
        yield counter
    finally:
        stack.remove(counter)


def count_call() -> None:
    """Record one AI engine call against every open count_calls() block."""
    for counter in getattr(_req, "counters", ()):
        counter.count += 1


def last_model() -> str | None:
    """The model that answered this thread's most recent successful chat()."""
    return getattr(_req, "last_model", None)


def set_request_config(cfg: dict | None) -> None:
    _req.cfg = cfg


def clear_request_config() -> None:
    _req.cfg = None


@contextlib.contextmanager
def user_llm(cfg: dict | None):
    """Use a specific user's Kimchi config for the duration of the block."""
    set_request_config(cfg or {})
    try:
        yield
    finally:
        clear_request_config()


def _override() -> dict | None:
    return getattr(_req, "cfg", None)


def api_key() -> str | None:
    ov = _override()
    if ov is not None:                      # per-user mode: no env fallback
        return ov.get("api_key") or None
    # Global/local mode: a UI-saved key wins over the env var.
    return _config().get("api_key") or os.environ.get("KIMCHI_API_KEY") or None


def model() -> str:
    ov = _override()
    if ov is not None:
        return ov.get("model") or DEFAULT_MODEL
    return _config().get("model") or os.environ.get("KIMCHI_MODEL") or DEFAULT_MODEL


def is_configured() -> bool:
    return bool(api_key())


def status() -> dict:
    """For the settings UI. Never returns the key itself."""
    cfg_key = _config().get("api_key")
    key = cfg_key or os.environ.get("KIMCHI_API_KEY")
    masked = None
    if key:
        masked = ("•" * max(0, len(key) - 4)) + key[-4:] if len(key) > 4 else "••••"
    return {
        "configured": bool(key),
        "from_env": bool(key) and not cfg_key,    # effective key came from env
        "model": model(),
        "key_hint": masked,
    }


def status_of(cfg: dict | None) -> dict:
    """Status for an explicit per-user config dict. Never returns the key."""
    cfg = cfg or {}
    key = cfg.get("api_key")
    masked = None
    if key:
        masked = ("•" * max(0, len(key) - 4)) + key[-4:] if len(key) > 4 else "••••"
    return {
        "configured": bool(key),
        "from_env": False,
        "model": cfg.get("model") or DEFAULT_MODEL,
        "key_hint": masked,
    }


def save_config(key: str | None = None, model_id: str | None = None) -> dict:
    cfg = _config()
    if key is not None:
        key = key.strip()
        if key:
            cfg["api_key"] = key
        else:
            cfg.pop("api_key", None)
    if model_id is not None and model_id.strip():
        cfg["model"] = model_id.strip()
    _config_file().write_text(json.dumps(cfg, indent=2))
    return status()


def clear_config() -> dict:
    if _config_file().exists():
        _config_file().unlink()
    return status()


class LLMError(RuntimeError):
    pass


def _complete(key: str, model_id: str, prompt: str, system: str = "",
              temperature: float = 0.7, max_tokens: int = 800,
              timeout: float = 60.0) -> str:
    """Low-level call against an explicit key/model — no global state."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model_id, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens},
            timeout=timeout,
        )
    except Exception as e:
        raise LLMError(f"Kimchi request failed: {e}") from e

    if r.status_code == 401:
        raise LLMError("Kimchi rejected the API key (401). Check it in settings.")
    if r.status_code >= 400:
        raise LLMError(f"Kimchi error {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise LLMError(f"Unexpected Kimchi response: {e}") from e


_last_ok_model: str | None = None


def chat(prompt: str, system: str = "", temperature: float = 0.7,
         max_tokens: int = 800, timeout: float = 60.0) -> str:
    """Single-turn completion. Tries the configured model, then falls back to
    other Kimchi models when one is out of credits — so a single exhausted
    model doesn't break clip processing."""
    global _last_ok_model
    count_call()
    key = api_key()
    if not key:
        raise LLMError("No Kimchi API key configured.")

    primary = _last_ok_model or model()
    order = [primary] + [m for m in MODELS if m != primary]
    last_err: LLMError | None = None
    for m in order:
        try:
            out = _complete(key, m, prompt, system, temperature, max_tokens,
                            timeout)
            _last_ok_model = m       # remember so we skip exhausted models next
            _req.last_model = m
            return out
        except LLMError as e:
            last_err = e
            if "401" in str(e):      # bad key — other models won't help
                raise
            continue
    raise last_err or LLMError("All Kimchi models failed.")


def verify_key(key: str, model_id: str = "") -> dict:
    """Make a tiny request with the given key/model to confirm it works."""
    try:
        _complete(key.strip(), (model_id.strip() or DEFAULT_MODEL),
                  "Reply with the word: ok", max_tokens=5, timeout=20)
        return {"ok": True, "detail": "Key works."}
    except LLMError as e:
        return {"ok": False, "detail": str(e)}


def verify_and_pick(key: str, preferred: str = "") -> dict:
    """Verify the key, preferring `preferred` model but falling back to the
    others when one is out of credits/unavailable. Returns {ok, model, detail}.
    A bad key (401) stops immediately."""
    key = key.strip()
    order = [preferred] if preferred else []
    for m in MODELS:
        if m not in order:
            order.append(m)

    last = "No model available."
    for m in order:
        res = verify_key(key, m)
        if res["ok"]:
            return {"ok": True, "model": m, "detail": f"Connected using {m}."}
        last = res["detail"]
        if "401" in last:           # bad key — trying other models won't help
            break
    return {"ok": False, "model": preferred or DEFAULT_MODEL, "detail": last}
