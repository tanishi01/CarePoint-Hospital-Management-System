-- Apply once to an existing CarePoint database; do not rerun database.sql,
-- which contains destructive DROP TABLE statements.
ALTER TABLE doctor_schedules
    ADD COLUMN break_start TIME NULL,
    ADD COLUMN break_end TIME NULL;
