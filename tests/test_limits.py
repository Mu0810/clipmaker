"""Deterministic metadata validators (task 6.2, §5.2)."""
from __future__ import annotations

import pytest

from platforms import limits as L


@pytest.mark.req("4.3")
def test_angle_brackets_are_removed_from_every_youtube_field():
    out = L.youtube_fields({"title": "A <b>bold</b> take", "description": "see <this>",
                            "tags": ["a<b", "c>d"], "category_id": "27"})
    assert "<" not in out["title"] and ">" not in out["title"]
    assert out["description"] == "see this" and out["tags"] == ["ab", "cd"]
    assert out["category_id"] == "27"


@pytest.mark.req("4.3")
def test_the_title_is_trimmed_at_a_word_boundary():
    title = "word " * 30                      # 150 chars
    out = L.youtube_fields({"title": title})
    assert len(out["title"]) <= 100 and not out["title"].endswith("wor")


@pytest.mark.req("4.3", "4.5")
def test_a_multibyte_description_keeps_its_footer_within_the_byte_limit():
    body = "आ" * 4000                          # 3 bytes each in UTF-8 = 12,000 bytes
    footer = "Subscribe! #channel"
    desc = L.fit_description(body, footer)
    assert desc.endswith(footer)               # the footer survives the trim
    assert len(desc.encode("utf-8")) <= L.DESCRIPTION_MAX_BYTES
    # And it is cut on a character boundary, not mid-codepoint.
    assert desc == desc.encode("utf-8").decode("utf-8")


def test_a_footer_that_alone_overflows_is_trimmed():
    out = L.fit_description("body", "x" * 6000)
    assert len(out.encode("utf-8")) <= L.DESCRIPTION_MAX_BYTES


@pytest.mark.req("4.3")
def test_tag_cost_counts_quotes_and_commas():
    assert L.tag_cost("solo") == len("solo") + 1          # + comma
    assert L.tag_cost("two words") == len("two words") + 2 + 1   # + quotes + comma
    kept = L.fit_tags(["aaaa", "two words", "zzzzzzzzzz"], limit=20)
    assert kept == ["aaaa", "two words"]       # 5 + 11 = 16; the third would overflow


@pytest.mark.req("4.3")
def test_an_unknown_category_falls_back_to_people_and_blogs():
    assert L.youtube_fields({"title": "t", "category_id": "999"})["category_id"] == "22"
    assert L.youtube_fields({"title": "t", "category_id": "27"})["category_id"] == "27"


@pytest.mark.req("4.3")
def test_instagram_keeps_at_most_30_hashtags_and_20_mentions():
    caption = ("Great clip! " + " ".join(f"#tag{i}" for i in range(40))
               + " " + " ".join(f"@user{i}" for i in range(25)))
    out = L.instagram_fields({"caption": caption})["caption"]
    assert out.count("#") == 30 and out.count("@") == 20
    assert "#tag0" in out and "#tag39" not in out       # extras dropped from the end
    assert "@user0" in out and "@user24" not in out


@pytest.mark.req("4.3")
def test_a_long_instagram_caption_is_trimmed():
    out = L.instagram_fields({"caption": "word " * 600})["caption"]   # 3000 chars
    assert len(out) <= 2200


@pytest.mark.req("4.5")
def test_words_to_avoid_match_at_word_boundaries_case_insensitively():
    assert L.surviving_avoid_words("I love CRYPTO coins", ["crypto"]) == ["crypto"]
    assert L.surviving_avoid_words("a cryptography talk", ["crypto"]) == []   # not a whole word
    meta = {"title": "Clean", "description": "no bad words", "tags": ["scam alert"]}
    assert L.avoid_words_in_meta(meta, "youtube", ["scam"]) == ["scam"]
    assert L.avoid_words_in_meta({"caption": "all good"}, "instagram", ["scam"]) == []


@pytest.mark.req("4.5")
def test_the_hook_rule():
    assert L.hook_ok("Wait for the twist", "A different title", "A spoken opening")
    assert not L.hook_ok("one", "", "")                     # too few words
    assert not L.hook_ok("one two three four five six seven", "", "")   # too many
    assert not L.hook_ok("Same As Title", "same as title", "x")        # equals the title
    assert not L.hook_ok("the first line", "t", "The First Line")      # equals first sentence
