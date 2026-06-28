-- Musical key for harmonic reference. Standard notation only; Camelot code is
-- derived at read time in Python (see db.py CAMELOT_MAP), not stored.
ALTER TABLE songs ADD COLUMN IF NOT EXISTS key_standard TEXT;
