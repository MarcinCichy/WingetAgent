-- Usuń istniejące tabele, aby uniknąć błędów przy ponownej inicjalizacji
DROP TABLE IF EXISTS computers;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS updates;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS update_history;

-- Tabela przechowująca informacje o monitorowanych komputerach
CREATE TABLE computers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT UNIQUE NOT NULL,
    ip_address TEXT NOT NULL,
    last_report TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tabela przechowująca listę zainstalowanych aplikacji dla każdego komputera
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    version TEXT,
    app_id TEXT,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

-- Tabela przechowująca listę dostępnych aktualizacji (zarówno aplikacji, jak i systemu)
CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    app_id TEXT, -- Dodajemy app_id, aby łatwiej zlecać aktualizacje
    current_version TEXT,
    available_version TEXT,
    update_type TEXT NOT NULL, -- 'APP' dla aplikacji, 'OS' dla systemu
    status TEXT NOT NULL DEFAULT 'Do uaktualnienia', -- NOWA KOLUMNA: 'Do uaktualnienia', 'Oczekuje', 'Niepowodzenie'
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

-- NOWA TABELA: Zadania do wykonania przez agentów
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    command TEXT NOT NULL, -- np. 'update_package'
    payload TEXT NOT NULL, -- np. ID pakietu
    status TEXT NOT NULL DEFAULT 'oczekuje', -- 'oczekuje', 'w toku', 'zakończone', 'błąd'
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);

-- NOWA TABELA: Historia pomyślnie wykonanych aktualizacji
CREATE TABLE update_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    old_version TEXT,
    new_version TEXT,
    completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (computer_id) REFERENCES computers (id) ON DELETE CASCADE
);