-- Migration 007: Remove duplicate setlists that have songs
--
-- Migration 001 created one "Main Set" per band_set_list_songs ROW (a per-row
-- gen_random_uuid() defeated the intended SELECT DISTINCT — see the fix in
-- 001_multi_band_multi_setlist.sql). Migration 006 only removed *empty*
-- duplicates; the remaining dupes all carry songs (partial scatter-writes
-- caused by the non-deterministic default-setlist resolution).
--
-- Strategy: within each group of setlists sharing the same owner + name +
-- created_at (the tell-tale of the 001 bug — every clone shares one timestamp),
-- keep the row with the MOST songs (tie-break lowest id) and move every other
-- clone, plus its setlist_songs, into backup tables before deleting it.
--
-- Idempotent: matches zero rows on a clean DB. Backup tables are plain column
-- copies (no PK/FK/unique) so they survive parent deletion and tolerate re-runs.

BEGIN;

CREATE TABLE IF NOT EXISTS setlists_dedup_backup      (LIKE setlists);
CREATE TABLE IF NOT EXISTS setlist_songs_dedup_backup (LIKE setlist_songs);

CREATE TEMP TABLE _dedup_losers ON COMMIT DROP AS
WITH counts AS (
    SELECT setlist_id, COUNT(*) AS cnt FROM setlist_songs GROUP BY setlist_id
),
ranked AS (
    SELECT s.id,
           COUNT(*) OVER (PARTITION BY COALESCE(s.band_id::text, s.user_id::text),
                          s.name, s.created_at) AS dup_count,
           ROW_NUMBER() OVER (PARTITION BY COALESCE(s.band_id::text, s.user_id::text),
                              s.name, s.created_at
                              ORDER BY COALESCE(c.cnt, 0) DESC, s.id ASC) AS rn
    FROM setlists s
    LEFT JOIN counts c ON c.setlist_id = s.id
)
SELECT id FROM ranked WHERE dup_count > 1 AND rn > 1;

INSERT INTO setlist_songs_dedup_backup
SELECT ss.* FROM setlist_songs ss JOIN _dedup_losers l ON l.id = ss.setlist_id;

INSERT INTO setlists_dedup_backup
SELECT s.* FROM setlists s JOIN _dedup_losers l ON l.id = s.id;

DELETE FROM setlists WHERE id IN (SELECT id FROM _dedup_losers);

COMMIT;
