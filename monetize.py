"""Monetization kit: a reusable CTA / affiliate footer, default hashtags, and a
pinned-comment suggestion that get applied to every clip's description.

This is how a creator turns views into money — every description carries the
same call-to-action and links (affiliate, channel, products) without retyping.
Stored locally in monetize_config.json (gitignored).
"""
from __future__ import annotations

import json
from pathlib import Path

import paths


def _config_file() -> Path:
    return paths.data_file("monetize_config.json")


def _config() -> dict:
    if _config_file().exists():
        try:
            return json.loads(_config_file().read_text())
        except Exception:
            return {}
    return {}


def status_of(cfg: dict | None) -> dict:
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "cta": cfg.get("cta", ""),
        "links": cfg.get("links", ""),
        "default_hashtags": cfg.get("default_hashtags", ""),
        "pinned_comment": cfg.get("pinned_comment", ""),
    }


def status() -> dict:
    return status_of(_config())


def normalize(cfg: dict) -> dict:
    """Clean/clamp a monetization config for storage."""
    return {
        "enabled": bool(cfg.get("enabled")),
        "cta": str(cfg.get("cta", "")).strip()[:500],
        "links": str(cfg.get("links", "")).strip()[:1000],
        "default_hashtags": str(cfg.get("default_hashtags", "")).strip()[:500],
        "pinned_comment": str(cfg.get("pinned_comment", "")).strip()[:500],
    }


def save(cfg: dict) -> dict:
    keep = normalize(cfg)
    _config_file().write_text(json.dumps(keep, indent=2))
    return status()


def apply_to_description(description: str, cfg: dict | None = None) -> str:
    """Append the CTA + links footer to a description, if enabled."""
    cfg = _config() if cfg is None else cfg
    if not cfg.get("enabled"):
        return description
    parts = [description.rstrip()] if description else []
    footer = []
    if cfg.get("cta"):
        footer.append(cfg["cta"].strip())
    if cfg.get("links"):
        footer.append(cfg["links"].strip())
    if footer:
        parts.append("\n".join(footer))
    return "\n\n".join(p for p in parts if p).strip()


def default_hashtags(cfg: dict | None = None) -> list[str]:
    cfg = _config() if cfg is None else cfg
    if not cfg.get("enabled"):
        return []
    raw = cfg.get("default_hashtags", "")
    tags = []
    for t in raw.replace(",", " ").split():
        t = t.strip().lstrip("#")
        if t:
            tags.append("#" + t)
    return tags
