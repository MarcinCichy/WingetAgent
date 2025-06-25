-- Usuń istniejące tabele, aby uniknąć błędów przy ponownej inicjalizacji
DROP TABLE IF EXISTS computers;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS updates;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS update_history;
DROP TABLE IF EXISTS reports; -- Nowa tabela

-- Tabela przechowująca informacje o monitorowanych komputerach (bez zmian)
CREATE TABLE computers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT UNIQUE NOT NULL,
    ip_address TEXT NOT NULL,
    last_report TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- NOWA TABELA: Przechowuje każdą migawkę/raport w czasie
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    report_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

-- ZMODYFIKOWANA TABELA: Aplikacje przypisane do konkretnego raportu
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL, -- NOWA KOLUMNA
    name TEXT NOT NULL,
    version TEXT,
    app_id TEXT,
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
);

-- ZMODYFIKOWANA TABELA: Aktualizacje przypisane do konkretnego raportu
CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL, -- NOWA KOLUMNA
    name TEXT NOT NULL,
    app_id TEXT,
    current_version TEXT,
    available_version TEXT,
    update_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Do uaktualnienia',
    FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
);

-- Istniejące tabele zadań i historii pozostają bez zmian
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'oczekuje', -- 'oczekuje', 'w toku', 'zakończone', 'błąd'
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

CREATE TABLE update_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    old_version TEXT,
    new_version TEXT,
    completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);