-- Creează baza de date dacă nu există
CREATE DATABASE IF NOT EXISTS osint_search;
USE osint_search;

CREATE TABLE twitter_searches (
    search_id INT AUTO_INCREMENT PRIMARY KEY,
    search_query TEXT NOT NULL,
    search_date DATE NOT NULL,
    search_time TIME NOT NULL
);

CREATE TABLE twitter_results (
    result_id INT AUTO_INCREMENT PRIMARY KEY,
    search_id INT NOT NULL,
    username VARCHAR(255) NOT NULL,
    tweet_content TEXT NOT NULL,
    tweet_link VARCHAR(512) NOT NULL,
    tweet_date DATE,
    tweet_time TIME,
    reply_count INT DEFAULT 0,
    repost_count INT DEFAULT 0,
    like_count INT DEFAULT 0,
    bookmark_count INT DEFAULT 0,
    FOREIGN KEY (search_id) REFERENCES twitter_searches(search_id),
    INDEX idx_search_id (search_id),
    INDEX idx_username (username)
);

CREATE TABLE twitter_search_history (
    history_id INT AUTO_INCREMENT PRIMARY KEY,
    original_search_id INT NOT NULL,
    related_search_id INT NOT NULL,
    comparison_date DATE NOT NULL,
    comparison_time TIME NOT NULL,
    changes_detected BOOLEAN DEFAULT FALSE,
    new_tweets_count INT DEFAULT 0,
    removed_tweets_count INT DEFAULT 0,
    engagement_changes JSON DEFAULT NULL,
    FOREIGN KEY (original_search_id) REFERENCES twitter_searches(search_id),
    FOREIGN KEY (related_search_id) REFERENCES twitter_searches(search_id),
    INDEX idx_original_search (original_search_id),
    INDEX idx_related_search (related_search_id)
);

CREATE TABLE google_searches (
    search_id INT AUTO_INCREMENT PRIMARY KEY,
    search_query TEXT NOT NULL,
    search_date DATE NOT NULL,
    search_time TIME NOT NULL
);

CREATE TABLE google_results (
    result_id INT AUTO_INCREMENT PRIMARY KEY,
    search_id INT NOT NULL,
    site_name VARCHAR(255),
    result_link VARCHAR(512) NOT NULL,
    result_title TEXT,
    result_content TEXT,
    publish_date DATE,
    publish_time TIME,
    FOREIGN KEY (search_id) REFERENCES google_searches(search_id),
    INDEX idx_search_id (search_id),
    INDEX idx_site_name (site_name)
);

CREATE TABLE google_search_history (
    history_id INT AUTO_INCREMENT PRIMARY KEY,
    original_search_id INT NOT NULL,
    related_search_id INT NOT NULL,
    comparison_date DATE NOT NULL,
    comparison_time TIME NOT NULL,
    changes_detected BOOLEAN DEFAULT FALSE,
    new_results_count INT DEFAULT 0,
    removed_results_count INT DEFAULT 0,
    FOREIGN KEY (original_search_id) REFERENCES google_searches(search_id),
    FOREIGN KEY (related_search_id) REFERENCES google_searches(search_id),
    INDEX idx_original_search (original_search_id),
    INDEX idx_related_search (related_search_id)
);

CREATE TABLE scheduled_searches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL,
    source ENUM('google', 'twitter') NOT NULL,
    query TEXT NOT NULL,
    interval_type VARCHAR(50) NOT NULL,
    interval_value INT NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME,
    status ENUM('active', 'completed', 'stopped') NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_run DATETIME,
    next_run DATETIME,
    total_runs INT DEFAULT 0
);

-- Creează utilizatorul MySQL cu permisiunile corespunzătoare
CREATE USER IF NOT EXISTS 'root'@'localhost' IDENTIFIED BY 'parola_de_conectare_la_baza_de_date';
GRANT ALL PRIVILEGES ON osint_search.* TO 'root'@'localhost';
FLUSH PRIVILEGES;
