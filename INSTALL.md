# Clip Maker - Installation & Usage Guide

## Quick Start (Python 3.12 or 3.10)

### Step 1: Install Python 3.12

Your current Python 3.14 is too new for some dependencies. Install Python 3.12:

```bash
brew install python@3.12
```

### Step 2: Create Virtual Environment

```bash
cd ~/clipmaker
rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3: Start the Server

```bash
.venv/bin/uvicorn app:app --reload --port 8000
```

Then open: **http://localhost:8000**

---

## What's New - Professional Features

Your Clip Maker now includes:

### ✅ **Batch Processing**
Queue multiple videos and process them overnight. Perfect for bulk content creation.

### ✅ **Template/Preset System**
Save your favorite settings (caption style, music, viral options) and reuse them with one click.

### ✅ **Analytics Dashboard**
Track views, engagement, and performance across all your clips. See which hashtags work best.

### ✅ **Smart Scheduling**
Get AI-powered recommendations for the best times to post based on your historical performance.

### ✅ **Clip Editor**
Trim videos, adjust captions, and create custom thumbnails before publishing.

### ✅ **Upload Queue**
Schedule clips to auto-upload at optimal times while you sleep.

---

## Architecture

### Backend Modules

**Core Features:**
- `app.py` - FastAPI server
- `pipeline.py` - Main processing pipeline
- `highlights.py` - Viral moment detection
- `captions.py` - Caption generation
- `transcribe.py` - Speech-to-text
- `editing.py` - Video editing
- `thumbnail.py` - Thumbnail generation
- `music.py` - Background music
- `db.py` - SQLite database

**Pro Features (NEW):**
- `presets.py` - Template system
- `batch.py` - Batch queue
- `analytics.py` - Performance tracking
- `scheduler_advanced.py` - Smart scheduling
- `editor.py` - Clip editing tools
- `api_pro.py` - Pro API endpoints

**Platform Integration:**
- `youtube_oauth.py` - YouTube OAuth
- `youtube_pw.py` - YouTube password auth
- `instagram_upload.py` - Instagram posting
- `autopilot/legacy.py` - Scheduled uploads (run by the Autopilot publisher)

**AI & Intelligence:**
- `llm.py` - LLM integration
- `monetize.py` - Monetization features
- `besttime.py` - Posting time optimization

### Database Structure

Single SQLite database (`clipmaker.db`) with tables:

**Core:**
- `jobs` - Processing jobs
- `schedules` - Scheduled uploads
- `users` - User accounts
- `sessions` - Login sessions
- `user_settings` - User preferences

**Pro:**
- `presets` - Saved templates
- `batch_queue` - Batch jobs
- `clip_analytics` - Performance metrics
- `analytics_snapshots` - Time-series data
- `hashtag_performance` - Hashtag stats
- `time_slot_performance` - Best posting times
- `upload_queue` - Scheduled uploads

---

## API Endpoints

### Core Endpoints

```
POST   /api/process              - Process a video URL
GET    /api/status/{job_id}      - Check job status
GET    /api/library              - List all jobs
DELETE /api/jobs/{job_id}        - Delete job
GET    /api/download/{job_id}/{clip_index} - Download clip
```

### Pro Endpoints

**Presets:**
```
POST   /api/pro/presets          - Create preset
GET    /api/pro/presets          - List presets
GET    /api/pro/presets/{id}     - Get preset
PATCH  /api/pro/presets/{id}     - Update preset
DELETE /api/pro/presets/{id}     - Delete preset
```

**Batch Processing:**
```
POST   /api/pro/batch            - Create batch
GET    /api/pro/batch            - List batches
GET    /api/pro/batch/{id}       - Get batch status
DELETE /api/pro/batch/{id}       - Cancel batch
```

**Analytics:**
```
POST   /api/pro/analytics/record         - Record metrics
GET    /api/pro/analytics/summary        - Dashboard data
GET    /api/pro/analytics/clip/{id}      - Clip metrics
GET    /api/pro/analytics/hashtags       - Top hashtags
GET    /api/pro/analytics/trends         - Trending patterns
```

**Scheduling:**
```
POST   /api/pro/schedule/upload          - Schedule upload
GET    /api/pro/schedule/best-times      - Get recommendations
GET    /api/pro/schedule/calendar        - View calendar
GET    /api/pro/schedule/list            - List scheduled
DELETE /api/pro/schedule/{id}            - Cancel upload
```

**Editor:**
```
POST   /api/pro/editor/trim              - Trim video
POST   /api/pro/editor/thumbnail         - Create thumbnail
GET    /api/pro/editor/duration          - Get duration
```

---

## Usage Examples

### Example 1: Process with Preset

```bash
# Create preset
curl -X POST http://localhost:8000/api/pro/presets \
  -H "Content-Type: application/json" \
  -d '{
    "name": "YouTube Shorts - Viral",
    "settings": {
      "caption_style": "karaoke",
      "music": true,
      "viral_mode": true,
      "max_duration": 60
    }
  }'

# Use preset
curl -X POST http://localhost:8000/api/process \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://youtube.com/watch?v=VIDEO_ID",
    "preset_id": "PRESET_ID_FROM_ABOVE"
  }'
```

### Example 2: Batch Process

```bash
curl -X POST http://localhost:8000/api/pro/batch \
  -H "Content-Type: application/json" \
  -d '{
    "batch_name": "Weekly Content",
    "videos": [
      {"url": "https://youtube.com/watch?v=VIDEO1", "name": "Part 1"},
      {"url": "https://youtube.com/watch?v=VIDEO2", "name": "Part 2"},
      {"url": "https://youtube.com/watch?v=VIDEO3", "name": "Part 3"}
    ],
    "settings": {
      "caption_style": "karaoke",
      "music": true
    }
  }'
```

### Example 3: Schedule Upload

```bash
# Get best times
curl http://localhost:8000/api/pro/schedule/best-times?platform=youtube

# Schedule for best time
curl -X POST http://localhost:8000/api/pro/schedule/upload \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "JOB_ID",
    "clip_index": 0,
    "platform": "youtube",
    "scheduled_time": 1721401200,
    "title": "My Viral Short",
    "description": "Check this out!",
    "tags": ["viral", "shorts"],
    "video_path": "/path/to/clip.mp4"
  }'
```

---

## Configuration

### Environment Variables

```bash
# Upload limits
CLIPMAKER_MAX_UPLOAD_MB=1024      # Max video upload size
CLIPMAKER_MAX_MUSIC_MB=50         # Max music file size

# Authentication (optional)
CLIPMAKER_AUTH_MODE=off           # off, first, open
CLIPMAKER_SECURE_COOKIES=false    # true for HTTPS

# LLM API (optional, for AI features)
ANTHROPIC_API_KEY=your_key_here
```

### File Structure

```
clipmaker/
├── app.py                    # Main FastAPI app
├── requirements.txt          # Dependencies
├── clipmaker.db             # SQLite database
├── output/                  # Processed clips
├── music/                   # Background music files
├── static/                  # Web UI
│   └── index.html          # Main interface
├── presets.py              # Pro: Templates
├── batch.py                # Pro: Batch queue
├── analytics.py            # Pro: Analytics
├── scheduler_advanced.py   # Pro: Smart scheduling
├── editor.py              # Pro: Clip editor
├── api_pro.py             # Pro: API router
├── PRO_FEATURES.md        # Pro features docs
└── INSTALL.md             # This file
```

---

## Troubleshooting

### Python Version Issues

If you see compilation errors about greenlet, pydantic-core, or av:
- You're using Python 3.14 which is too new
- Install Python 3.12: `brew install python@3.12`
- Recreate venv with 3.12 as shown in Step 2

### Missing ffmpeg

If video processing fails:
```bash
brew install ffmpeg
```

### Database Errors

If you get database errors, reinitialize:
```bash
rm clipmaker.db
python -c "import db; db.init()"
```

### Port Already in Use

If port 8000 is taken:
```bash
.venv/bin/uvicorn app:app --reload --port 8001
```

---

## Development

### Running in Dev Mode

```bash
# With auto-reload
.venv/bin/uvicorn app:app --reload --port 8000

# With debug logging
.venv/bin/uvicorn app:app --reload --log-level debug
```

### Database Inspection

```bash
sqlite3 clipmaker.db
.tables
.schema jobs
SELECT * FROM jobs;
```

### Adding New Features

1. Create module in root: `my_feature.py`
2. Add init function: `def init_tables():`
3. Import in `app.py`
4. Call init in startup
5. Add API endpoints in `api_pro.py`

---

## Production Deployment

### Using Docker (Recommended)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Using systemd

```ini
[Unit]
Description=Clip Maker
After=network.target

[Service]
Type=simple
User=clipmaker
WorkingDirectory=/home/clipmaker/clipmaker
ExecStart=/home/clipmaker/clipmaker/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

### Behind Nginx

```nginx
server {
    listen 80;
    server_name clips.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

---

## Support & Documentation

- **PRO_FEATURES.md** - Detailed pro feature documentation
- **UI_REDESIGN.md** - UI design documentation
- **UPGRADE_SUMMARY.md** - Dependency upgrade notes

Your Clip Maker is now a complete professional tool! 🎬✨
