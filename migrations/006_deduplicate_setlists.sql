-- Migration 006: Remove duplicate setlists
--
-- Before migration 004 was applied to production, _default_setlist_id's
-- SAVEPOINT fallback (INSERT without timing columns) ran on every app load,
-- creating a new empty setlist row whenever the timing-column INSERT failed.
-- This left users with many identically-named empty setlists.
--
-- Strategy: for each owner (band or solo user), keep the ONE setlist that has
-- the most songs (or the oldest if tied). Delete all other empty duplicates.
-- Setlists with songs are NEVER deleted regardless of rank.
--
-- Safe to run: CASCADE on setlist_songs means the delete propagates cleanly.
-- Idempotent: if no duplicates exist, the DELETE matches zero rows.

BEGIN;

WITH song_counts AS (
    SELECT setlist_id, COUNT(*) AS cnt
    FROM setlist_songs
    GROUP BY setlist_id
),
ranked AS (
    SELECT s.id,
           COALESCE(sc.cnt, 0) AS song_count,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(s.band_id::text, s.user_id::text)
               ORDER BY COALESCE(sc.cnt, 0) DESC, s.created_at ASC
           ) AS rn
    FROM setlists s
    LEFT JOIN song_counts sc ON sc.setlist_id = s.id
)
DELETE FROM setlists
WHERE id IN (
    SELECT id FROM ranked WHERE rn > 1 AND song_count = 0
);

COMMIT;
