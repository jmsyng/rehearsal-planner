-- Migration 008: Shows (gigs) that group multiple ordered setlists
--
-- A show is one performance night (e.g. a cover band playing three one-hour
-- sets at a bar). A setlist belongs to at most one show via setlists.show_id,
-- ordered within the show by setlists.position. show_id NULL = standalone set
-- (rehearsal lists, drafts). Deleting a show demotes its sets to standalone
-- (ON DELETE SET NULL) — song data is never destroyed.

BEGIN;

CREATE TABLE IF NOT EXISTS shows (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    band_id    UUID REFERENCES bands(id)            ON DELETE CASCADE,
    user_id    UUID REFERENCES neon_auth."user"(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    show_date  DATE,
    venue      TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT show_has_one_owner CHECK (
        (band_id IS NOT NULL AND user_id IS NULL) OR
        (band_id IS NULL     AND user_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS shows_band_idx ON shows (band_id);
CREATE INDEX IF NOT EXISTS shows_user_idx ON shows (user_id);

ALTER TABLE setlists ADD COLUMN IF NOT EXISTS show_id  UUID REFERENCES shows(id) ON DELETE SET NULL;
ALTER TABLE setlists ADD COLUMN IF NOT EXISTS position INTEGER;  -- order within show; NULL when standalone
CREATE INDEX IF NOT EXISTS setlists_show_idx ON setlists (show_id, position);

COMMIT;
