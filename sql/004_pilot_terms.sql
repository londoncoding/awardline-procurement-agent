-- Existing installations retain each customer's explicit limit. New pilot
-- customers default to three trial dossier revisions.
ALTER TABLE pilot_customers ALTER COLUMN trial_limit SET DEFAULT 3;
