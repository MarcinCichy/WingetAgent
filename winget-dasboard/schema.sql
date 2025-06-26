-- Usuń istniejące tabele, aby uniknąć błędów przy ponownej inicjalizacji
DROP TABLE IF EXISTS computers;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS updates;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS action_history; -- Zastępuje starą tabelę update_history
DROP TABLE IF EXISTS reports;

-- Tabela przechowująca informacje o monitorowanych komputerach
CREATE TABLE computers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT UNIQUE NOT NULL,
    ip_address TEXT NOT NULL,
    last_report TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tabela przechowująca każdą migawkę/raport w czasie
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    report_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

-- Aplikacje przypisane do konkretnego raportu
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    app_id TEXT,
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
);

-- Aktualizacje przypisane do konkretnego raportu
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

-- Zadania do wykonania przez agentów
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

-- NOWA, UNIWERSALNA TABELA HISTORII AKCJI
CREATE TABLE action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action_type TEXT NOT NULL, -- np. 'APP_UPDATE_SUCCESS', 'APP_UNINSTALL_FAILURE', 'OS_UPDATE_SUCCESS'
    details TEXT, -- Zapisane jako JSON, np. {"name": "Notepad++", "from": "8.1", "to": "8.2"}
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);