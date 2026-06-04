-- Migration 004: Add per-setlist timing settings and band-level defaults.
--
-- bands gets six default_* columns (seed values for new setlists).
-- setlists gets six settings columns (per-setlist, source of truth).
-- Existing setlists are backfilled: band-owned ones from their band's new defaults
-- (same hardcoded values), solo ones from the hardcoded defaults.
-- All column additions use IF NOT EXISTS so this is safe to re-run.

BEGIN;

-- ── Band-level default timing settings ───────────────────────────────────────
ALTER TABLE bands
    ADD COLUMN IF NOT EXISTS default_target_seconds        INTEGER NOT NULL DEFAULT 9000,
    ADD COLUMN IF NOT EXISTS default_warn_seconds          INTEGER NOT NULL DEFAULT 7200,
    ADD COLUMN IF NOT EXISTS default_song_buffer_seconds   INTEGER NOT NULL DEFAULT 45,
    ADD COLUMN IF NOT EXISTS default_tuning_change_seconds INTEGER NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS default_break_count           INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS default_break_seconds         INTEGER NOT NULL DEFAULT 0;

-- ── Per-setlist timing settings ───────────────────────────────────────────────
-- Nullable so an additive migration doesn't force a default; we backfill below.
ALTER TABLE setlists
    ADD COLUMN IF NOT EXISTS target_seconds        INTEGER,
    ADD COLUMN IF NOT EXISTS warn_seconds          INTEGER,
    ADD COLUMN IF NOT EXISTS song_buffer_seconds   INTEGER,
    ADD COLUMN IF NOT EXISTS tuning_change_seconds INTEGER,
    ADD COLUMN IF NOT EXISTS break_count           INTEGER,
    ADD COLUMN IF NOT EXISTS break_seconds         INTEGER;

-- Backfill band-owned setlists from their band's (newly-defaulted) columns.
UPDATE setlists sl
SET target_seconds        = COALESCE(sl.target_seconds,        b.default_target_seconds),
    warn_seconds          = COALESCE(sl.warn_seconds,          b.default_warn_seconds),
    song_buffer_seconds   = COALESCE(sl.song_buffer_seconds,   b.default_song_buffer_seconds),
    tuning_change_seconds = COALESCE(sl.tuning_change_seconds, b.default_tuning_change_seconds),
    break_count           = COALESCE(sl.break_count,           b.default_break_count),
    break_seconds         = COALESCE(sl.break_seconds,         b.default_break_seconds)
FROM bands b
WHERE sl.band_id = b.id;

-- Backfill remaining setlists (solo-owned, or any still NULL) with hardcoded defaults.
UPDATE setlists
SET target_seconds        = COALESCE(target_seconds,        9000),
    warn_seconds          = COALESCE(warn_seconds,          7200),
    song_buffer_seconds   = COALESCE(song_buffer_seconds,   45),
    tuning_change_seconds = COALESCE(tuning_change_seconds, 60),
    break_count           = COALESCE(break_count,           0),
    break_seconds         = COALESCE(break_seconds,         0);

COMMIT;
