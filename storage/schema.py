"""Database schema definitions for the bedside biosignal pipeline."""

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA temp_store = MEMORY;

-- Samples table: exactly one entry per sample received
CREATE TABLE IF NOT EXISTS samples (
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    ch0 INTEGER NOT NULL,
    ch1 INTEGER NOT NULL,
    ch2 INTEGER,
    PRIMARY KEY (session_id, sensor, idx)
);

-- Losses table: accurate accounting of missing or lost intervals
CREATE TABLE IF NOT EXISTS losses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    from_idx INTEGER NOT NULL,
    to_idx INTEGER,  -- NULL if unknown/open-ended
    reason TEXT NOT NULL,
    detected_at REAL NOT NULL
);

-- Cursors table: tracks reader and uploader state strictly per (session_id, sensor)
CREATE TABLE IF NOT EXISTS cursors (
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    last_read_idx INTEGER NOT NULL DEFAULT -1,
    last_acked_idx INTEGER NOT NULL DEFAULT -1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, sensor)
);

-- Results table: computed window metrics for offline (device) and online (cloud)
CREATE TABLE IF NOT EXISTS results (
    session_id TEXT NOT NULL,
    "window" INTEGER NOT NULL,
    mode TEXT NOT NULL,  -- 'online' or 'offline'
    missing_1 INTEGER NOT NULL,
    missing_2 INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    mean_s1c1 REAL,
    mean_s1c2 REAL,
    mean_s1c3 REAL,
    mean_s2c1 REAL,
    mean_s2c2 REAL,
    min_s1c1 REAL,
    min_s1c2 REAL,
    min_s1c3 REAL,
    min_s2c1 REAL,
    min_s2c2 REAL,
    max_s1c1 REAL,
    max_s1c2 REAL,
    max_s1c3 REAL,
    max_s2c1 REAL,
    max_s2c2 REAL,
    magnitude REAL,
    heart_rate REAL,
    processed_at REAL NOT NULL,
    PRIMARY KEY (session_id, "window", mode)
);

-- System state table: stores operational mode ('online' or 'offline')
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_samples_export ON samples (session_id, idx ASC, sensor ASC);
CREATE INDEX IF NOT EXISTS idx_samples_uploader ON samples (session_id, sensor, idx ASC);
CREATE INDEX IF NOT EXISTS idx_losses_query ON losses (session_id, sensor, from_idx ASC);
CREATE INDEX IF NOT EXISTS idx_results_query ON results (session_id, "window" ASC, mode ASC);
"""
