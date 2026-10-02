-- 校園失物招領：MySQL 版
-- 在 MySQL 執行本檔（建議 8.0+）

CREATE DATABASE IF NOT EXISTS lost_found
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE lost_found;

CREATE TABLE IF NOT EXISTS items (
    id          CHAR(36) PRIMARY KEY,
    type        ENUM('found', 'lost') NOT NULL,
    image_url   VARCHAR(500) NULL,
    embedding   JSON NOT NULL,
    category    VARCHAR(50) NOT NULL,
    location    VARCHAR(200) NOT NULL,
    description TEXT,
    status      ENUM('pending', 'open', 'closed', 'rejected') NOT NULL DEFAULT 'pending',
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_status_type (status, type),
    INDEX idx_category (category),
    INDEX idx_created_at (created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
