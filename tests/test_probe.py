"""Media probe (task 4.2)."""
from __future__ import annotations

import pytest

from tests.helpers.media import make_video


@pytest.mark.req("10.8")
def test_probe_reports_the_fields_publishing_checks_need(tmp_path):
    from media.probe import probe

    path = make_video(tmp_path / "a.mp4", seconds=2, size=(320, 240), fps=25,
                      extra=["-movflags", "+faststart"])
    info = probe(path)
    assert info["width"] == 320 and info["height"] == 240
    assert info["fps"] == 25
    assert abs(info["duration"] - 2.0) < 0.15
    assert info["bytes"] == path.stat().st_size
    assert (info["video_codec"], info["pix_fmt"]) == ("h264", "yuv420p")
    assert info["field_order"] in (None, "progressive")
    assert (info["audio_codec"], info["sample_rate"], info["channels"]) == ("aac", 48000, 2)
    assert info["moov_before_mdat"] is True


@pytest.mark.req("10.8")
def test_moov_at_the_end_is_detected(tmp_path):
    from media.probe import probe

    info = probe(make_video(tmp_path / "late.mp4", seconds=1))
    assert info["moov_before_mdat"] is False


@pytest.mark.req("10.8")
def test_edit_lists_are_detected(tmp_path):
    from media.probe import probe

    without = make_video(tmp_path / "noelst.mp4", seconds=1, extra=["-use_editlist", "0"])
    with_elst = make_video(tmp_path / "elst.mp4", seconds=1, extra=["-use_editlist", "1"])
    assert probe(without)["has_edit_list"] is False
    assert probe(with_elst)["has_edit_list"] is True


@pytest.mark.req("10.8")
def test_audio_free_video(tmp_path):
    from media.probe import probe

    info = probe(make_video(tmp_path / "silent.mp4", seconds=1, audio=False))
    assert info["audio_codec"] is None and info["sample_rate"] is None


@pytest.mark.req("10.8")
def test_unreadable_and_missing_files_raise(tmp_path):
    from media.probe import ProbeError, probe

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(ProbeError):
        probe(junk)
    with pytest.raises(ProbeError):
        probe(tmp_path / "missing.mp4")
