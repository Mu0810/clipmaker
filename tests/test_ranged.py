"""Range requests on /api/clip and the job-id guards (task 4.2)."""
from __future__ import annotations

import pytest

JOB = "0123456789ab"
DATA = bytes(range(256)) * 40            # 10,240 bytes


@pytest.fixture
def clip(_isolated_data):
    job_dir = _isolated_data / "output" / JOB
    job_dir.mkdir(parents=True)
    (job_dir / "01-a.mp4").write_bytes(DATA)
    return f"/api/clip/{JOB}/01-a.mp4"


@pytest.mark.req("7.2")
def test_full_response_advertises_ranges(client, clip):
    r = client.get(clip)
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"


@pytest.mark.req("7.2")
@pytest.mark.parametrize("header,start,end", [
    ("bytes=0-99", 0, 99),
    ("bytes=10000-", 10000, 10239),
    ("bytes=-240", 10000, 10239),
    ("bytes=10200-99999", 10200, 10239),
])
def test_single_ranges(client, clip, header, start, end):
    r = client.get(clip, headers={"Range": header})
    assert r.status_code == 206
    assert r.content == DATA[start:end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{len(DATA)}"
    assert r.headers["content-length"] == str(end - start + 1)


@pytest.mark.req("7.2")
@pytest.mark.parametrize("header", ["bytes=20000-", "bytes=500-100", "bytes=-0"])
def test_unsatisfiable_ranges_get_416(client, clip, header):
    r = client.get(clip, headers={"Range": header})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(DATA)}"


@pytest.mark.req("7.2")
def test_multi_range_falls_back_to_the_full_body(client, clip):
    r = client.get(clip, headers={"Range": "bytes=0-1,5-6"})
    assert r.status_code == 200 and r.content == DATA


@pytest.mark.req("7.2")
def test_head_requests(client, clip):
    full = client.head(clip)
    assert full.status_code == 200 and full.headers["content-length"] == str(len(DATA))
    part = client.head(clip, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.headers["content-length"] == "10"


@pytest.mark.req("7.2")
def test_stale_if_range_gets_the_full_file(client, clip):
    r = client.get(clip, headers={"Range": "bytes=0-9", "If-Range": '"stale"'})
    assert r.status_code == 200 and r.content == DATA
    etag = client.head(clip).headers["etag"]
    r = client.get(clip, headers={"Range": "bytes=0-9", "If-Range": etag})
    assert r.status_code == 206


@pytest.mark.req("7.2")
def test_download_keeps_its_attachment_header(client, clip):
    r = client.get(clip + "?download=true", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-disposition"].startswith('attachment; filename="01-a.mp4"')


@pytest.mark.req("N3.2")
@pytest.mark.parametrize("path", [f"/api/clip/{JOB}/..%2F..%2Fclipmaker.db",
                                  f"/api/clip/{JOB}/missing.mp4",
                                  "/api/clip/not-a-job-id/01-a.mp4"])
def test_traversal_and_bad_ids_are_404(client, clip, path):
    assert client.get(path).status_code == 404


@pytest.mark.req("N3.2", "N6.1")
@pytest.mark.parametrize("job_id", ["%2E%2E", "%2E", "output", "0123456789AB", "0123"])
def test_delete_job_rejects_ids_that_are_not_job_ids(client, _isolated_data, job_id):
    """Percent-encoded dot segments reach the route as '..' and '.'; the plain
    forms are normalised away by the client before they could get there."""
    sentinel = _isolated_data / "keep.txt"
    sentinel.write_text("data folder contents")
    response = client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 404
    assert sentinel.exists() and (_isolated_data / "clipmaker.db").exists()
    assert (_isolated_data / "output").is_dir()


@pytest.mark.req("N3.2", "N6.1")
@pytest.mark.parametrize("raw", ["/api/jobs/..", "/api/jobs/."])
def test_plain_dot_segments_never_reach_delete(client, _isolated_data, raw):
    sentinel = _isolated_data / "keep.txt"
    sentinel.write_text("data folder contents")
    response = client.delete(raw)
    assert response.status_code in (404, 405)
    assert sentinel.exists() and (_isolated_data / "output").is_dir()


@pytest.mark.req("N6.1")
def test_delete_job_removes_only_that_jobs_folder(client, _isolated_data):
    other = _isolated_data / "output" / "ffffffffffff"
    other.mkdir(parents=True)
    target = _isolated_data / "output" / JOB
    target.mkdir(parents=True)
    (target / "01-a.mp4").write_bytes(b"x")
    assert client.delete(f"/api/jobs/{JOB}").json() == {"ok": True}
    assert not target.exists() and other.exists()
    assert (_isolated_data / "output").is_dir()
