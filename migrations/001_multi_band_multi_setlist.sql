-- Migration 001: Multi-band, multi-setlist data model
--
-- What this changes:
--   songs      — new UUID primary key; external_id keeps the old Spotify/custom ID;
--                ownership moves to either band_id OR user_id (not both); plays removed
--   setlists   — new table replacing set_lists + band_set_list_songs;
--                named, multiple per band or user
--   setlist_songs — new table replacing set_list_songs + band_set_list_songs;
--                   references setlists and songs by UUID
--   song_proposals.song_id — updated from plain TEXT to a proper UUID reference
--
-- Safe to run on the existing database. Wrapped in a transaction — if anything
-- fails, the entire migration is rolled back and the database is left unchanged.

BEGIN;

-- ── 1. Temporary mapping table (personal songs only) ─────────────────────────
-- Gives each existing PERSONAL (old_text_id, user_id) pair a fresh UUID so we
-- can re-point the personal setlist at the new song rows. Band songs are handled
-- separately in step 2 (they get de-duplicated), and the tables that reference
-- band songs are re-pointed by joining on (band_id, external_id) instead.

CREATE TEMP TABLE _song_id_map (
    old_id      TEXT NOT NULL,
    old_user_id UUID NOT NULL,
    new_id      UUID NOT NULL DEFAULT gen_random_uuid(),
    PRIMARY KEY (old_id, old_user_id)
);

INSERT INTO _song_id_map (old_id, old_user_id)
SELECT id, user_id FROM songs WHERE band_id IS NULL;

-- ── 2. New songs table ────────────────────────────────────────────────────────
-- Key differences from the old table:
--   • id is a proper UUID (was a composite key of text id + user_id)
--   • external_id stores the old text id (Spotify track ID, or the generated
--     ID for custom songs — NULL if there was no meaningful external reference)
--   • exactly one of band_id / user_id must be set (enforced by CHECK constraint)
--   • added_by records which user originally added the song
--   • plays removed (it belongs on setlist_songs, where it already also lived)

CREATE TABLE songs_new (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT,
    band_id         UUID        REFERENCES bands(id)              ON DELETE CASCADE,
    user_id         UUID        REFERENCES neon_auth."user"(id)   ON DELETE CASCADE,
    added_by        UUID        REFERENCES neon_auth."user"(id)   ON DELETE SET NULL,
    name            TEXT        NOT NULL,
    artist          TEXT        NOT NULL DEFAULT '',
    duration_raw    TEXT        NOT NULL DEFAULT '',
    duration_sec    INTEGER     NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'For Consideration',
    tuning          TEXT,
    recorded_tuning TEXT,
    our_tuning      TEXT,
    album_art       TEXT,
    spotify_url     TEXT,
    youtube_link    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT song_has_one_owner CHECK (
        (band_id IS NOT NULL AND user_id IS NULL) OR
        (band_id IS NULL     AND user_id IS NOT NULL)
    )
);

-- 2a. Personal songs — one row per (user_id, song), using the mapped UUIDs.
INSERT INTO songs_new (
    id, external_id, band_id, user_id, added_by,
    name, artist, duration_raw, duration_sec, status,
    tuning, recorded_tuning, our_tuning,
    album_art, spotify_url, youtube_link, created_at
)
SELECT
    m.new_id,
    s.id,        -- old text ID → external_id (Spotify ID or custom-song generated ID)
    NULL,        -- personal song: no band
    s.user_id,
    s.user_id,   -- they added their own personal song
    s.name, s.artist, s.duration_raw, s.duration_sec, s.status,
    s.tuning, s.recorded_tuning, s.our_tuning,
    s.album_art, s.spotify_url, s.youtube_link,
    s.created_at
FROM songs s
JOIN _song_id_map m ON m.old_id = s.id AND m.old_user_id = s.user_id
WHERE s.band_id IS NULL;

-- 2b. Band songs — DE-DUPLICATE. In the old model each member who added a song
-- got their own row, so the same song could appear several times in one band.
-- The new model has ONE shared record per song per band. DISTINCT ON keeps the
-- earliest-created row as the representative; added_by becomes whoever added it
-- first. (Per-member opinions still live in song_votes, untouched.)
INSERT INTO songs_new (
    id, external_id, band_id, user_id, added_by,
    name, artist, duration_raw, duration_sec, status,
    tuning, recorded_tuning, our_tuning,
    album_art, spotify_url, youtube_link, created_at
)
SELECT DISTINCT ON (s.band_id, s.id)
    gen_random_uuid(),
    s.id,        -- old text ID → external_id
    s.band_id,
    NULL,        -- band song: no individual owner
    s.user_id,   -- earliest adder
    s.name, s.artist, s.duration_raw, s.duration_sec, s.status,
    s.tuning, s.recorded_tuning, s.our_tuning,
    s.album_art, s.spotify_url, s.youtube_link,
    s.created_at
FROM songs s
WHERE s.band_id IS NOT NULL
ORDER BY s.band_id, s.id, s.created_at ASC;

-- ── 3. New setlists table ─────────────────────────────────────────────────────
-- Replaces both set_lists (one-per-user) and band_set_list_songs (one-per-band).
-- A setlist has a name and belongs to either a band or a user — never both.

CREATE TABLE setlists (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT        NOT NULL DEFAULT 'Main Set',
    band_id    UUID        REFERENCES bands(id)             ON DELETE CASCADE,
    user_id    UUID        REFERENCES neon_auth."user"(id)  ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT setlist_has_one_owner CHECK (
        (band_id IS NOT NULL AND user_id IS NULL) OR
        (band_id IS NULL     AND user_id IS NOT NULL)
    )
);

-- ── 4. New setlist_songs table ────────────────────────────────────────────────
-- Replaces set_list_songs and band_set_list_songs.
-- References setlists and songs_new by UUID.

CREATE TABLE setlist_songs (
    setlist_id UUID    NOT NULL REFERENCES setlists(id)   ON DELETE CASCADE,
    song_id    UUID    NOT NULL REFERENCES songs_new(id)  ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    plays      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (setlist_id, song_id)
);

-- ── 5. Migrate personal setlists ─────────────────────────────────────────────
-- One named setlist per user who had a set_list row.

INSERT INTO setlists (id, name, user_id, created_at, updated_at)
SELECT gen_random_uuid(), 'Main Set', sl.user_id, now(), sl.updated_at
FROM set_lists sl;

-- Personal setlist songs — only include songs that are still personal (not
-- migrated to a band). Songs that were migrated to a band are covered below.
INSERT INTO setlist_songs (setlist_id, song_id, position, plays)
SELECT
    sl_new.id,
    m.new_id,
    sls.position,
    sls.plays
FROM set_list_songs sls
JOIN songs s
    ON s.id = sls.song_id AND s.user_id = sls.user_id
JOIN _song_id_map m
    ON m.old_id = sls.song_id AND m.old_user_id = sls.user_id
JOIN setlists sl_new
    ON sl_new.user_id = sls.user_id AND sl_new.band_id IS NULL
WHERE s.band_id IS NULL;  -- personal songs only

-- ── 6. Migrate band setlists ──────────────────────────────────────────────────
-- One named setlist per band that had band_set_list_songs rows.

INSERT INTO setlists (id, name, band_id, created_at, updated_at)
SELECT DISTINCT gen_random_uuid(), 'Main Set', bsls.band_id, now(), now()
FROM band_set_list_songs bsls;

INSERT INTO setlist_songs (setlist_id, song_id, position, plays)
SELECT
    sl_new.id,
    sn.id,
    bsls.position,
    bsls.plays
FROM band_set_list_songs bsls
JOIN songs_new sn
    ON sn.external_id = bsls.song_id AND sn.band_id = bsls.band_id
JOIN setlists sl_new
    ON sl_new.band_id = bsls.band_id AND sl_new.user_id IS NULL;

-- ── 7. Update song_proposals to reference new song UUIDs ─────────────────────
-- song_proposals.song_id was plain TEXT with no formal link to songs.
-- We add a proper UUID column, populate it, then replace the old column.

ALTER TABLE song_proposals ADD COLUMN song_id_new UUID;

UPDATE song_proposals sp
SET song_id_new = sn.id
FROM songs_new sn
WHERE sn.external_id = sp.song_id
  AND sn.band_id = sp.band_id;

-- Proposals for songs that no longer exist in the new table are orphaned.
-- Remove them cleanly rather than leaving NULL references.
DELETE FROM song_proposals WHERE song_id_new IS NULL;

ALTER TABLE song_proposals DROP COLUMN song_id;
ALTER TABLE song_proposals RENAME COLUMN song_id_new TO song_id;
ALTER TABLE song_proposals ALTER COLUMN song_id SET NOT NULL;
ALTER TABLE song_proposals ADD CONSTRAINT fk_proposals_song
    FOREIGN KEY (song_id) REFERENCES songs_new(id) ON DELETE CASCADE;

-- ── 8. Drop old tables ────────────────────────────────────────────────────────
-- Order matters: tables with foreign keys referencing songs must go first.

DROP TABLE set_list_songs;
DROP TABLE set_lists;
DROP TABLE band_set_list_songs;
DROP TABLE songs;

-- ── 9. Rename songs_new → songs, add indexes ──────────────────────────────────

ALTER TABLE songs_new RENAME TO songs;

CREATE INDEX songs_user_idx        ON songs (user_id);
CREATE INDEX songs_band_idx        ON songs (band_id);
CREATE INDEX songs_external_id_idx ON songs (external_id);
CREATE INDEX setlist_songs_position_idx ON setlist_songs (setlist_id, position);

-- ── 10. Duplicate guards ──────────────────────────────────────────────────────
-- The same external song can appear at most once per library. These are PARTIAL
-- unique indexes — "unique, but only for rows where the WHERE clause is true":
--   • only enforced when BOTH an owner and an external_id exist, so
--   • different bands can each hold the same song (different band_id), and
--   • fully custom songs (external_id is empty) are never auto-deduplicated —
--     two hand-entered songs with the same name are allowed.

CREATE UNIQUE INDEX songs_band_external_uniq
    ON songs (band_id, external_id)
    WHERE band_id IS NOT NULL AND external_id IS NOT NULL;

CREATE UNIQUE INDEX songs_user_external_uniq
    ON songs (user_id, external_id)
    WHERE user_id IS NOT NULL AND external_id IS NOT NULL;

COMMIT;
