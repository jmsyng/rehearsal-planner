#!/usr/bin/env python3
"""Run once to initialize the database schema and apply migrations."""
import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()

conn = psycopg2.connect(os.environ["DATABASE_URL"])
with open("schema.sql") as f:
    sql = f.read()
with conn.cursor() as cur:
    cur.execute(sql)
conn.commit()

# ── Data migrations (idempotent) ──────────────────────────────────────────────
MIGRATIONS = """
-- 1. Remap legacy Yay/Meh/Boo votes to Likert 1-5 scale
UPDATE song_votes
   SET vote = CASE vote
                  WHEN 'yay' THEN '5'
                  WHEN 'meh' THEN '3'
                  WHEN 'boo' THEN '1'
                  ELSE vote
              END
 WHERE vote IN ('yay', 'meh', 'boo');

-- 2. Recompute proposal scores from new vote values
UPDATE song_proposals sp
   SET score = (
       SELECT COALESCE(SUM(CASE sv.vote
           WHEN '5' THEN 5 WHEN '4' THEN 4 WHEN '3' THEN 3
           WHEN '2' THEN 2 WHEN '1' THEN 1 ELSE 0 END), 0)
       FROM song_votes sv WHERE sv.proposal_id = sp.id
   );

-- 3. Rename legacy 'Resting' status to 'Archived'
UPDATE songs SET status = 'Archived' WHERE status = 'Resting';

-- 4. Auto-create an 'approved' proposal for any band song that has none
--    (enables vote-on-all without a null proposalId)
INSERT INTO song_proposals (band_id, song_id, proposed_by, status, score)
SELECT DISTINCT s.band_id, s.id, s.user_id, 'approved', 0
FROM songs s
WHERE s.band_id IS NOT NULL
  AND s.status != 'Archived'
  AND NOT EXISTS (
      SELECT 1 FROM song_proposals sp
      WHERE sp.song_id = s.id AND sp.band_id = s.band_id
  );
"""

with conn.cursor() as cur:
    cur.execute(MIGRATIONS)
conn.commit()
conn.close()
print("Schema initialized and migrations applied successfully.")
