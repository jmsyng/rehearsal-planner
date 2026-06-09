-- Migration 005: Add a public share token to every setlist
--
-- Enables a read-only, no-auth shareable link for a setlist (the ordered set
-- list + time-budget bar). Mirrors bands.invite_token: a permanent, unguessable
-- token that always exists. The column has a volatile default, so Postgres
-- evaluates gen_random_uuid() per existing row — every current setlist gets its
-- own distinct token and the UNIQUE constraint holds.
--
-- Safe to run on the existing database. Idempotent: ADD COLUMN IF NOT EXISTS
-- skips the column if it's already present.

BEGIN;

ALTER TABLE setlists
  ADD COLUMN IF NOT EXISTS share_token TEXT UNIQUE DEFAULT gen_random_uuid()::text;

COMMIT;
