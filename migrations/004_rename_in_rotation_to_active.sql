-- Rename legacy "In Rotation" status to "Active" to match the updated status vocabulary.
UPDATE songs SET status = 'Active' WHERE status = 'In Rotation';
