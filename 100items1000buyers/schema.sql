CREATE TABLE IF NOT EXISTS items (
    id INT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    total INT NOT NULL,
    available INT NOT NULL,
    reserved INT NOT NULL DEFAULT 0,
    sold INT NOT NULL DEFAULT 0
);

DROP TABLE IF EXISTS orders;

CREATE TABLE orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    item_id INT NOT NULL,
    user_id INT NOT NULL,
    status ENUM('pending', 'paid', 'expired', 'refunded') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    expires_at TIMESTAMP(3) NULL DEFAULT NULL,
    UNIQUE KEY uq_item_user (item_id, user_id),
    KEY idx_item_status (item_id, status)
);

INSERT IGNORE INTO items (id, name, total, available) VALUES (1, 'Concert ticket', 100, 100);
