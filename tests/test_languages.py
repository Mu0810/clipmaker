"""The languages Clip Maker handles (autopilot/languages.py; design §15,
after task 13.2)."""
from __future__ import annotations

import pytest

from autopilot import languages, metadata


@pytest.mark.req("2.7", "14.1")
def test_the_languages_are_the_ones_whisper_transcribes():
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    # A faster-whisper upgrade that adds or drops a language shows up here.
    assert set(languages.NAMES) == set(_LANGUAGE_CODES)
    names = [o["name"] for o in languages.options()]
    assert names == sorted(names) and len(set(names)) == len(names) == 100
    assert [languages.name(c) for c in ("hi", "pt-BR", "yue", "xx", None)] == [
        "Hindi", "Portuguese", "Cantonese", None, None]
    assert [languages.spoken(c) for c in ("en-US", " ta ", "haw", "fil", "Hindi")] == [
        "en", "ta", "haw", None, None]


@pytest.mark.req("4.4")
def test_the_metadata_prompt_names_the_language():
    assert metadata.language_label("jw") == "Javanese (jw)"
    assert metadata.language_label("pt-BR") == "Portuguese (pt-BR)"
    assert metadata.language_label("x-klingon") == "x-klingon"
    assert metadata.language_label(None) == "the clip's spoken language"
