-- Migration 003: Raise default approval factor from 3.0 to 3.25
--
-- At 3.25 a 4-member band needs 13/20 pts to approve a song.
-- This means 4 Mehs (12) and 2 Love+2 Hard-no (12) both fail to approve.
-- Scales to other sizes: 2→7/10, 3→10/15, 5→17/25, 6→20/30.
--
-- Migrates any bands still on the previous default (3.0) to 3.25.
-- Bands that explicitly chose a different value via the slider are untouched.

BEGIN;

ALTER TABLE bands ALTER COLUMN approval_factor SET DEFAULT 3.25;

UPDATE bands SET approval_factor = 3.25 WHERE approval_factor = 3.0;

COMMIT;
