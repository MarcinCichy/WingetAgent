DROP TABLE IF EXISTS computers;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS updates;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS action_history;
DROP TABLE IF EXISTS reports;

CREATE TABLE computers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT UNIQUE NOT NULL,
    ip_address TEXT NOT NULL,
    reboot_required BOOLEAN NOT NULL DEFAULT 0,
    last_report TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    report_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    app_id TEXT,
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
);
CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    app_id TEXT,
    current_version TEXT,
    available_version TEXT,
    update_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Do uaktualnienia',
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'oczekuje',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);
CREATE TABLE action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action_type TEXT NOT NULL,
    details TEXT,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);