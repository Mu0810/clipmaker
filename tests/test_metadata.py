"""Per-platform AI metadata: Write, Check, Rewrite and holds (task 6.2, §5)."""
from __future__ import annotations

import json

import pytest

import llm
from autopilot import metadata

CLIP = "We shipped the feature on Tuesday and users loved the dark mode."


def write(youtube=None, instagram=None, hook="Watch the reveal", titles=None,
          self_check=None):
    body = {
        "youtube": youtube or {"title": "Dark mode shipped Tuesday",
                               "description": "We shipped dark mode.", "tags": ["dark mode"],
                               "category_id": "28"},
        "instagram": instagram or {"caption": "Dark mode is here!", "hashtags": ["#darkmode"]},
        "hook": hook, "title_suggestions": titles or ["Dark mode shipped Tuesday"]}
    if self_check is not None:
        body["self_check"] = self_check
    return json.dumps(body)


def check(supported=True, claims=None, sensitive=False, cats=None):
    return json.dumps({"supported": supported, "unsupported_claims": claims or [],
                       "sensitive": sensitive, "sensitive_categories": cats or [],
                       "reason": ""})


def run(fake_llm, *replies, **kwargs):
    fake_llm.queue(*replies)
    with llm.count_calls() as counter:
        out = metadata.generate(CLIP, **kwargs)
    out["_calls"] = counter.count
    return out


@pytest.mark.req("4.1", "4.2", "N8.1")
def test_the_happy_path_uses_two_calls_and_no_holds(fake_llm):
    out = run(fake_llm, write(), check())
    assert out["_calls"] == 2 and out["holds"] == []
    assert out["youtube"]["title"] and out["instagram"]["caption"]
    assert out["youtube"]["category_id"] == "28"


@pytest.mark.req("4.6", "N8.1")
def test_a_failed_check_gets_one_rewrite_then_an_unsupported_hold(fake_llm):
    # Write, Check (fails), Rewrite whose self_check still admits a problem = 3 calls.
    out = run(fake_llm, write(), check(supported=False, claims=["sold a million"]),
              write(youtube={"title": "Still wrong", "description": "sold a million",
                             "tags": ["x"], "category_id": "22"},
                    self_check={"supported": False, "notes": "could not verify"}))
    assert "unsupported_claims" in out["holds"]
    assert out["_calls"] == 3            # Write + Check + Rewrite, no re-Check (N8.1)


@pytest.mark.req("4.6")
def test_a_rewrite_that_fixes_the_claim_leaves_no_hold(fake_llm):
    out = run(fake_llm, write(), check(supported=False, claims=["sold a million"]),
              write(youtube={"title": "Dark mode shipped", "description": "We shipped it.",
                             "tags": ["dark mode"], "category_id": "22"},
                    self_check={"supported": True, "notes": "all supported"}))
    assert out["holds"] == [] and out["_calls"] == 3


@pytest.mark.req("4.5")
def test_a_surviving_word_to_avoid_gives_an_avoid_words_hold(fake_llm):
    # The rewrite still contains the banned word, so the hold stays.
    bad = {"title": "A scam exposed", "description": "total scam", "tags": ["scam"],
           "category_id": "22"}
    out = run(fake_llm, write(youtube=bad), check(),
              write(youtube=bad), check(),
              brand={"avoid_words": ["scam"]})
    assert "avoid_words" in out["holds"]


@pytest.mark.req("13.3")
def test_a_sensitive_flag_gives_a_sensitive_hold(fake_llm):
    out = run(fake_llm, write(), check(sensitive=True, cats=["health claims"]))
    assert "sensitive" in out["holds"]
    assert out["sensitive_categories"] == ["health claims"]


@pytest.mark.req("4.9")
def test_no_ai_engine_gives_a_heuristic_hold_and_no_calls(fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    with llm.count_calls() as counter:
        out = metadata.generate(CLIP, fallback_title="Dark mode shipped")
    assert counter.count == 0 and "heuristic_metadata" in out["holds"]
    assert out["youtube"]["title"] and out["instagram"]["caption"]


@pytest.mark.req("4.7", "4.8")
def test_made_for_kids_and_disclosure_never_come_from_the_ai(fake_llm):
    # Even if the model tries to set them, generate() ignores anything but the
    # caller's disclosure flag and never emits a made_for_kids field.
    out = run(fake_llm, write(), check())
    assert "made_for_kids" not in out["youtube"] and out["disclosure_ai"] is False
    on = run(fake_llm, write(), check(), disclosure=True)
    assert on["disclosure_ai"] is True


@pytest.mark.req("4.4")
def test_the_language_is_passed_to_the_prompt(fake_llm):
    run(fake_llm, write(), check(), language="hi")
    assert "Hindi (hi)" in fake_llm.calls[0]["prompt"]


@pytest.mark.req("4.3", "4.5")
def test_brand_hashtags_and_footer_are_applied_within_limits(fake_llm):
    monetize_cfg = {"enabled": True, "cta": "Subscribe!", "links": "example.com",
                    "default_hashtags": "mychannel"}
    out = run(fake_llm, write(), check(),
              brand={"always_hashtags": ["brandtag"]}, monetize_cfg=monetize_cfg)
    assert "Subscribe!" in out["youtube"]["description"]
    assert "#brandtag" in out["instagram"]["caption"] and "#mychannel" in out["instagram"]["caption"]
    assert len(out["youtube"]["description"].encode("utf-8")) <= 5000
