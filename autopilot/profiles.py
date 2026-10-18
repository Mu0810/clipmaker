"""Autopilot profiles: saved settings applied to each new source (Requirement 14)."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationError, ValidationInfo,
                      field_validator)

import besttime
from autopilot import clearance, languages, store

PLATFORMS = ("youtube", "instagram")
WEEKDAYS = tuple(str(d) for d in range(7))          # "0" = Monday ... "6" = Sunday
NAME_MAX = 60
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_TAG_RE = re.compile(r"^\w{1,60}$")


def default_windows() -> dict:
    """Today's best-time table (besttime.py), keyed platform -> weekday -> hours."""
    return {p: {str(d): sorted(besttime._WINDOWS.get(p, {}).get(d, besttime._DEFAULT_HOURS))
                for d in range(7)}
            for p in PLATFORMS}


class Brand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tone: str = Field("", max_length=300)
    avoid_words: list[str] = Field(default_factory=list, max_length=100)
    always_hashtags: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("avoid_words")
    @classmethod
    def _avoid_words(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for word in value:
            word = " ".join(str(word).split())
            if not word:
                continue
            if len(word) > 60:
                raise ValueError("each word to avoid can be at most 60 characters")
            if word.casefold() not in {w.casefold() for w in out}:
                out.append(word)
        return out

    @field_validator("always_hashtags")
    @classmethod
    def _hashtags(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for tag in value:
            tag = str(tag).strip().lstrip("#")
            if not tag:
                continue
            if not _TAG_RE.match(tag):
                raise ValueError(f"'{tag}' is not a valid hashtag (letters, numbers and _ only)")
            if tag.casefold() not in {t.casefold() for t in out}:
                out.append(tag)
        return out


class ProfileSettings(BaseModel):
    """Every setting a profile holds (Requirement 14.1), defaults per 14.2."""
    model_config = ConfigDict(extra="forbid")

    clips_per_source: int = Field(6, ge=1, le=20)
    length_min_s: float = Field(20, ge=3, le=180)
    length_max_s: float = Field(60, ge=3, le=180)
    min_score: int = Field(60, ge=0, le=100)
    language: str | None = None                 # None = detect
    metadata_language: str | None = None        # None = same as spoken
    layout: Literal["fill", "fit"] = "fill"
    caption_style: Literal["words", "phrases", "karaoke"] = "words"
    hook: bool = True
    auto_trim: bool = True
    music_track: str | None = None
    music_volume: float = Field(0.25, ge=0, le=1)
    platforms: list[Literal["youtube", "instagram"]] = Field(
        default_factory=lambda: list(PLATFORMS))
    youtube_visibility: Literal["public", "private"] = "public"
    windows: dict[str, dict[str, list[int]]] = Field(default_factory=default_windows)
    min_gap_hours: float = Field(4, ge=0, le=48)
    daily_max: int = Field(2, ge=1, le=10)
    mode: Literal["review", "full_auto"] = "review"
    brand: Brand = Field(default_factory=Brand)
    ai_disclosure: bool = False

    @field_validator("language")
    @classmethod
    def _spoken_language(cls, value: str | None) -> str | None:
        # Whisper refuses any other code, so every video using the preset would
        # fail at its transcript. A regional code is stored as its language.
        if value is None or not value.strip():
            return None
        code = languages.spoken(value)
        if code is None:
            raise ValueError(f"Clip Maker can't transcribe '{value.strip()}'; pick a spoken "
                             "language from the list, or Detect")
        return code

    @field_validator("metadata_language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _LANG_RE.match(value):
            raise ValueError("use a language code such as 'en' or 'hi'")
        return value

    @field_validator("music_track")
    @classmethod
    def _music_track(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if Path(value).name != value:
            raise ValueError("music_track must be a file name from the music folder")
        return value

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("choose at least one platform")
        if len(set(value)) != len(value):
            raise ValueError("platforms must not repeat")
        return value

    @field_validator("windows")
    @classmethod
    def _windows(cls, value: dict) -> dict:
        if set(value) != set(PLATFORMS):
            raise ValueError("windows needs exactly the keys youtube and instagram")
        out: dict = {}
        for platform in PLATFORMS:
            days = value[platform]
            if set(days) != set(WEEKDAYS):
                raise ValueError(f"windows.{platform} needs weekdays '0' (Monday) to '6' (Sunday)")
            out[platform] = {}
            for day in WEEKDAYS:
                hours = days[day]
                if any(isinstance(h, bool) or not 0 <= h <= 23 for h in hours):
                    raise ValueError("posting hours must be whole hours from 0 to 23")
                if len(set(hours)) != len(hours):
                    raise ValueError("posting hours must not repeat within a day")
                out[platform][day] = sorted(hours)
        return out

    @field_validator("length_max_s")
    @classmethod
    def _longer_than_shortest(cls, value: float, info: ValidationInfo) -> float:
        # On the field, not the model, so the error names a field the
        # profile screen can show it beside (task 12.5).
        shortest = info.data.get("length_min_s")
        if shortest is not None and value <= shortest:
            raise ValueError("the longest clip must be longer than the shortest")
        return value


class ProfileError(Exception):
    """A refused profile change. With `field`, the route answers 422 with
    [{field, message}], so the profile screen shows it beside that field."""

    def __init__(self, status: int, message: str, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field


def _number(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return "This isn't valid."
    return text[0].upper() + text[1:] + ("" if text.endswith((".", "!", "?")) else ".")


def _message(err: dict) -> str:
    """A pydantic error in words a person reads beside the field."""
    kind, ctx = err.get("type", ""), err.get("ctx") or {}
    if kind == "greater_than_equal":
        return f"Must be at least {_number(ctx.get('ge'))}."
    if kind == "less_than_equal":
        return f"Must be at most {_number(ctx.get('le'))}."
    if kind == "greater_than":
        return f"Must be more than {_number(ctx.get('gt'))}."
    if kind == "less_than":
        return f"Must be less than {_number(ctx.get('lt'))}."
    if kind in ("int_parsing", "int_type", "int_from_float"):
        return "Enter a whole number."
    if kind in ("float_parsing", "float_type"):
        return "Enter a number."
    if kind in ("bool_parsing", "bool_type"):
        return "Choose yes or no."
    if kind == "literal_error":
        return "Choose one of the options."
    if kind == "string_too_long":
        return f"Use at most {ctx.get('max_length')} characters."
    if kind == "too_long":
        return f"Use at most {ctx.get('max_length')} entries."
    if kind == "extra_forbidden":
        return "This setting isn't known."
    if kind == "missing":
        return "This is required."
    msg = str(err.get("msg") or "")
    return _sentence(msg.removeprefix("Value error, ") if kind == "value_error" else msg)


def _field(loc) -> str:
    """The setting an error belongs to: the top-level name, brand settings
    by their own name, and never a list position or a weekday."""
    parts = [str(part) for part in loc]
    if not parts:
        return "body"
    if parts[0] == "brand" and len(parts) > 1:
        return f"brand.{parts[1]}"
    return parts[0]


def validation_details(exc: ValidationError) -> list[dict]:
    """Pydantic errors as plain JSON for a 422 response: `field` and
    `message` for the screen, with pydantic's own `loc`, `msg` and `type`."""
    return [{"field": _field(err["loc"]), "message": _message(err),
             "loc": [str(part) for part in err["loc"]], "msg": err["msg"],
             "type": err["type"]} for err in exc.errors()]


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ProfileError(422, "A preset needs a name.", field="name")
    if len(name) > NAME_MAX:
        raise ProfileError(422, f"Preset names can be at most {NAME_MAX} characters.",
                           field="name")
    return name


def validate_settings(owner: str, data: dict) -> ProfileSettings:
    """Raises ValidationError (shape) or ProfileError (owner-specific rules)."""
    settings = ProfileSettings.model_validate(data)
    if settings.music_track and not clearance.is_cleared(owner, settings.music_track):
        raise ProfileError(422, f"The music track '{settings.music_track}' is not marked "
                                "as cleared for publishing.", field="music_track")
    return settings


def _known_fields(settings: dict) -> dict:
    return {k: v for k, v in (settings or {}).items() if k in ProfileSettings.model_fields}


def _to_profile(row) -> dict:
    d = store.decode_row(row)
    return {"id": d["id"], "name": d["name"], "is_default": bool(d["is_default"]),
            "revision": d["revision"], "settings": d["settings"],
            "created_at": d["created_at"], "updated_at": d["updated_at"]}


def ensure_default(owner: str) -> dict:
    """The owner's default profile, creating 'Default' on first use."""
    now = time.time()
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM profiles WHERE owner_id=? "
                            "ORDER BY created_at, id", (owner,)).fetchall()
        if not rows:
            pid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO profiles(id, owner_id, name, settings_json, is_default, "
                "revision, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, owner, "Default", json.dumps(ProfileSettings().model_dump()),
                 1, 1, now, now))
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            return _to_profile(row)
        default = next((r for r in rows if r["is_default"]), None)
        if default is None:
            conn.execute("UPDATE profiles SET is_default=1 WHERE id=?", (rows[0]["id"],))
            default = conn.execute("SELECT * FROM profiles WHERE id=?",
                                   (rows[0]["id"],)).fetchone()
        return _to_profile(default)


def list_profiles(owner: str) -> list[dict]:
    ensure_default(owner)
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM profiles WHERE owner_id=? "
                            "ORDER BY is_default DESC, created_at, id", (owner,)).fetchall()
    return [_to_profile(r) for r in rows]


def get(owner: str, pid: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=? AND owner_id=?",
                           (pid, owner)).fetchone()
    return _to_profile(row) if row else None


def create(owner: str, name, settings: dict | None = None) -> dict:
    name = _clean_name(name)
    validated = validate_settings(owner, settings or {})
    ensure_default(owner)
    now = time.time()
    pid = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO profiles(id, owner_id, name, settings_json, is_default, "
            "revision, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (pid, owner, name, json.dumps(validated.model_dump()), 0, 1, now, now))
    return get(owner, pid)


def update(owner: str, pid: str, name=None, settings_patch: dict | None = None,
           make_default: bool = False) -> dict:
    """Rename, merge settings changes (the whole result is re-validated), or
    make this the default. Increments the revision."""
    current = get(owner, pid)
    if current is None:
        raise ProfileError(404, "Preset not found.")
    new_name = _clean_name(name) if name is not None else current["name"]
    new_settings = current["settings"]
    if settings_patch is not None:
        merged = {**_known_fields(current["settings"]), **settings_patch}
        new_settings = validate_settings(owner, merged).model_dump()
    with store.connect() as conn:
        conn.execute(
            "UPDATE profiles SET name=?, settings_json=?, revision=revision+1, "
            "updated_at=? WHERE id=? AND owner_id=?",
            (new_name, json.dumps(new_settings), time.time(), pid, owner))
        if make_default:
            conn.execute("UPDATE profiles SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END "
                         "WHERE owner_id=?", (pid, owner))
    return get(owner, pid)


def delete(owner: str, pid: str) -> None:
    with store.connect() as conn:
        rows = conn.execute("SELECT id, is_default FROM profiles WHERE owner_id=? "
                            "ORDER BY created_at, id", (owner,)).fetchall()
        target = next((r for r in rows if r["id"] == pid), None)
        if target is None:
            raise ProfileError(404, "Preset not found.")
        if len(rows) == 1:
            raise ProfileError(409, "You can't delete your only preset.")
        conn.execute("DELETE FROM profiles WHERE id=? AND owner_id=?", (pid, owner))
        if target["is_default"]:
            successor = next(r["id"] for r in rows if r["id"] != pid)
            conn.execute("UPDATE profiles SET is_default=1 WHERE id=?", (successor,))


def duplicate(owner: str, pid: str, name=None) -> dict:
    source = get(owner, pid)
    if source is None:
        raise ProfileError(404, "Preset not found.")
    new_name = name if name is not None else f"Copy of {source['name']}"[:NAME_MAX]
    return create(owner, new_name, _known_fields(source["settings"]))
