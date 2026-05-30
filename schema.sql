-- Neon Auth (Better Auth) manages neon_auth.user automatically.
-- Note: "user" is a SQL reserved word, so it must always be quoted.

-- ── Band / Collaboration ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bands (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    invite_token TEXT        UNIQUE NOT NULL DEFAULT gen_random_uuid()::text,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per-member average rating required to approve a song; threshold = ceil(band_size * approval_factor).
-- Default 3.5 preserves the original hardcoded behavior.
ALTER TABLE bands ADD COLUMN IF NOT EXISTS approval_factor NUMERIC NOT NULL DEFAULT 3.5;

CREATE TABLE IF NOT EXISTS band_members (
    band_id   UUID NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
    user_id   UUID NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (band_id, user_id)
);

CREATE TABLE IF NOT EXISTS song_proposals (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    band_id     UUID        NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
    song_id     TEXT        NOT NULL,
    proposed_by UUID        REFERENCES neon_auth."user"(id) ON DELETE SET NULL,
    status      TEXT        NOT NULL DEFAULT 'pending',
    score       INTEGER     NOT NULL DEFAULT 2,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS song_votes (
    proposal_id UUID NOT NULL REFERENCES song_proposals(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    vote        TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (proposal_id, user_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    proposal_id UUID        REFERENCES song_proposals(id) ON DELETE CASCADE,
    type        TEXT        NOT NULL DEFAULT 'new_proposal',
    details     JSONB,
    read        BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Safe column additions for existing databases
ALTER TABLE song_votes    ADD COLUMN IF NOT EXISTS reason  TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS details JSONB;

-- Band's shared set list (separate from per-user set_list_songs)
CREATE TABLE IF NOT EXISTS band_set_list_songs (
    band_id  UUID    NOT NULL REFERENCES bands(id) ON DELETE CASCADE,
    song_id  TEXT    NOT NULL,
    position INTEGER NOT NULL,
    plays    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (band_id, song_id)
);
CREATE INDEX IF NOT EXISTS bsls_band_position_idx ON band_set_list_songs (band_id, position);

-- ── Songs ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS songs (
    id              TEXT        NOT NULL,
    user_id         UUID        NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    duration_raw    TEXT        NOT NULL DEFAULT '',
    duration_sec    INTEGER     NOT NULL DEFAULT 0,
    plays           INTEGER     NOT NULL DEFAULT 1,
    artist          TEXT        NOT NULL DEFAULT '',
    status          TEXT        NOT NULL DEFAULT 'For Consideration',
    tuning          TEXT,
    recorded_tuning TEXT,
    our_tuning      TEXT,
    album_art       TEXT,
    spotify_url     TEXT,
    youtube_link    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, user_id)
);
-- Add band_id if it doesn't exist yet (safe to run on existing databases)
ALTER TABLE songs ADD COLUMN IF NOT EXISTS band_id UUID REFERENCES bands(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS songs_user_idx ON songs (user_id);
CREATE INDEX IF NOT EXISTS songs_band_idx ON songs (band_id);

-- One set list per user (one active list at a time)
CREATE TABLE IF NOT EXISTS set_lists (
    user_id    UUID PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ordered songs within the set list
CREATE TABLE IF NOT EXISTS set_list_songs (
    user_id  UUID    NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    song_id  TEXT    NOT NULL,
    position INTEGER NOT NULL,
    plays    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, song_id),
    FOREIGN KEY (song_id, user_id) REFERENCES songs(id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sls_user_position_idx ON set_list_songs (user_id, position);

-- User-added tuning values (defaults are always merged in by the API)
CREATE TABLE IF NOT EXISTS user_tunings (
    user_id UUID NOT NULL REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    tuning  TEXT NOT NULL,
    PRIMARY KEY (user_id, tuning)
);

-- ── User Profile & Preferences ──────────────────────────────────────────────────
-- Neon Auth (Better Auth) owns neon_auth."user"; app-side identity/prefs live here.

-- Display name + band roles/instruments (profile-display only, no functional gating).
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id      UUID PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    display_name TEXT,
    roles        TEXT[]      NOT NULL DEFAULT '{}',   -- e.g. {Guitar,Vocals}
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-user notification toggles. A missing row means all-on (handled via COALESCE).
CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id         UUID PRIMARY KEY REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    new_proposal    BOOLEAN NOT NULL DEFAULT true,  -- "a song needs your vote"
    proposal_failed BOOLEAN NOT NULL DEFAULT true,
    song_archived   BOOLEAN NOT NULL DEFAULT true
);
