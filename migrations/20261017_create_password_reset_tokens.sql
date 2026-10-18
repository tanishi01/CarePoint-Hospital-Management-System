-- Additive password-reset storage and database-backed request throttling.
-- Apply once to the configured CarePoint database. Do not rerun database.sql.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL,
    email_hash CHAR(64) NOT NULL,
    request_ip_hash CHAR(64) NOT NULL,
    token_hash CHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NULL,
    used_at DATETIME NULL,
    attempt_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    INDEX idx_password_reset_email_created (email_hash, created_at),
    INDEX idx_password_reset_ip_created (request_ip_hash, created_at),
    INDEX idx_password_reset_token (token_hash),
    CONSTRAINT fk_password_reset_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
