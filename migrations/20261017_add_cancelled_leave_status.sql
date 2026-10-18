-- Allow doctors to cancel their own pending leave requests.
ALTER TABLE doctor_leave_requests
    MODIFY COLUMN status ENUM('pending', 'approved', 'rejected', 'cancelled')
    NOT NULL DEFAULT 'pending';
