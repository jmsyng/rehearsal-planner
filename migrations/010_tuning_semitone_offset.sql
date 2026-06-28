-- Per-user semitone offset for each tuning (default or custom), relative to
-- standard (E standard = 0). Powers Key transposition between a song's
-- recorded tuning and the band's actual tuning. Existing rows default to 0;
-- users set the real value via My Settings.
ALTER TABLE user_tunings ADD COLUMN IF NOT EXISTS semitone_offset INTEGER NOT NULL DEFAULT 0;
