"""File responses that honour HTTP Range requests (design KD10, §10.2).

Starlette 0.38.6 (pinned through FastAPI 0.115.0) always sends whole files, so
browsers cannot seek in a video without downloading it, and Safari refuses to
play video at all without range support. This supports a single byte range
per request, which is what video players send.
"""
from __future__ import annotations

import os
import re
import stat
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, Response, StreamingResponse

CHUNK = 256 * 1024
_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


def _disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _base_headers(path: Path, size: int, mtime: float, filename: str | None) -> dict:
    headers = {
        "accept-ranges": "bytes",
        "last-modified": formatdate(mtime, usegmt=True),
        "etag": f'"{int(mtime):x}-{size:x}"',
    }
    if filename:
        headers["content-disposition"] = _disposition(filename)
    return headers


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """(start, end_inclusive) for a satisfiable single range, None for "send the
    whole file" (no header, or a multi-range we don't serve), or "unsatisfiable"."""
    if not header:
        return None
    if "," in header:                       # multi-range: answer with the full body
        return None
    m = _RANGE.match(header)
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if size == 0:
        return "unsatisfiable"
    if first == "":                         # suffix range: the last N bytes
        length = int(last)
        if length == 0:
            return "unsatisfiable"
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or (last and end < start):
        return "unsatisfiable"
    return start, min(end, size - 1)


def _iter_file(path: Path, start: int, length: int):
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            data = f.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def ranged_file_response(request, path: Path, media_type: str,
                         filename: str | None = None) -> Response:
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        return Response(status_code=404)
    size, mtime = st.st_size, st.st_mtime
    headers = _base_headers(path, size, mtime, filename)

    rng = parse_range(request.headers.get("range"), size)
    if_range = request.headers.get("if-range")
    if rng is not None and if_range and if_range.strip() != headers["etag"]:
        rng = None                          # the file changed: send it whole

    if rng == "unsatisfiable":
        headers["content-range"] = f"bytes */{size}"
        return Response(status_code=416, headers=headers)

    if rng is None:
        if request.method == "HEAD":
            headers["content-length"] = str(size)
            return Response(status_code=200, headers=headers, media_type=media_type)
        return FileResponse(path, media_type=media_type, headers=headers,
                            stat_result=st)

    start, end = rng
    length = end - start + 1
    headers["content-range"] = f"bytes {start}-{end}/{size}"
    headers["content-length"] = str(length)
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers, media_type=media_type)
    return StreamingResponse(_iter_file(path, start, length), status_code=206,
                             headers=headers, media_type=media_type)
