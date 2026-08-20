#!/usr/bin/env python3
    -- Table 1: User Settings & Profile Data (Cookie, Caption, Cover, DP, Bio, Username)
CREATE TABLE IF NOT EXISTS instagram_settings (
    owner_id TEXT PRIMARY KEY,
    ig_cookie TEXT,
    ig_username TEXT,
    auto_caption TEXT,
    thumbnail_file_id TEXT,
    dp_file_id TEXT,
    bio_text TEXT,
    bio_link TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Table 2: Video Links Storage (Max 50 links per user)
CREATE TABLE IF NOT EXISTS instagram_video_links (
    id UUID DEFAULT extensions.uuid_generate_v4() PRIMARY KEY,
    owner_id TEXT REFERENCES instagram_settings(owner_id) ON DELETE CASCADE,
    video_link TEXT NOT NULL,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);
