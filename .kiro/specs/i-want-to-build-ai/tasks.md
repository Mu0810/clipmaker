# Implementation Plan: AI Autopilot

Spec `i-want-to-build-ai` · feature · draft for review · 9 Oct 2026 · implements `requirements.md` and `design.md` in this folder (`§N` means a design.md section)

## How to execute

- Work in `/Users/hello/clipmaker`. Run the tests with `.venv/bin/python -m pytest -q`. The whole suite must pass after every sub-task. UI tests (from task 12.2) run with `-m ui`.
- Tag every test with `@pytest.mark.req("<id>")`, naming the requirement IDs it checks. Task 13.2 fails if any requirement has no test.
- Tests run offline against the fakes from task 1.3. No test may upload anything, call the AI engine or make a network request.
- Never start the app or run migrations against the real `/Users/hello/clipmaker/clipmaker.db` or the real token files. For a manual check, use a scratch server: `CLIPMAKER_DATA_DIR=$KIROCREW_SCRATCH/cm-data .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765`.
- Ask the user before installing anything (`pip install`, `brew install`, model downloads). Never run `setup.sh`: it deletes `.venv`.
- The folder is not a git repository. Record progress by ticking boxes here.
- ⏸ marks a checkpoint. Stop and wait for the user.

## Tasks

- [x] 1. Test harness and isolated data

  - [x] 1.1 Make every data path relocatable
    - Add `paths.py` with functions that read `CLIPMAKER_DATA_DIR` on each call and default to the repository folder: `data_dir()`, `db_path()`, `output_dir()`, `music_dir()`, `data_file(name)`. Two functions always point into the repository: `models_dir()` and `fonts_dir()`.
    - Use them in place of the module constants:
      - `db.DB_PATH`
      - `OUTPUT` in `app.py`
      - `pipeline.OUTPUT_ROOT`
      - `scheduler.OUTPUT`
      - `music.MUSIC_DIR`
      - `llm.CONFIG_FILE`
      - `monetize.CONFIG_FILE`
      - `youtube_upload.CLIENT_SECRET` and `TOKEN_FILE`
      - `instagram_upload.SESSION_FILE` and `META_FILE`
      - `youtube_pw.PROFILE_DIR` and `META_FILE`
    - `static/` and the login page stay in the repository. With the variable unset, behaviour is unchanged.
    - Verify: `tests/test_paths.py` shows every path resolving under a temporary data directory.
    - _Requirements: N7.1, N6.1_

  - [x] 1.2 Move start-up side effects into a lifespan hook
    - Move what `app.py` does at import time into `bootstrap()`, run from a FastAPI `lifespan`: creating the output folder, `db.init()`, `db.reconcile_jobs()`, the pro-table init calls, `scheduler.start()` and `_start_workers()`.
    - Skip the background threads when `CLIPMAKER_NO_BACKGROUND=1`.
    - Later tasks add steps to `bootstrap()` in the order given in §13.
    - Verify: entering `with TestClient(app)` starts no thread named `scheduler` or `job-worker`.
    - _Requirements: N7.1, N6.1_

  - [x] 1.3 Pytest harness and offline guards (§19)
    - Add `requirements-dev.txt` with `pytest==9.1.1`, and install it after asking the user.
    - Add `pytest.ini`:
      - `testpaths = tests`
      - markers `req` and `ui`
      - `addopts = -m "not ui"`
    - Add `tests/conftest.py` with:
      - an autouse fixture that sets `CLIPMAKER_DATA_DIR` to `tmp_path` and `CLIPMAKER_NO_BACKGROUND=1`, and asserts that `paths.db_path()` is inside `tmp_path`;
      - an offline guard that patches `httpx.HTTPTransport.handle_request` and `httpx.AsyncHTTPTransport.handle_async_request` to route requests to fakes registered by host. An unregistered host raises `AssertionError`, except `localhost:11434`, which raises `httpx.ConnectError` so Ollama looks absent;
      - a socket guard that refuses `socket.socket.connect` to any non-loopback address;
      - a `client` fixture (`with TestClient(app) as c`);
      - a `fake_llm` fixture that replaces `llm.chat` and `llm.is_configured` with a scripted responder.
    - Verify: `tests/test_harness.py` checks that `GET /api/jobs` returns `{"jobs": []}` from the temporary database, that an unregistered HTTP request raises, and that connecting a socket to a public address raises.
    - _Requirements: N7.1_

- [x] 2. Close today's security gaps and migrate the database

  - [x] 2.1 Remove the unsafe `/api/pro` code (§13.2)
    - First copy the `CREATE TABLE` statements from `presets.py`, `batch.py`, `analytics.py` and `scheduler_advanced.py` into `tests/fixtures/legacy_pro_schema.sql`.
    - Delete `api_pro.py`, `presets.py`, `batch.py`, `analytics.py`, `scheduler_advanced.py` and `editor.py`.
    - Remove their imports, init calls and the `include_router` line from `app.py`, and their imports from the database-init block of `setup.sh`.
    - In `README.md`, remove the "Pro Endpoints" and "New Pro Modules" sections.
    - Keep `scheduler.py`; task 9.5 retires it.
    - Verify: `tests/test_pro_removed.py` checks that no route starts with `/api/pro`, and that `POST /api/pro/editor/trim` with file paths in the body returns a non-2xx status without starting a subprocess (`subprocess.run` monkeypatched to record calls).
    - _Requirements: 15.1, 15.2, 15.3_

  - [x] 2.2 Cross-site request check and file permissions (§16)
    - Add `autopilot/__init__.py` and `autopilot/hardening.py`.
    - `bootstrap()` starts with `os.umask(0o077)`. After `db.init()` it sets mode 600 on `clipmaker.db` (and its `-wal` and `-shm` files), `token.json`, `client_secret.json`, `meta_app.json`, `meta_token.json`, `ig_session.json`, `llm_config.json` and `monetize_config.json`, and mode 700 on `yt_pw_profile/`, wherever they exist.
    - Add a middleware for non-GET, non-HEAD and non-OPTIONS requests to `/api/*`. It rejects with 403 `{"detail": "Cross-site request blocked."}` when:
      - the `Origin` host and port differ from the `Host` header;
      - `Origin` is `null`;
      - there is no `Origin` and the `Referer` host differs.

      Requests with neither header pass, so command-line clients keep working.
    - Verify: `tests/test_hardening.py` checks:
      - a cross-site `Origin` gets 403 on `POST /api/process` and on a multipart `POST /api/process-file`;
      - a same-origin request reaches the handler (400 for an empty URL);
      - GET requests are unaffected;
      - fixture files end up with modes 600 and 700;
      - a file created after start-up has mode 600.
    - _Requirements: N3.1_

  - [x] 2.3 Schema version 1 migration (§3, §13)
    - In `db._conn()`, set `PRAGMA busy_timeout = 5000`.
    - Add `autopilot/migrations.py` with `run()`, following §13:
      - back up with `sqlite3.Connection.backup` to `clipmaker.db.bak-<YYYYmmdd-HHMMSS>` (mode 600);
      - set `PRAGMA journal_mode=WAL` outside the transaction;
      - in one transaction:
        - add the §3.1 columns and the §3.2 tables and indexes;
        - copy `schedules` into `posts` with the §13.1 mapping, keeping ids. The owner is `coalesce(user_id, 'local')`, and `meta_json` and `ai_meta_json` are `{title, description, tags, privacy, file}`;
        - drop legacy pro tables that are empty and rename the others to `legacy_<name>`;
        - set `PRAGMA user_version = 1`.
    - `bootstrap()` calls `run()` right after `db.init()`.
    - Add `autopilot/store.py`: a connection context manager that uses `db._conn()` and `db._LOCK`, row decoding that turns each `x_json` column into a decoded `x`, and `app_state` get and set.
    - Verify: `tests/test_migrations.py` builds a database with `db.init()` plus `legacy_pro_schema.sql` and sample rows (jobs, users, settings, and schedules in each of the four statuses), then checks:
      - the backup exists with mode 600 and the same row counts;
      - `user_version` is 1 and the journal mode is WAL;
      - old rows are unchanged and new columns hold their defaults;
      - schedules map to posts as in §13.1;
      - empty pro tables are dropped and a seeded one is renamed;
      - a second `run()` makes no new backup and changes nothing.
    - _Requirements: N2.1, N6.2, 15.4_

- [x] 3. Autopilot foundations

  - [x] 3.1 Owner, preferences, activity log and router (§3.2, §11, §14)
    - `autopilot/owner.py`:
      - `current_owner(request)` returns the signed-in user's id when `auth.enabled()`, `local` otherwise, and raises 401 when auth is on but nobody is signed in;
      - `get_prefs(owner)` and `set_prefs(owner, **changes)` store preferences in `user_settings` under `skey='autopilot_prefs'`: `timezone` (validated with `zoneinfo.ZoneInfo`), `paused`, `last_profile_id` and `autopilot_default`.
    - `autopilot/activity.py`: `log(owner, actor, kind, summary, job_id=None, clip_id=None, post_id=None, detail=None)`, which rejects any kind outside a fixed `KINDS` set; `list_entries(owner, filters, before_id, limit)`; `purge(older_than_days=400)`.
    - `autopilot/api.py`: an `/api/autopilot` router with GET and PATCH `/prefs`, included in `app.py` before the static mount.
    - Verify: `tests/test_owner.py` and `tests/test_activity.py` check:
      - single-user mode resolves to `local`;
      - with two users in multi-user mode, each reads and writes only their own preferences, checked both ways;
      - an invalid time zone gets 400 and a signed-out request gets 401;
      - an unknown activity kind is rejected and purge keeps 400 days.
    - _Requirements: 5.6, 12.1, 15.3, N3.2, N4.1, N4.2_

  - [x] 3.2 Profiles and music clearance (§12, §14)
    - `autopilot/profiles.py`:
      - `ProfileSettings` is a pydantic model with `extra="forbid"` and the §12 fields and defaults. `windows` defaults to `besttime._WINDOWS`, stored as `{platform: {"0".."6": [hours]}}`.
      - Validation: clips per source 1–20; length from 3 s, minimum below maximum, maximum at most 180 s; minimum score 0–100; gap 0–48 h; daily maximum 1–10; at least one platform; hours 0–23 with no repeats; volume 0–1; `music_track` must be cleared.
      - Functions: create, read, update (incrementing `revision`), delete (refused for the last profile), `ensure_default(owner)`, `duplicate` and `make_default`.
    - Add the profile routes from §14, except `/replan` (task 6.4).
    - Music clearance: `POST /api/autopilot/music/{track}/clearance {cleared}`, with the track resolved through `music.resolve`. `GET /api/music` gains a `cleared` list and keeps its current keys.
    - Verify: `tests/test_profiles.py` checks:
      - the defaults match the §12 table;
      - an unknown key gets 422 and an uncleared track is rejected;
      - the last profile cannot be deleted, and `duplicate` copies the settings;
      - owners are isolated, checked both ways.
    - _Requirements: 3.9, 14.1, 14.2, N4.2_

  - [x] 3.3 Readiness checklist (§9.4)
    - `autopilot/readiness.py`:
      - `check(owner)` returns items `{key, kind, platform, ok, message, fix}` for the §9.4 rows. The inputs are the `accounts` rows (`scopes_json`, `status`, `made_for_kids`, declarations), `llm.is_configured()`, `PIL.features.check_feature("raqm")` and `("fribidi")`, whether `cv2` imports and `models/face_detection_yunet_2023mar.onnx` exists, and the preferred time zone.
      - `platform_ready(owner, platform)` returns `(ok, reasons)`.
    - Add `GET /api/autopilot/readiness`.
    - Verify: `tests/test_readiness.py` inserts account rows and checks that the required items flip, that advisory items never block a platform, and that each message matches §9.4.
    - _Requirements: 11.1, 11.2, 11.3_

- [x] 4. Media building blocks

  - [x] 4.1 Dependencies and setup scripts (§20)
    - Add `opencv-python-headless==4.14.0.94` and `fonttools==4.66.1` to `requirements.txt`. Ask, then install, and confirm `cv2` imports alongside the installed numpy 2.5.1.
    - Add `scripts/fetch_models.sh`:
      - download `https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx` into `models/`;
      - check it against a SHA-256 pinned in the script, computed on the first download and dated in a comment;
      - exit non-zero on a mismatch.
    - `setup.sh`: run `brew install fribidi` when it is missing, after asking, and call `scripts/fetch_models.sh`.
    - `start.sh`: export `DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:/usr/lib"` before starting uvicorn.
    - `.gitignore`: add `meta_app.json`, `meta_token.json`, `clipmaker.db.bak-*` and `models/*.onnx`.
    - `DEPLOY.md`: the Docker steps install `ffmpeg libfribidi0 fonts-noto-core tzdata` and run the model download.
    - Verify:
      - `bash -n setup.sh start.sh scripts/fetch_models.sh` passes;
      - `scripts/fetch_models.sh` downloads the model and verifies it;
      - with the `start.sh` environment, Pillow reports both `raqm` and `fribidi` as available.
    - _Requirements: 3.2, 3.5_

  - [x] 4.2 Media probe, ranged responses and a test-video helper (§4.6, §10.2)
    - Add `tests/helpers/media.py` with `make_video(path, seconds, size, fps, audio=True)`, built on ffmpeg's `testsrc2` and `sine` sources.
    - `media/__init__.py` and `media/probe.py`: `probe(path)` uses `ffprobe -of json` to report duration, bytes, width, height, fps, video codec, pixel format, field order, video bitrate, audio codec, sample rate and channels. A small MP4 box walker adds `moov_before_mdat` and `has_edit_list` (any `elst` box under `moov/trak/edts`).
    - `media/ranged.py`: `ranged_file_response(request, path, media_type, filename=None)` handles single ranges (`a-b`, `a-`, `-n`) with 206 and `Content-Range`, always sends `Accept-Ranges: bytes`, returns 416 with `Content-Range: bytes */size` for unsatisfiable ranges, answers multi-range requests with the full 200 body, and supports HEAD.
    - `/api/clip/{job}/{name}` uses it, keeping the path-traversal guard and `?download=true`.
    - Verify: `tests/test_probe.py` and `tests/test_ranged.py` check:
      - probe fields are correct on generated files;
      - `has_edit_list` is false for a file written with `-use_editlist 0` and true for libx264 output written with `-use_editlist 1`;
      - every range case, through `TestClient`.
    - _Requirements: 7.2, 10.8_

  - [x] 4.3 Word-level transcripts (§4.2)
    - Before changing any code, record today's `pipeline.parse_subtitles()` output for a fixture VTT as an expected-output file.
    - `media/transcript.py`:
      - `transcribe_words(video, language=None, model_name=…, progress=None)` uses multilingual faster-whisper (int8, CPU, word timestamps, VAD, beam 1, no previous-text conditioning). The model comes from `CLIPMAKER_WHISPER_MODEL`, default `small`.
      - `sentences(words)` follows the §4.2 rules: end punctuation including `। ॥ ؟ 。 ！ ？`, pauses over 0.8 s, and sentences over 25 s split at their longest pause.
      - `from_caption_segments()` gives length-weighted words with `estimated: true`.
      - `save()` and `load()` read and write `transcript.json` v1, and `to_segments()` returns `highlights.Segment` objects.
    - Manual flow: `pipeline.process` writes `transcript.json`. Captions produce estimated words. The Whisper path keeps `WHISPER_MODEL` (default `base.en`) and English, and adds word timestamps. `transcribe.transcribe()` and `pipeline.parse_subtitles()` keep their return types.
    - Verify: `tests/test_transcript.py` uses a fake Whisper model and checks:
      - the sentence rules, language passthrough and cache reuse;
      - estimated words from captions;
      - `parse_subtitles()` output still matches the recorded expected file.
    - _Requirements: 1.8, 2.6, 2.7, 3.4_

  - [x] 4.4 Clip selection (§4.3)
    - `media/select.py`: `select_clips(transcript, n, length_min, length_max, language)` returns `(clips, method)`, as specified in §4.3:
      - the prompt contract;
      - splitting into at most 3 overlapping chunks above about 60,000 estimated tokens;
      - index validation and fitting each candidate to the length range by whole sentences;
      - the overlap filter, then the top N, labelled `ai:<model>`;
      - the Ollama and heuristic fallbacks, moved from `highlights.py` to sentence units and scored by `highlights.score_viral_batch`.
    - `llm.py`: add a thread-local `count_calls()` context manager that counts each `llm.chat()` call and each Ollama generate call made inside it.
    - `highlights.select_highlights()` delegates to `select_clips`, keeping its signature. The manual target length t maps to the range `max(10, 0.6·t)` to `min(180, 1.4·t)`.
    - Verify: `tests/test_select.py` checks:
      - fake AI replies (valid, out of range, overlapping, too short, too long) are handled as §4.3 describes;
      - a long fixture produces 3 chunks;
      - the Ollama fallback (a fake registered for `localhost:11434`) and the heuristic fallback each carry their label;
      - clip edges fall on word boundaries and `count_calls()` counts correctly.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, N8.1_

  - [x] 4.5 EditSpec and keep spans (§4.4, §4.5)
    - `media/editspec.py`: the EditSpec model and validation from §4.4. Validation takes an `is_cleared(track)` callable for the music check.
    - `media/keep.py`: keep spans from words with auto-trim (gaps over 0.5 s and filler-only runs removed, 0.1 s padding that never crosses a gap's midpoint), `to_clip_time()`, `to_source_time()` and `kept_duration()`.
    - Verify: `tests/test_editspec.py` and `tests/test_keep.py` cover cut merging, the 3 s minimum, hook length, volume range, uncleared music, padding clamps and time mapping in both directions.
    - _Requirements: 3.6, 7.3, 7.7_

- [x] 5. Rendering

  - [x] 5.1 Script-aware text drawing (§4.7)
    - `media/text.py`:
      - script detection by Unicode block;
      - font discovery that searches `/System/Library/Fonts`, `/System/Library/Fonts/Supplemental`, `/Library/Fonts`, `~/Library/Fonts`, the Debian Noto folders and `fonts/`. It prefers the per-script names in §4.7 and confirms coverage from each font's character map with fontTools. In a font collection it uses the Bold face, then Semibold, then the first face;
      - mixed-script lines split into runs, each measured with its own font;
      - `needs_shaping(text)` and `shaping_available()`;
      - RTL word order;
      - emoji stripped from burned text.
    - `captions.py` and `thumbnail.py` choose and draw fonts through `media/text.py`.
    - Verify: `tests/test_text.py` checks:
      - Latin, Devanagari, Bengali and Arabic samples each resolve to a font that covers every character. A script with no candidate font on the machine is skipped, with the reason;
      - a character no font covers is dropped and logged;
      - a Hinglish line splits into runs;
      - `shaping_available()` false (monkeypatched) is reported;
      - Arabic words come out in RTL order.
    - _Requirements: 3.5_

  - [x] 5.2 Caption and hook overlay stream (§4.6 step 3)
    - `media/overlays.py`: `build_overlays(words, keep_map, spec, out_dir)` returns `(concat_path, intervals)`. It applies caption edits, chunks words for each style (words, phrases, karaoke), places the hook box, and writes one 1080×1920 RGBA PNG per interval plus an `ffconcat` file with exact durations, repeating the last file.
    - Verify: `tests/test_overlays.py` checks:
      - interval boundaries for scripted words in each style;
      - durations summing to the clip length;
      - valid `ffconcat` syntax;
      - caption edits applied and the hook window respected;
      - PNG size and mode, and the karaoke highlight colour `#FFD63D`.
    - _Requirements: 3.4, 3.7, 7.4_

  - [x] 5.3 Face-tracked reframing (§4.6 step 2)
    - `media/reframe.py` with an injectable detector:
      - the default detector is `cv2.FaceDetectorYN` with the YuNet model, falling back to OpenCV's bundled Haar cascade;
      - frames come from an ffmpeg `rawvideo` pipe at 4 fps and 640 px wide;
      - shot cuts use HSV-histogram correlation below 0.6;
      - faces are linked into tracks at IoU ≥ 0.3, and the primary track is chosen;
      - the crop holds still inside the central 70%, eases over 0.4 s, jumps at shot cuts and is clamped to the frame;
      - output is a `sendcmd` file plus the track as JSON, or `None` when no shot has a face.
    - Verify: `tests/test_reframe.py` checks:
      - with scripted detections, the crop holds, eases, jumps and clamps, and a moving face stays inside the crop in at least 95% of samples;
      - with no faces, the result is `None`;
      - the `sendcmd` lines are well formed;
      - a detector smoke test on a generated frame, skipped when `cv2` or the model is missing.
    - _Requirements: 3.2, 3.3_

  - [x] 5.4 Cover frame and thumbnail (§4.6 step 5)
    - `media/cover.py`:
      - sample at 2 fps, skipping the first and last 0.5 s. Samples come from the original-ratio clip inside the crop window, not the vertical, so burned-in captions and hook don't count as focus (changed during the task; see §4.6 step 5);
      - score frames by Laplacian variance (computed with numpy), plus 0.5 when a face is detected;
      - write the 1080×1920 cover JPEG, and the 16:9 thumbnail through `thumbnail.generate()`, re-saved at lower quality if it is over 2 MB.
    - Verify: `tests/test_cover.py` uses a generated video that is blurred everywhere except a sharp middle span and checks that the chosen time falls in that span, the thumbnail is at most 2 MB, and the cover is 1080×1920.
    - _Requirements: 3.8, 8.9_

  - [x] 5.5 EditSpec renderer (§4.6)
    - `media/render.py`: `render_version(job_dir, source, transcript, spec, version, base_name, progress)` produces:
      - the original-ratio clip from the keep spans;
      - the crop track (Fill layout);
      - the overlay stream;
      - the vertical, encoded with `libx264 -preset veryfast -crf 20 -maxrate 20M -bufsize 20M -x264-params vbv-init=0.5 -bf 0 -pix_fmt yuv420p` (the buffer and B-frame settings changed during the task; see §4.6 step 4), frame rate clamped to 23–60 fps (30 otherwise), AAC 128 kb/s at 48 kHz stereo, and `-movflags +faststart -use_editlist 0`. Music is ducked under speech, using cleared tracks only;
      - the cover and thumbnail;
      - the probe results.
    - Version 1 keeps today's file names; later versions add `.vN` before the suffix.
    - Verify: `tests/test_render.py` renders 12 s generated sources (1280×720, 25 fps and 15 fps) and checks:
      - the output is 1080×1920, H.264 4:2:0 progressive, 23–60 fps (15 fps input gives 30), AAC 48 kHz stereo, moov before mdat, no edit list, and at most 25 Mb/s;
      - the duration matches the kept duration within one frame for a spec with a cut;
      - a solid-colour overlay interval appears within one frame of its start and end;
      - the Fill path with a `sendcmd` jump renders, and a source with no faces renders in Fit;
      - music mixing works with a generated track.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.9, 10.8, N1.4_

  - [x] 5.6 Manual flow on the shared renderer (§2.2)
    - `pipeline.process` builds a version-1 EditSpec per clip and renders it with `render_version`. The layout is `fit`, to keep today's look; captions, hook, trim and music come from the request. Its signature and result shape stay the same.
    - Verify: `tests/test_pipeline_manual.py` runs `pipeline.process` on a generated local source with a fixture transcript and `fake_llm`, then checks that the result and clip keys match a recorded key set, every listed file exists, and the vertical is 1080×1920.
    - _Requirements: 1.8, N6.1_

- [x] 6. Autopilot pipeline

  - [x] 6.1 Intake, restart recovery and stages (§4.1)
    - `autopilot/intake.py` and the routes `POST /sources/probe`, `POST /sources` and `POST /sources/file`, as specified in §4.1:
      - `yt-dlp --print` fields for each URL, with a 60 s timeout;
      - at most 20 URLs, and sources over 3 hours refused;
      - the rights confirmation enforced on the server, skipped for the user's own channel;
      - for files, duration from ffprobe and a SHA-256;
      - the new `jobs` columns filled, and sources queued in submission order.
    - `requeue_interrupted()` replaces `db.reconcile_jobs()` in `bootstrap()`.
    - `/api/process` and `/api/process-file` store `request_json`. The job `stage` is saved with progress, and `/api/status` returns it.
    - Verify: `tests/test_intake.py`, with a fake `yt-dlp` runner, checks:
      - a source over 3 h is refused;
      - a missing confirmation is refused, and the user's own channel (an inserted account row) needs none;
      - submission order is kept;
      - an interrupted job is re-queued once, then fails with "Interrupted twice by a restart";
      - a manual job's request is stored.
    - _Requirements: 1.2, 1.3, 1.5, 1.6, 13.1, 13.2_

  - [x] 6.2 Per-platform AI metadata (§5)
    - `platforms/__init__.py` and `platforms/limits.py`: the §5.2 text validators and the list of YouTube categories.
    - `autopilot/metadata.py`: the Write, Check and Rewrite calls with their prompts, brand rules (words to avoid; always-include hashtags and the monetization footer appended by code, then the limits re-applied), hook rules and the §5.3 holds.
    - Verify: `tests/test_limits.py` and `tests/test_metadata.py` check:
      - a multi-byte description trimmed at a character boundary keeps its footer;
      - tag-length counting includes quotes and commas, and `<` and `>` are removed;
      - an Instagram caption keeps at most 30 hashtags and 20 @-mentions;
      - a failed Check gets exactly one Rewrite, then an `unsupported_claims` hold;
      - a surviving word to avoid gives an `avoid_words` hold;
      - the heuristic fallback gives a `heuristic_metadata` hold, and a sensitive flag gives a `sensitive` hold;
      - made for kids never comes from the AI, and AI disclosure is off by default;
      - the language is passed to the prompts;
      - each clip uses at most 3 calls.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 13.3, N8.1_

  - [x] 6.3 Posting planner (§6.2, §6.3)
    - `autopilot/planner.py`:
      - `assign(posts, occupied, profile, tz, now)` is a pure function;
      - `account_windows(account_metrics, defaults)` returns the windows plus a basis label.
    - Verify: `tests/test_planner.py` checks:
      - candidate slots in Asia/Kolkata and in America/New_York across 1 Nov 2026 and 14 Mar 2027 (nonexistent times move forward 1 h, ambiguous ones use `fold=0`);
      - the 30-minute lead time, minimum gap and daily maximum;
      - placement in score order;
      - user-locked, uploaded and live posts occupying their slots;
      - the `unscheduled` reason;
      - account-data windows: at least 10 posts with a 72 h snapshot, weekday-and-hour buckets with at least 2 posts, hour-of-day pooling, the defaults, and the basis text.
    - _Requirements: 5.2, 5.3, 5.4, 5.6_

  - [x] 6.4 Posts and the review-queue API (§6.1, §6.4, §7, §14)
    - Add store functions for `clips`, `clip_versions` and `posts`.
    - `autopilot/posts.py`:
      - `create_posts_for_job()` follows §6.1: minimum score, profile platforms, the 13.4 overlap skip with an activity entry, holds, approval by mode, `blocked` through `readiness.platform_ready`, then the planner;
      - an `is_cleared()` predicate;
      - `release_unapproved(now)`;
      - approve (overriding a hold is logged), reject (frees the slot and marks an uploaded post for `yt_delete`), reset a field to the AI version, PATCH (limits enforced, `remote_dirty` set when uploaded), move a slot (locked, with gap and daily-limit warnings), add a post, and bulk actions in one transaction with a result per post.
    - Routes from §14: GET `/posts` (filters, plus `limits` for the UI counters), GET and PATCH `/posts/{id}`, POST `/posts`, approve, reject, reset, bulk, and `/profiles/{id}/replan`.
    - Verify: `tests/test_posts_api.py` checks:
      - a held post stays pending in Full auto;
      - a Review-mode post past its slot moves to `waiting_approval`, and after approval gets the next free slot;
      - reset restores the AI value, and a limit violation gets 422 naming the field;
      - a bulk action with mixed results reports each post;
      - a moved slot is locked and a re-plan leaves it alone;
      - a clip already posted to the same account is skipped;
      - owners are isolated, checked both ways.
    - _Requirements: 5.1, 5.5, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 13.4, 14.4, 15.2, N3.2, N4.1, N4.2_

  - [x] 6.5 Autopilot source pipeline (§4)
    - `autopilot/process.py`: `run(job_id)` performs, in order:
      - download or local source;
      - transcript, using the Autopilot model and the profile language;
      - selection, counted by `llm.count_calls()`;
      - metadata for each clip;
      - the initial EditSpec;
      - render version 1 into `clips` and `clip_versions`, mirrored into `result_json`;
      - the `sensitive` and `text_shaping` holds;
      - `create_posts_for_job()`;
      - stages and progress, `best_score` and the 1.7 message, `jobs.ai_calls`, and activity entries.
    - The profile snapshot is stored at submission.
    - `app._worker_loop` sends jobs with `autopilot = 1` to `process.run`.
    - Verify: `tests/test_process.py` runs a generated 40 s source with a fixture transcript and `fake_llm` (no Whisper download) and checks:
      - the clip, version and post rows, with slots inside the windows and holds where the AI replies set them;
      - `ai_calls` at most 3N + 3, and the stage sequence;
      - when every score is below the minimum, no posts are created and the message gives the best score;
      - with Autopilot off, no posts are created.
    - _Requirements: 1.4, 1.7, 1.8, 2.3, 3.1, 14.3, N8.1_

- [x] 7. Platform clients

  - [x] 7.1 HTTP client, error classes and host allow-list (§8.6, §16)
    - `platforms/http.py`:
      - an `httpx` client factory with timeouts;
      - refuses any host except `www.googleapis.com`, `oauth2.googleapis.com`, `graph.facebook.com` and `rupload.facebook.com`;
      - a logging filter that redacts `Authorization` headers and `access_token=` values;
      - captures request ids (`X-GUploader-UploadID`, `x-fb-trace-id`).
    - `platforms/errors.py`: `classify(platform, status, body)` returns `temporary`, `deferred`, `access` or `permanent`, with the platform code, following the §8.6 table. The Instagram codes and subcodes come from Meta's error-code reference:

      | Code / subcode | Class |
      |---|---|
      | 9 / 2207042 | deferred (publishing limit) |
      | 4 / 2207051 | permanent (Instagram suspects spam; never retried) |
      | 25 / 2207050 | access (account restricted) |
      | 24 / 2207006, 24 / 2207008, -2 / 2207020, -1 / 2207001, -1 / 2207032, -1 / 2207053 | temporary; the retry uses a new container |
      | 9007 / 2207027 | still processing |
      | 352 / 2207026 | permanent (unsupported format) |

    - Verify: `tests/test_errors.py` and `tests/test_http.py` check every row of the classification table, refusal of a host outside the list, and redaction in captured logs.
    - _Requirements: 10.2, 10.3, N3.1, N3.4_

  - [x] 7.2 YouTube Data API client (§8.2)
    - `platforms/google_creds.py`:
      - load, refresh and save Google credentials from `token.json` in single-user mode and `user_settings.youtube` in multi-user mode;
      - load the scopes stored with the token, so a broader Autopilot grant isn't reduced;
      - `granted_scopes()`.
    - `platforms/youtube_api.py`:
      - resumable init, chunk PUT and status query;
      - videos list, update and delete;
      - `thumbnails.set`;
      - the user's channel and uploads lookup;
      - statistics in batches of 50;
      - counters `yt-uploads:<client id>` and `yt-units:<client id>` in `platform_usage`, per Pacific day (`America/Los_Angeles`).
    - Add `tests/fakes/youtube.py`.
    - Verify: `tests/test_youtube_api.py` checks:
      - init headers and body;
      - resume after a 308 using `Range`, and 404 and 410 reported as "session gone";
      - update sends the full snippet and status objects;
      - counters reset at Pacific midnight across a DST change, and a 101st upload in a day is refused.
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 8.9, 10.4, 10.6, 12.5_

  - [x] 7.3 Meta login and the Instagram API client (§8.3, §9.2)
    - `platforms/meta_auth.py`:
      - the dialog URL with `config_id`, `response_type=code` and `override_default_response_type=true`;
      - code exchange and long-lived token exchange;
      - `/me/accounts` discovery, filtered to Pages with `MANAGE` or `CREATE_CONTENT`;
      - `debug_token` with the app token, and `appsecret_proof`.
    - `platforms/instagram_api.py`:
      - `content_publishing_limit`;
      - resumable REELS container with `caption`, `thumb_offset` and `is_ai_generated`;
      - rupload with an offset;
      - container status, `media_publish`, media lookup by `since` and caption, and insights (`views`, `likes`, `comments`).
      - The token goes only in headers. The Page token is tried first, then the user token, and the one that worked is recorded.
    - Add `tests/fakes/instagram.py`.
    - Verify: `tests/test_instagram_api.py` checks:
      - rupload headers, and resume from `bytes_transferred`;
      - the `appsecret_proof` HMAC;
      - no token ever appears in a URL;
      - Page discovery filtering;
      - the token fallback and its recorded choice.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 11.4, 12.5_

- [x] 8. Connections

  - [x] 8.1 YouTube connection for Autopilot (§9.1, §9.3)
    - Scopes: the manual flows keep `youtube.upload` and `youtube.readonly`. "Connect for Autopilot" adds `youtube` with `include_granted_scopes=true`:
      - single-user mode: `POST /api/connections/youtube/connect`, using `InstalledAppFlow` and writing `token.json`;
      - multi-user mode: `GET /api/connections/youtube/start`, using the existing web flow and callback.
    - After connecting, fill `accounts` from the user's channel, with `quota_key = 'yt:<client id>'`.
    - Routes: `GET /api/connections/youtube`, `POST /api/autopilot/youtube/declarations` (audience, audit passed, OAuth app in production) and `POST /api/connections/youtube/disconnect`. Disconnect deletes the token, revokes it best effort, and marks the account and its unpublished posts `needs_reconnect`.
    - `README.md`: add the YouTube part of an "Autopilot setup" section.
    - Verify: `tests/test_connect_youtube.py`, with the flows mocked, checks:
      - the account row and the stored scopes;
      - declarations are saved;
      - disconnect has the effects above;
      - the manual `/api/upload` path is unchanged.
    - _Requirements: 4.7, 8.1, 11.1, 11.5, 11.6, 11.7, 11.8_

  - [x] 8.2 Instagram connection through Meta (§9.2, §9.3)
    - `autopilot/connections_api.py`, Instagram routes:
      - `POST /instagram/app`: single-user mode saves `meta_app.json` with mode 600; multi-user mode reads the `CLIPMAKER_META_*` variables;
      - `GET /instagram/start`, with a per-owner `state` that is single-use and expires after 10 minutes;
      - `GET /instagram/callback`: check the state, exchange the code, get a long-lived token, discover Pages, then select automatically when there is one or ask when there are several;
      - `POST /instagram/token` (the paste-a-token fallback), `POST /instagram/select` and `POST /instagram/disconnect`.
    - The account row stores `page_id`, `external_id` and both expiry dates.
    - `/api/instagram/status` adds `manual_only: true` for the instagrapi login.
    - `README.md`: add the Meta part of "Autopilot setup".
    - Verify: `tests/test_connect_instagram.py` checks:
      - a replayed or expired state is rejected;
      - a missing `instagram_content_publish` permission is named;
      - no professional account gives the 11.4 message;
      - several Pages lead to a selection step;
      - disconnect has the effects above.
    - _Requirements: 9.1, 11.4, 11.5, 11.6, 11.7, 11.8, N3.3_

- [x] 9. Publisher

  - [x] 9.1 Publisher loop (§8.1, §8.5, §8.8)
    - `autopilot/publisher.py`:
      - `tick(now)` with an injectable clock;
      - the `publisher_lease` (60 s) and the heartbeat;
      - due-work selection as in §8.1, and the missed-slot pass from §8.5;
      - calls `posts.release_unapproved`;
      - pause gating, except for pause steps;
      - pre-publish checks: format via `platforms/limits.check_file(probe, platform)` from §8.8, account status, and rights recorded;
      - one single-thread executor per account;
      - each `publish_attempts` row written in the same transaction as the post update;
      - the retry schedule (2, 10 and 30 minutes) and deferrals;
      - an access error moves every post of that account to `needs_reconnect`;
      - a registry of step handlers.
    - `bootstrap()` starts the publisher thread, ticking every 20 s.
    - Verify: `tests/test_publisher.py`, with stub steps and a fake clock, checks:
      - with two instances, only the lease holder acts;
      - missed slots: only the most recent one under 2 h old publishes, the others are re-planned with their locks dropped and logged, and uploaded posts are left alone;
      - one step per account at a time;
      - a pause blocks new starts;
      - a format block names the limit;
      - an access error fans out to the account's posts.
    - _Requirements: 6.3, 6.4, 10.5, 10.7, 10.8, 10.9, N2.1_

  - [x] 9.2 YouTube publishing steps (§8.2, §8.4)
    - Step handlers `yt_init`, `yt_upload`, `yt_lookup`, `yt_finalize`, `yt_verify`, `yt_update`, `yt_unschedule`, `yt_reschedule`, `yt_delete` and `yt_replace`, with errors classified as in 7.1.
    - `yt_init` saves the session URI, file size and SHA-256 before sending any bytes.
    - A `kept_private` post links to the [YouTube API audit form](https://support.google.com/youtube/contact/yt_api_form).
    - Add `POST /posts/{id}/replace`.
    - Verify: `tests/test_publish_youtube.py`, against the fake, checks:
      - every row of the §8.2 table;
      - a crash after k chunks, resumed by a new publisher instance, leaves exactly one video;
      - a lost session with a matching upload found creates no duplicate, and with none found starts a new session;
      - `quotaExceeded` defers to Pacific midnight, and `uploadLimitExceeded` waits 24 h;
      - a 403 from `thumbnails.set` marks the capability `unavailable`;
      - verification at +15 min and +2 h gives `live` or `kept_private`;
      - rejecting a post deletes its upload.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 10.1, 10.2, 10.3, 10.4, N2.2_

  - [x] 9.3 Instagram publishing steps (§8.3, §8.4)
    - Step handlers `ig_limit`, `ig_container`, `ig_upload`, `ig_processing`, `ig_publish` and `ig_resolve`.
    - The container id and upload URI are saved before uploading. A reached limit moves the post past the estimated reset and notifies the user (9.5).
    - Verify: `tests/test_publish_instagram.py`, against the fake, checks:
      - every row of the §8.3 table;
      - a crash between container and publish resumes without a duplicate;
      - when the publish outcome is unknown and the container reads PUBLISHED, the post resolves to `live`;
      - status is polled once a minute, at most 5 times, then treated as temporary;
      - ERROR or EXPIRED leads to a new container on the retry;
      - the limit rule above.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 10.1, 10.2, 10.3, 10.4, N2.2_

  - [x] 9.4 Pause and resume (§7)
    - `POST /api/autopilot/pause {paused}` stores the flag in the preferences and logs it.
    - While paused, uploaded YouTube posts with a future publish time get `yt_unschedule` and `paused_hold = 1`.
    - On resume, posts whose slots passed are re-planned, and `paused_hold` posts get `yt_reschedule` at the later of their slot and now + 15 min.
    - Verify: `tests/test_pause.py` checks that no publish starts while paused, the unschedule and reschedule calls, and that slots which passed during the pause are re-planned.
    - _Requirements: 6.8, 6.9, 6.10_

  - [x] 9.5 Legacy schedules on the publisher; retire `scheduler.py` (§8.7, §13.2)
    - The publisher runs posts with `method = legacy` through today's upload functions and never retries them.
    - `/api/schedule` GET, POST and DELETE are re-implemented over `posts`, with today's request and response shapes.
    - Delete `scheduler.py` and its start-up call.
    - Autopilot never creates legacy posts.
    - Verify: `tests/test_schedule_compat.py` checks:
      - responses match fixtures captured from today's endpoints before the change;
      - a due legacy post uploads through the monkeypatched legacy functions;
      - a failed legacy post is not retried, and one interrupted by a restart is marked failed;
      - Autopilot code paths cannot create a legacy post.
    - _Requirements: 11.6, 15.4, N6.1_

  - [x] 9.6 Deleting a source with planned posts (1.9)
    - `GET /sources/{id}/delete-impact`, `DELETE /sources/{id}` and the existing `DELETE /api/jobs/{id}` share one path:
      - report the number of unpublished posts;
      - on confirmation, cancel them, queue `yt_delete` for uploaded YouTube videos that aren't public yet, and keep the rows and metrics of published posts.
    - Verify: `tests/test_source_delete.py` checks the counts, the cancellations, the YouTube deletion (fake) and that published history is kept.
    - _Requirements: 1.9, 8.5_

- [x] 10. Maintenance and reporting

  - [x] 10.1 Token health, metrics, purge and blocked re-planning (§8.9, §11)
    - `autopilot/maintenance.py` and `autopilot/metrics.py`. An hourly tick, started from `bootstrap()`, does the following:
      - checks each account at most once a day:
        - Google: refresh the token and save it. `invalid_grant` means `needs_reconnect`; when that happens 6–8 days after consent, the message points to the OAuth app's "Testing" status;
        - Meta: `debug_token`. Invalid means `needs_reconnect`; either expiry within 7 days gives a reconnect reminder;
      - fetches metrics daily for 30 days after a post is published: YouTube statistics in batches of 50, within 9,000 quota units a day; Instagram insights. Each fetch adds a `post_metrics` row with `age_hours`;
      - purges activity older than 400 days;
      - re-plans `blocked` posts once their platform is ready.
    - Verify: `tests/test_maintenance.py`, with a fake clock and fakes, checks:
      - the once-a-day limit;
      - each token outcome and the 7-day reminder;
      - metric rows and the unit cap;
      - the purge;
      - blocked posts getting slots after a ready account is inserted.
    - _Requirements: 5.4, 10.6, 10.7, 11.2, 12.1, 12.5_

  - [x] 10.2 Counts, activity and history API (§11, §14)
    - Add three routes:
      - `GET /counts` returns `{failed, waiting_approval, needs_reconnect, paused}`;
      - `GET /activity` takes filters (kind, source, post) and is paged by `before_id`;
      - `GET /history` lists posts in `live`, `private`, `kept_private` or `failed`, with their link and latest metrics.
    - Verify: `tests/test_reporting_api.py` checks that counts follow post statuses, history shows the latest metric snapshot, and owners are isolated, checked both ways.
    - _Requirements: 12.1, 12.2, 12.4, 12.5_

- [x] 11. Editor backend

  - [x] 11.1 Editor API and render worker (§10.2, §10.4)
    - `autopilot/editor_api.py` and the `/api/editor` routes:
      - `GET /clips/{id}` imports clips created before this feature from `result_json` as version 1. When the job's transcript is missing or estimated, it transcribes the proxy window with word timestamps;
      - `POST /clips/{id}/proxy` builds a proxy on the render worker: 540 px tall, `-g 15`, `-crf 30`, AAC 96 kb/s, served through `media/ranged.py`;
      - `POST /clips/{id}/render` validates the spec, creates the version in `rendering` and queues it. When the render is ready, the clip's posts in `scheduled`, `waiting_approval`, `unscheduled`, `blocked` or `retrying` switch to it. Uploaded posts get `yt_replace` only with `confirm_replace`. A failed render leaves the previous version in use;
      - `GET /renders/{version id}` reports progress.
    - Add one render worker thread, separate from the job workers, started from `bootstrap()`.
    - Verify: `tests/test_editor_api.py` checks:
      - importing a job created the old way;
      - the proxy's height and keyframe interval;
      - a successful render switches posts, and a failed one keeps the previous version;
      - `confirm_replace` gates the replacement of uploaded videos;
      - a render completes while a job worker is busy;
      - owners are isolated, checked both ways.
    - _Requirements: 7.1, 7.2, 7.7, 7.8, 7.9, 8.6, N1.4, N4.2_

- [x] 12. Frontend (§15)

  - [x] 12.1 ⏸ Mockups for the review queue and the clip editor
    - Build 2 static HTML mockups of each screen in `mockups/autopilot/`, using fake data and the colour and font variables from `static/index.html`. Make the options genuinely different in layout and density, and recommend one of each.
    - Serve them with `python3 -m http.server 8766 --bind 127.0.0.1 --directory mockups/autopilot` and take one screenshot of each, at most 1500 px wide.
    - Stop here. Show the screenshots, wait for the user's choice, and write the chosen options under this task. Tasks 12.4 and 12.6 follow those choices.
    - **Chosen 17 Oct 2026:**
      - Review queue: **A · Agenda** (`mockups/autopilot/queue-a.html`). Posts are rows under their day, with each platform's count for the day. One post opens inline for editing. A bar at the bottom approves or rejects what's ticked: posts, a whole day, or a whole video.
      - Clip editor: **B · Script** (`mockups/autopilot/editor-b.html`). The transcript is the main surface, with START and END pins on words and fillers and pauses marked inline. A slim source strip above has handles for fine trims. The preview and settings sit in a column on the right.
      - Screenshots: `mockups/autopilot/shots/`. The chosen mockups are the visual spec for 12.4 and 12.6.
    - _Requirements: 6.5, 7.1, N5.2_

  - [x] 12.2 Frontend shell, UI test harness and the header chip
    - Add `static/js/api.js` (fetch helper with JSON errors), `static/js/router.js` (hash routes `#/autopilot/...` and `#/editor/<id>`; Escape goes back) and `static/css/autopilot.css`, loaded as modules from `static/index.html`.
    - Add an "Autopilot" header chip:
      - shows a counts badge and the pause state;
      - polls `/counts` every 30 s and announces changes through `aria-live`;
      - shows a browser notification when a count rises and notifications are allowed, with a one-time "Allow notifications" control.
    - Add `tests/ui/conftest.py`:
      - a live uvicorn server on a free port, bound to 127.0.0.1, with a temporary data directory and a seeding helper;
      - Playwright with `channel="chrome"`, headless; the tests skip when Chrome is unavailable;
      - endpoints that would reach the network are faked with `page.route`.
    - Verify: `tests/ui/test_shell.py` (`-m ui`) checks that the chip renders, the badge follows seeded counts, and Escape leaves a route.
    - _Requirements: 6.8, 12.2, 12.3, N5.2_

  - [x] 12.3 Composer and the Sources view
    - Composer:
      - the Autopilot switch and profile picker, remembered through the preferences;
      - with the switch on, today's option rows are replaced by a one-line profile summary, and a "Several links" textarea (at most 20 lines) appears;
      - probe, then a rights dialog naming each channel, skipped for the user's own channel;
      - a file upload requires the confirmation.
    - `#/autopilot/sources`, for each source:
      - stage and percentage, and a "Restarted once" note;
      - the 1.7 message with the best score;
      - the number of AI calls;
      - delete, with an impact confirmation.
    - Verify: `tests/ui/test_composer.py` checks the switch behaviour, the 20-line limit, a rights dialog listing channels (probe faked), submission creating sources, and a stage update appearing in the list.
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.7, 1.9, 13.1, 13.2, N8.1_

  - [x] 12.4 Review queue view
    - `static/js/queue.js`, following the mockup chosen in 12.1:
      - posts grouped by day; each card shows the cover, platform, account, an editable slot (with gap and daily-limit warnings), score and reason, holds with their reasons, and editable fields with live counters driven by the server's `limits`;
      - actions: Approve, Reject, Reset to AI version and Edit clip;
      - bulk selection by source or by day;
      - the pause switch;
      - Instagram's "posts only while the app is running" note, and the `kept_private` explanation;
      - `content-visibility: auto` on cards;
      - times shown in the preferred time zone with its name.
    - Verify: `tests/ui/test_queue.py` checks:
      - 200 seeded posts render within 1 s, measured with `performance.now()`;
      - approve works from the keyboard;
      - the counters are correct;
      - a bulk approve shows a result per post;
      - Reset to AI version works.
    - _Requirements: 5.5, 5.6, 6.2, 6.5, 6.6, 6.7, 6.8, 8.7, 9.5, N1.3, N5.1, N5.2_

  - [x] 12.5 Setup, Profiles, History and Activity views
    - `#/autopilot/setup`:
      - the readiness checklist with fix-it steps;
      - "Connect for Autopilot" for YouTube, with the scope explained;
      - the channel-audience choice and the advisory declarations;
      - Instagram app settings, the Meta-side steps, connect, the Page picker and the paste-a-token fallback;
      - connection details (11.5);
      - the time zone.
    - `#/autopilot/profiles`: create, edit, duplicate, delete and make default, with validation errors next to fields, "Re-plan pending posts", and music-track clearance.
    - `#/autopilot/history` and `#/autopilot/activity`.
    - Verify: `tests/ui/test_setup_profiles.py` checks:
      - checklist items follow seeded accounts;
      - Instagram connect opens the start URL (faked);
      - a profile validation error shows next to its field;
      - history rows link out;
      - activity pages load.
    - _Requirements: 3.9, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 12.1, 12.4, 12.5, 14.1, 14.4_

  - [x] 12.6 Clip editor UI and entry points (§10.1, §10.3)
    - `static/js/editor.js` and `static/js/preview.js`, following the mockup chosen in 12.1:
      - preview, timeline and handles (arrow keys move 0.1 s, Shift+arrow 1 s);
      - transcript word cuts and word edits;
      - captions, hook, layout and crop drag, music (cleared tracks only) and cover panels;
      - undo and redo of up to 200 steps;
      - render with progress;
      - warnings for platform length limits, already-published posts, and replacing uploaded videos.
    - On the existing clip cards and in the library, add an "Edit" button and a status chip for each platform's post.
    - Verify: `tests/ui/test_editor.py` checks:
      - `preview.js` time mapping matches `media/keep.py` on fixture specs, evaluated in the page;
      - a handle move updates the preview within 200 ms;
      - deleting words cuts them and editing a word changes it;
      - undo and redo work;
      - render shows progress;
      - each warning appears;
      - "Edit" opens the editor.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 8.6, N5.1, N5.2_

  - [x] 12.7 Accessibility checks and a fresh-context usability review
    - Keyboard-only UI tests for the queue, editor and setup screens:
      - focus is visible and every control has a label;
      - status changes are announced;
      - text-bearing buttons use `--terra-hover`.
    - Run a usability review in a sub-agent with no project memory (frontend-design-workflow phase 4). Fix what it finds, then attach a screenshot of each new screen (at most 1500 px wide).
    - Verify: `tests/ui/test_a11y.py` passes and the review's findings are resolved.
    - _Requirements: N5.1, N5.2_

- [x] 13. Finish

  - [x] 13.1 Benchmark script (§18)
    - `scripts/bench_autopilot.py --source <file> [--fake-ai] [--quick]`:
      - times each stage of `process.run` on a scratch data directory;
      - re-renders one 60 s clip;
      - measures the 95th-percentile latency of `/api/autopilot/posts` and `/counts` while a job and a render run;
      - prints the results against the N1.1, N1.2 and N1.4 budgets.
    - Verify:
      - `tests/test_bench.py` runs `--quick --fake-ai` on a generated 30 s source;
      - if a real 60-minute recording is available, run the script on it once with `--fake-ai` and report the numbers to the user.
    - _Requirements: N1.1, N1.2, N1.4_

  - [x] 13.2 Traceability and full regression
    - Add `tests/test_traceability.py`. It reads every requirement ID from `requirements.md` and fails if any ID has no `req("<id>")` tag in `tests/` (including `tests/ui/`).
    - Verify:
      - `.venv/bin/python -m pytest -q` and `.venv/bin/python -m pytest -q -m ui tests/ui` both pass;
      - `bash -n setup.sh start.sh scripts/fetch_models.sh` passes.
    - _Requirements: N7.1_
