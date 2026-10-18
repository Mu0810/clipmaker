# Deploying Clip Maker publicly

The app ships as a single-user **localhost** tool. Going multi-user/public is a
spectrum. This guide covers what's already built, what you must configure, and
the steps that are inherently external (account/app registration, app review,
provisioned infrastructure) and therefore can't be done from the codebase.

## Deployment modes

| Mode | Who can reach it | What's needed |
|------|------------------|---------------|
| **Localhost** (default) | you, on your machine | nothing; `CLIPMAKER_AUTH` unset |
| **Invite-only / trusted group** | people you give logins | `CLIPMAKER_AUTH=1` + a reverse proxy with TLS |
| **Open public signup** | anyone | everything below: per-user OAuth + Postgres + object storage + distributed workers + legal |

Built and verified already: authentication, per-user data isolation
(jobs/clips/schedules), SSRF/URL allowlist, upload size limits, bounded job
queue with durable progress, and per-user AI/monetization settings.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `CLIPMAKER_AUTH` | off | `1` to require login and scope data per user |
| `CLIPMAKER_SECURE_COOKIE` | off | `1` to mark the session cookie Secure (set behind HTTPS) |
| `CLIPMAKER_WORKERS` | `2` | concurrent processing jobs |
| `CLIPMAKER_MAX_UPLOAD_MB` | `1024` | max uploaded video size |
| `CLIPMAKER_MAX_MUSIC_MB` | `50` | max uploaded music track size |
| `CLIPMAKER_ALLOW_ANY_SITE` | off | `1` to allow non-YouTube source URLs (internal IPs still blocked) |
| `CLIPMAKER_GOOGLE_CLIENT_SECRET` | — | path to a Google **web** OAuth client JSON (enables per-user YouTube) |
| `CLIPMAKER_OAUTH_REDIRECT` | — | the OAuth callback URL registered in Google |
| `KIMCHI_API_KEY` | — | shared AI key for localhost mode (per-user keys are set in the UI when auth is on) |

## Run in production

System packages the app needs on Linux (Docker example):

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libfribidi0 fonts-noto-core tzdata curl \
 && rm -rf /var/lib/apt/lists/*
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt && bash scripts/fetch_models.sh
```

`libfribidi0` and `fonts-noto-core` let captions render Hindi, Arabic, Bengali
and similar scripts; `tzdata` provides time zones for the posting planner;
`scripts/fetch_models.sh` downloads the checksum-verified face-detection model.

Do **not** use `--reload`. Run multiple uvicorn workers behind a TLS reverse
proxy (nginx/Caddy):

```bash
CLIPMAKER_AUTH=1 CLIPMAKER_SECURE_COOKIE=1 \
  .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 4
```

> Note: with multiple uvicorn workers, the in-process job queue runs **per
> worker** — fine for one machine, but for true horizontal scaling move to a
> shared queue (see "Distributed workers"). Job *state* already lives in the DB,
> so the web and worker processes can be separate.

## Per-user YouTube OAuth (built — needs your Google project)

The code is in `youtube_oauth.py` + `/api/youtube/oauth/{start,callback}`, and
activates only when the two env vars are set.

1. <https://console.cloud.google.com/> → create a project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → **External**; add scopes `youtube.upload` and
   `youtube.readonly`; add test users while unverified.
4. Credentials → Create credentials → **OAuth client ID** → **Web application**.
   Add an authorized redirect URI: `https://YOURHOST/api/youtube/oauth/callback`.
   Download the JSON.
5. Set `CLIPMAKER_GOOGLE_CLIENT_SECRET=/path/to/web_client.json` and
   `CLIPMAKER_OAUTH_REDIRECT=https://YOURHOST/api/youtube/oauth/callback`.
6. **App verification:** to allow *anyone* (not just test users) to connect,
   Google requires app verification (privacy policy, scope justification,
   possibly a security assessment for sensitive scopes). Until verified, only
   added test users can connect. This step is Google's, not the code's.

Per-user tokens are stored in `user_settings`; uploads go to each user's own
channel. **Quota:** ~6 uploads/day per project by default — request a quota
increase for real volume.

## Instagram (honest status)

There is **no compliant public path** with the current `instagram_upload.py`
(instagrapi / private API) — it's against Instagram's ToS and unsafe at scale.
A public build must use the **Instagram Graph API** (Content Publishing), which
requires:
- a Meta app + Facebook Login, business verification;
- users connecting **Business/Creator** IG accounts linked to a Facebook Page;
- **Meta App Review** for `instagram_content_publish` (Meta approves it).

That's an external process I can't perform. The per-user token slot
(`user_settings`) is ready to hold Graph tokens once the app is approved; the
publishing calls would replace `instagram_upload.upload_reel`.

## Postgres (#5b — needs a database)

All DB access is centralized in `db.py`. To switch from SQLite:

1. `pip install "psycopg[binary]"`.
2. In `db.py`, replace `_conn()` to use `psycopg.connect(os.environ["DATABASE_URL"])`
   and set the row factory to dict rows.
3. SQL dialect changes (search/replace in `db.py`):
   - `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (...) DO UPDATE`
   - `?` placeholders → `%s`
   - `PRAGMA table_info(...)` migration → use `information_schema.columns`
     (or manage schema with Alembic).
4. Point `DATABASE_URL` at your Postgres and run.

Everything above `db.py` is unchanged.

## Object storage (#5b — needs a bucket)

Outputs live under `output/<job_id>/`. For multi-machine / durable storage:

1. Add a `storage.py` with `put/open/url/delete`; local-disk backend by default,
   S3/GCS backend behind an env flag.
2. `pipeline.py` writes to a temp dir, then uploads artifacts to the bucket.
3. `/api/clip` returns a short-lived **presigned URL** (redirect) instead of
   streaming from local disk.
4. Add lifecycle rules / per-user quotas to control cost.

## Distributed workers (#5b — needs Redis)

The seam is `app.py`'s `_QUEUE.put(...)` + `_worker_loop`. To scale across
processes/machines:

1. `pip install rq redis` (or Celery).
2. Replace the in-process queue with an RQ queue; run `rq worker` processes
   separately from the web server.
3. Workers already report progress through the DB (`db.update_progress`) and,
   with object storage, read/write artifacts from the bucket — so web and
   workers fully decouple.

## Legal / content policy (do this before public signup)

- Restrict sources to content the user **owns** (their uploads or their own
  channel) rather than "download any YouTube link" — operating a public
  re-upload service is a copyright/DMCA liability for **you**.
- Publish Terms of Service + Privacy Policy; register a DMCA agent and a
  takedown process.
- Encrypt secrets at rest (tokens/keys in `user_settings`).
