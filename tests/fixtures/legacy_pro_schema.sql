-- Tables created by the removed /api/pro modules (presets.py, batch.py,
-- analytics.py, scheduler_advanced.py), copied from clipmaker.db on 9 Oct 2026.
-- Used by tests/test_migrations.py to build a database in today's shape.
CREATE TABLE presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    user_id TEXT,
    created_at REAL,
    settings_json TEXT NOT NULL
);
CREATE TABLE batch_queue (
    id TEXT PRIMARY KEY,
    batch_name TEXT,
    user_id TEXT,
    created_at REAL,
    status TEXT,
    current_index INTEGER,
    total_videos INTEGER,
    videos_json TEXT,
    settings_json TEXT,
    completed_jobs_json TEXT,
    error TEXT
);
CREATE TABLE clip_analytics (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    clip_index INTEGER,
    user_id TEXT,
    platform TEXT,
    video_url TEXT,
    title TEXT,
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    watch_time_seconds INTEGER DEFAULT 0,
    engagement_rate REAL DEFAULT 0,
    created_at REAL,
    last_updated REAL
);
CREATE TABLE analytics_snapshots (
    id TEXT PRIMARY KEY,
    clip_id TEXT,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    timestamp REAL,
    FOREIGN KEY(clip_id) REFERENCES clip_analytics(id)
);
CREATE TABLE hashtag_performance (
    hashtag TEXT,
    user_id TEXT,
    total_uses INTEGER DEFAULT 0,
    avg_views REAL DEFAULT 0,
    avg_engagement REAL DEFAULT 0,
    last_used REAL,
    PRIMARY KEY(hashtag, user_id)
);
CREATE TABLE title_patterns (
    pattern TEXT,
    user_id TEXT,
    usage_count INTEGER DEFAULT 0,
    avg_views REAL DEFAULT 0,
    avg_engagement REAL DEFAULT 0,
    PRIMARY KEY(pattern, user_id)
);
CREATE TABLE time_slot_performance (
    user_id TEXT,
    platform TEXT,
    day_of_week INTEGER,
    hour INTEGER,
    total_posts INTEGER DEFAULT 0,
    avg_views REAL DEFAULT 0,
    avg_engagement REAL DEFAULT 0,
    PRIMARY KEY(user_id, platform, day_of_week, hour)
);
CREATE TABLE upload_queue (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    job_id TEXT,
    clip_index INTEGER,
    platform TEXT,
    scheduled_time REAL,
    title TEXT,
    description TEXT,
    tags_json TEXT,
    thumbnail_path TEXT,
    video_path TEXT,
    status TEXT,
    uploaded_url TEXT,
    error TEXT,
    created_at REAL
);
