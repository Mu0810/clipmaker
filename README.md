# 🎬 Clip Maker Pro

**Turn long videos into viral shorts with AI-powered automation**

A complete professional platform for content creators, agencies, and social media managers.

---

## ✨ Features

### Core Features
- 🎥 **Smart clip extraction** - AI identifies viral moments
- 📝 **Auto-captions** - Karaoke, word-by-word, or phrase styles
- 🎵 **Background music** - Trending audio integration
- 🖼️ **Thumbnail generation** - Eye-catching auto-generated previews
- 📊 **Viral scoring** - Rate clips by engagement potential
- 🎨 **Custom branding** - Watermarks and styling

### Professional Features (NEW)
- 📦 **Batch processing** - Queue 100+ videos overnight
- 🎯 **Template system** - Save & reuse presets instantly
- 📈 **Analytics dashboard** - Track views, engagement, hashtags
- ⏰ **Smart scheduling** - AI-powered best posting times
- ✂️ **Clip editor** - Trim, adjust, create custom thumbnails
- 🚀 **Upload queue** - Auto-post at optimal times

### Platform Support
- ✅ YouTube (direct upload)
- ✅ Instagram (automated posting)
- ✅ TikTok (video export)
- ✅ Twitter/X (video export)

---

## 🚀 Quick Start

### One-Command Setup

```bash
cd ~/clipmaker
chmod +x setup.sh
./setup.sh
```

### Manual Setup

```bash
# Install Python 3.12
brew install python@3.12

# Create virtual environment
cd ~/clipmaker
rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Start server
uvicorn app:app --reload --port 8000
```

**Then open:** http://localhost:8000

---

## 📸 Screenshots

### Before (Generic Template)
- Dark purple/pink theme
- Generic SaaS appearance
- Harsh contrasts

### After (Warm Editorial Design)
- Warm cream background with terracotta accents
- Editorial serif typography (Fraunces + Inter)
- Professional, distinctive aesthetic
- Pill-shaped buttons and refined spacing

---

## 🎯 Use Cases

### Content Creators
- Process weekly podcast episodes in bulk
- Find viral moments automatically
- Track which clips perform best
- Post at optimal times for your audience

### Social Media Managers
- Manage multiple client accounts
- Use templates for consistent branding
- Analytics per client
- Scheduled uploads across platforms

### Agencies
- Batch process client content
- White-label potential
- Performance reporting
- Team collaboration ready

---

## 📊 Professional Workflows

### Workflow 1: Weekly Content Creation
1. Create preset with your settings
2. Queue 10 videos for batch processing
3. Review analytics from last week
4. Edit best clips (trim, thumbnails)
5. Schedule uploads for peak times
6. Clips auto-post throughout the week

**Time:** 10 hours → 2 hours/week

### Workflow 2: Data-Driven Optimization
1. Generate clips with different presets
2. Post A/B versions
3. Track performance in analytics
4. See which style works best
5. Update presets based on data
6. Continuous improvement

---

## 🏗️ Architecture

### Backend Stack
- **FastAPI** - Modern async Python framework
- **SQLite** - Embedded database (zero config)
- **yt-dlp** - Video downloading
- **ffmpeg** - Video processing
- **faster-whisper** - Speech transcription
- **Pillow** - Image manipulation

### Database
Single SQLite database (`clipmaker.db`): jobs, schedules, users, sessions, user_settings

---

## 🔌 API Reference

### Core Endpoints

```bash
# Process video
POST /api/process
{
  "url": "https://youtube.com/watch?v=...",
  "preset_id": "optional-preset-id"
}

# Check status
GET /api/status/{job_id}

# List library
GET /api/library

# Download clip
GET /api/download/{job_id}/{clip_index}
```

---

## 🎨 Design System

### Colors
- **Background**: Warm cream `#F7F4EF`
- **Surface**: Off-white `#FBF9F5`, `#FFFFFF`
- **Accent**: Terracotta `#C4612F` (hover: `#A94E22`)
- **Borders**: Warm hairline `#E7E1D7`
- **Text**: Dark charcoal `#1F2421`, Muted `#5C635D`

### Typography
- **Headings**: Fraunces (display serif) with italic emphasis
- **Body**: Inter 300-400 (humanist sans)
- **Buttons**: Inter 400

### Components
- Pill buttons (999px border-radius)
- Soft hover lifts (1-3px)
- Warm hairline borders
- Gentle shadows
- Eyebrow pills for labels

---

## 📚 Documentation

- **[PRO_FEATURES.md](PRO_FEATURES.md)** - Professional features guide
- **[INSTALL.md](INSTALL.md)** - Installation & usage
- **[COMPARISON.md](COMPARISON.md)** - Before/after comparison
- **[UI_REDESIGN.md](UI_REDESIGN.md)** - Design documentation
- **[UPGRADE_SUMMARY.md](UPGRADE_SUMMARY.md)** - Dependency notes

---

## 🔧 Configuration

### Environment Variables

```bash
# Upload limits
CLIPMAKER_MAX_UPLOAD_MB=1024
CLIPMAKER_MAX_MUSIC_MB=50

# Authentication
CLIPMAKER_AUTH_MODE=off          # off, first, open
CLIPMAKER_SECURE_COOKIES=false

# AI features
ANTHROPIC_API_KEY=your_key
```

### Requirements

- **Python**: 3.12 or 3.10 (not 3.14)
- **ffmpeg**: For video processing
- **Storage**: 5GB+ recommended for output files

---

## 🤖 Autopilot setup

Autopilot publishes only through the platforms' official APIs. Each platform needs a one-time setup; the readiness checklist in the app shows what is still missing.

### YouTube

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **YouTube Data API v3**.
2. Create an **OAuth client**:
   - On this Mac (single-user mode): a **Desktop app** client. Download its JSON and add it in the app (it is saved as `client_secret.json`).
   - With sign-in on (multi-user mode): a **Web application** client. Set `CLIPMAKER_GOOGLE_CLIENT_SECRET` to its JSON file and `CLIPMAKER_OAUTH_REDIRECT` to `https://<your host>/api/youtube/oauth/callback`, and register that URL in Google Cloud.
3. Click **Connect for Autopilot**. Google asks for one extra permission beyond the manual upload button's: **Manage your YouTube videos**. Autopilot needs it to upload, schedule, update or remove the videos it posts, and to read their status and views.
4. Confirm your channel's **audience** (made for kids or not). Autopilot never guesses this.
5. Two things the app can't check, so you declare them:
   - **OAuth app in production.** While the Google Cloud app is in "Testing", Google ends the sign-in every 7 days. Publish the app in the OAuth consent screen to avoid weekly reconnects.
   - **API audit passed.** Until YouTube audits your Google Cloud project, videos uploaded through the API stay private. Apply through YouTube's [API audit form](https://support.google.com/youtube/contact/yt_api_form).

Limits: YouTube allows 100 API uploads a day per Google Cloud project, counted from midnight Pacific time. Clip Maker counts them and won't start the 101st.

Disconnecting revokes the access at Google, deletes the stored token, and makes unpublished posts wait until you reconnect. Videos already scheduled on YouTube still go live on time.

### Instagram

Autopilot posts Reels through Meta's Instagram API, signing in with Facebook Login for Business.

1. In the Instagram app, switch to a **professional account** (Business or Creator) and link it to a **Facebook Page** you manage.
2. In [Meta for Developers](https://developers.facebook.com/apps/), create an app of type **Business** and add **Facebook Login for Business**.
3. Under Facebook Login for Business → **Configurations**, create a configuration with the token type **User access token** and these permissions: `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement` and `business_management`. Note its **configuration id**.
4. Keep the app in **development mode**, with your Facebook account holding a role on it.
5. Add the app's settings:
   - On this Mac (single-user mode): enter the app id, app secret and configuration id in the app. They're saved to `meta_app.json`, readable by your account only. The sign-in returns to `http://localhost:8000/api/connections/instagram/callback`, which Meta allows for apps in development mode.
   - With sign-in on (multi-user mode): set `CLIPMAKER_META_APP_ID`, `CLIPMAKER_META_APP_SECRET`, `CLIPMAKER_META_CONFIG_ID` and `CLIPMAKER_META_REDIRECT` (`https://<your host>/api/connections/instagram/callback`), and add that address to the app's **Valid OAuth Redirect URIs**.
6. Click **Connect Instagram** and sign in with Facebook. If you manage several Pages with a linked Instagram account, choose the one Autopilot posts to.

**If the sign-in can't return to the app** (some developers report Meta refusing `localhost`), use **Paste a token**: in Meta's [Graph API Explorer](https://developers.facebook.com/tools/explorer/), choose your app, generate a **User Token** with the permissions above, and paste it into Clip Maker. It's exchanged for a 60-day token and checked the same way.

If something is missing, the app says what and how to fix it: an account that isn't professional or isn't linked to a Page, or a permission Meta didn't grant.

Limits: Meta caps API posts per account in a rolling 24 hours. Clip Maker reads the limit Meta reports, and a post that would pass it moves to the next free slot after the limit resets. Meta sign-ins last 60 days; you're asked to reconnect at least 7 days before access lapses.

Disconnecting deletes the stored Meta tokens and makes unpublished Instagram posts wait until you reconnect. To remove the app's access at Meta as well, remove it under Facebook's **Settings → Business integrations**. The password login used for manual Reels uploads is separate and keeps working. Autopilot never uses it, because password logins break Instagram's terms and risk blocks or bans.

---

## 📈 Performance

### Processing Speed
- **Clip extraction**: ~30s per minute of source video
- **Transcription**: ~5s per minute (with faster-whisper)
- **Caption burning**: ~10s per clip
- **Thumbnail generation**: <1s per clip

### Batch Processing
- **Concurrent**: 1 video at a time (CPU-bound)
- **Queue**: Unlimited size
- **Resume**: Automatic checkpoint recovery
- **Typical**: 50 videos overnight

### Analytics
- **Dashboard query**: <50ms
- **Storage**: ~1KB per clip
- **Snapshots**: Automatic cleanup (90+ days)

---

## 🛣️ Roadmap

### Phase 1: UI Implementation (Current)
- [x] Backend APIs complete
- [x] Database schema
- [x] UI redesign
- [ ] Preset interface
- [ ] Batch queue UI
- [ ] Analytics dashboard
- [ ] Calendar scheduler
- [ ] Clip editor timeline

### Phase 2: Intelligence
- [ ] AI title generation with A/B testing
- [ ] Automated trend detection
- [ ] Competitor analysis
- [ ] Content recommendations
- [ ] ROI tracking

### Phase 3: Collaboration
- [ ] Team workspaces
- [ ] Shared presets
- [ ] Role permissions
- [ ] Client portals
- [ ] White-label

### Phase 4: Integrations
- [ ] Webhooks
- [ ] Zapier
- [ ] Google Drive sync
- [ ] Slack notifications
- [ ] API marketplace

---

## 🤝 Contributing

This is currently a personal/commercial project. Future open-source release TBD.

---

## 📝 License

Proprietary - All rights reserved

---

## 🎯 Support

For questions or issues:
1. Check documentation in `/docs/`
2. Review API examples in `INSTALL.md`
3. Inspect database: `sqlite3 clipmaker.db`

---

## 🚀 Deployment

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### systemd

```bash
sudo systemctl enable clipmaker.service
sudo systemctl start clipmaker
```

### Nginx Reverse Proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

---

**Built with ❤️ for content creators who want to work smarter, not harder.**

🎬 **Clip Maker Pro** - Your viral content factory.
