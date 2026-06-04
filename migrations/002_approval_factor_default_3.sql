-- Migration 002: Lower the default approval factor from 3.5 to 3.0
--
-- We moved voting from a 3-point (Yay/Meh/Boo) scale to a 5-point Likert scale.
-- The approval threshold is ceil(band_size * approval_factor); on the 5-point
-- scale the default per-member average required to approve a song drops from
-- 3.5/5 (~70%) to 3.0/5 (~60%, a simple positive-leaning majority).
--
--   * Change the column default for new bands.
--   * Migrate existing bands that are still on the old unconfigured default (3.5)
--     to 3.0. Bands that explicitly chose another value via the slider are left
--     untouched (3.5 is indistinguishable from the old default, so it moves too).
--
-- Safe to run on the existing database. Idempotent: re-running only re-applies the
-- same default and re-touches rows already at 3.0 (no-op).

BEGIN;

ALTER TABLE bands ALTER COLUMN approval_factor SET DEFAULT 3.0;

UPDATE bands SET approval_factor = 3.0 WHERE approval_factor = 3.5;

COMMIT;
