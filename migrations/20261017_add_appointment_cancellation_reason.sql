-- Add a separate field to preserve the original visit reason when an
-- appointment is cancelled.
ALTER TABLE appointments
    ADD COLUMN cancellation_reason TEXT NULL AFTER reason;
