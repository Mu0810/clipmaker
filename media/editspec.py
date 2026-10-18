"""EditSpec: the declarative description of one clip version (design §4.4).

The AI's first cut, every editor change and every re-render are EditSpecs
rendered by one renderer, and a version never changes after it is rendered
(design KD9):

    {"v": 1, "in": 123.4, "out": 168.2, "cuts": [[130.12, 131.05]],
     "auto_trim": true, "layout": "fill",
     "crop": {"mode": "auto", "x": null},
     "captions": {"enabled": true, "style": "words", "edits": {"1043": "Kimchi"}},
     "hook": {"enabled": true, "text": "The part nobody saw", "duration": 2.5},
     "music": {"track": null, "volume": 0.25},
     "cover": {"t": 3.2}}

`in`, `out` and `cuts` are source seconds. `captions.edits` maps word numbers
in transcript.json to corrected text. `crop.x` (manual crop) is the crop
centre as a fraction of the source width. `cover.t` is clip time after cuts
and trimming; null means it is chosen when rendering.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from media import keep

MIN_LENGTH_S = 3.0
HOOK_MAX = 48
EDIT_MAX = 60
HOOK_DURATION_S = 2.5
_EPS = 1e-6


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Crop(_Part):
    mode: Literal["auto", "manual"] = "auto"
    x: float | None = Field(None, ge=0, le=1)


class Captions(_Part):
    enabled: bool = True
    style: Literal["words", "phrases", "karaoke"] = "words"
    edits: dict[str, str] = Field(default_factory=dict)


class Hook(_Part):
    enabled: bool = True
    text: str = ""
    duration: float = Field(HOOK_DURATION_S, ge=0.5, le=5)


class Music(_Part):
    track: str | None = None
    volume: float = Field(0.25, ge=0, le=1)


class Cover(_Part):
    t: float | None = Field(None, ge=0)


class EditSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    v: Literal[1] = 1
    in_: float = Field(alias="in", ge=0)
    out: float = Field(gt=0)
    cuts: list[tuple[float, float]] = Field(default_factory=list)
    auto_trim: bool = True
    layout: Literal["fill", "fit"] = "fill"
    crop: Crop = Field(default_factory=Crop)
    captions: Captions = Field(default_factory=Captions)
    hook: Hook = Field(default_factory=Hook)
    music: Music = Field(default_factory=Music)
    cover: Cover = Field(default_factory=Cover)

    def to_dict(self) -> dict:
        return self.model_dump(by_alias=True)


class EditSpecError(ValueError):
    """One or more problems, each {"field": dotted path, "message": text}."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__("; ".join(f"{p['field']}: {p['message']}" for p in problems))


def _field(loc) -> str:
    return ".".join("in" if part == "in_" else str(part) for part in loc) or "spec"


def parse(data) -> EditSpec:
    """The spec's shape only (types, ranges, unknown keys)."""
    try:
        return EditSpec.model_validate(data)
    except ValidationError as e:
        raise EditSpecError([{"field": _field(err["loc"]), "message": err["msg"]}
                             for err in e.errors()]) from None


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def validate(data, source_duration: float, *, words: list[dict] | None = None,
             is_cleared: Callable[[str], bool] | None = None,
             min_length: float = MIN_LENGTH_S) -> EditSpec:
    """Check a spec against its source and return it normalised: cuts merged,
    texts on one line. With the transcript's words, caption edits are checked
    against them and the minimum length (3 s, Reels' minimum, unless the
    caller sets `min_length`) applies after auto-trim. Music needs
    `is_cleared(track)` to say yes (Requirement 3.9). Raises EditSpecError."""
    spec = parse(data)
    problems: list[dict] = []

    def bad(field: str, message: str) -> None:
        problems.append({"field": field, "message": message})

    t_in, t_out = spec.in_, spec.out
    window_ok = True
    if t_out <= t_in:
        bad("out", "The clip must end after it starts.")
        window_ok = False
    if t_out > source_duration + 1e-3:
        bad("out", f"The clip ends at {t_out:.2f} s, after the video ends "
                   f"({source_duration:.2f} s).")
        window_ok = False

    cuts = []
    for k, (a, b) in enumerate(spec.cuts):
        if b <= a:
            bad(f"cuts.{k}", "A cut must end after it starts.")
        elif a < t_in - _EPS or b > t_out + _EPS:
            bad(f"cuts.{k}", "Cuts must lie inside the clip.")
        else:
            cuts.append((a, b))
    spec.cuts = keep.merge(cuts)

    edits = {}
    for key, text in spec.captions.edits.items():
        field = f"captions.edits.{key}"
        if not (key.isascii() and key.isdigit()):
            bad(field, "Subtitle edits are keyed by word number.")
        elif words is not None and int(key) >= len(words):
            bad(field, "There is no word with that number in the transcript.")
        elif not _one_line(text):
            bad(field, "A corrected word can't be empty; cut the word instead.")
        elif len(_one_line(text)) > EDIT_MAX:
            bad(field, f"A corrected word can be at most {EDIT_MAX} characters.")
        else:
            edits[str(int(key))] = _one_line(text)
    spec.captions.edits = edits

    spec.hook.text = _one_line(spec.hook.text)
    if len(spec.hook.text) > HOOK_MAX:
        bad("hook.text", f"The hook can be at most {HOOK_MAX} characters.")
    elif spec.hook.enabled and not spec.hook.text:
        bad("hook.text", "Write the hook text, or turn the hook off.")

    track = spec.music.track
    if track is not None:
        if not track or Path(track).name != track or track in (".", ".."):
            bad("music.track", "Choose a track from the music folder.")
        elif is_cleared is None or not is_cleared(track):
            bad("music.track", f"The track '{track}' isn't marked as cleared for publishing.")

    if spec.crop.mode == "manual" and spec.crop.x is None:
        bad("crop.x", "A manual crop needs a position.")
    elif spec.crop.mode == "auto":
        spec.crop.x = None

    if window_ok:
        cut_length = (t_out - t_in) - keep.kept_duration(spec.cuts)
        length = cut_length
        if words is not None and spec.auto_trim:
            length = keep.kept_duration(
                keep.keep_spans(words, t_in, t_out, spec.cuts, True))
        if length < min_length - _EPS:
            if length < cut_length - _EPS:
                bad("length", f"After trimming pauses and filler the clip is {length:.1f} s; "
                              f"clips must be at least {min_length:g} s. Turn off pause "
                              "trimming or make the clip longer.")
            else:
                bad("length", f"The clip is {length:.1f} s after cuts; clips must be at "
                              f"least {min_length:g} s.")
        elif spec.cover.t is not None and spec.cover.t >= length:
            bad("cover.t", "The cover frame must be inside the clip.")

    if problems:
        raise EditSpecError(problems)
    return spec


def initial(t_in: float, t_out: float, settings: dict, hook_text: str = "") -> EditSpec:
    """The AI's first cut of a clip: the selected span, the profile's editing
    settings and the metadata's hook (design §4.4). The cover is chosen when
    rendering."""
    text = _one_line(hook_text)[:HOOK_MAX].strip()
    return EditSpec.model_validate({
        "in": t_in, "out": t_out, "cuts": [],
        "auto_trim": settings.get("auto_trim", True),
        "layout": settings.get("layout", "fill"),
        "captions": {"enabled": True, "style": settings.get("caption_style", "words")},
        "hook": {"enabled": bool(settings.get("hook", True) and text), "text": text},
        "music": {"track": settings.get("music_track"),
                  "volume": settings.get("music_volume", 0.25)},
    })
