-- Neon Auth (Better Auth) manages neon_auth.user automatically.
-- Note: "user" is a SQL reserved word, so it must always be quoted.

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
CREATE INDEX IF NOT EXISTS songs_user_idx ON songs (user_id);

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
