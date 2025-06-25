import os
import sqlite3
import json
import logging
from flask import Flask, request, g, render_template, abort, Response, jsonify, send_from_directory
from functools import wraps
from datetime import datetime
from zoneinfo import ZoneInfo

# --- Konfiguracja ---
DATABASE = 'winget_dashboard.db'
# WAŻNE: Ustaw ten sam klucz, który będzie używany przez agenty!
API_KEY = "test1234"

app = Flask(__name__)
app.config.from_object(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- Filtr Czasu do konwersji UTC na czas lokalny ---
@app.template_filter('to_local_time')
def to_local_time_filter(utc_str):
    """Filtr Jinja2 do konwersji czasu UTC na lokalny czas warszawski."""
    if not utc_str:
        return ""
    try:
        utc_dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(ZoneInfo("Europe/Warsaw"))
        return local_dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return utc_str


# --- Obsługa Bazy Danych ---
def get_db():
    if not hasattr(g, 'sqlite_db'):
        g.sqlite_db = sqlite3.connect(app.config['DATABASE'])
        g.sqlite_db.row_factory = sqlite3.Row
    return g.sqlite_db


@app.teardown_appcontext
def close_db(error):
    if hasattr(g, 'sqlite_db'):
        g.sqlite_db.close()


@app.cli.command('init-db')
def init_db_command():
    """Inicjalizuje bazę danych na podstawie pliku schema.sql."""
    db = sqlite3.connect(app.config['DATABASE'])
    with app.open_resource('schema.sql', mode='r') as f:
        db.cursor().executescript(f.read())
    db.commit()
    db.close()
    print('Zainicjowano bazę danych.')


# --- Bezpieczeństwo API (Dekorator) ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.headers.get('X-API-Key') and request.headers.get('X-API-Key') == app.config['API_KEY']:
            return f(*args, **kwargs)
        else:
            abort(401)

    return decorated_function


# --- GŁÓWNE STRONY (FRONTEND) ---
@app.route('/')
def index():
    db = get_db()
    computers = db.execute(
        "SELECT id, hostname, ip_address, last_report FROM computers ORDER BY hostname COLLATE NOCASE").fetchall()
    return render_template('index.html', computers=computers)


@app.route('/computer/<hostname>')
def computer_details(hostname):
    db = get_db()
    computer = db.execute("SELECT * FROM computers WHERE hostname = ?", (hostname,)).fetchone()
    if not computer:
        abort(404)

    apps = db.execute(
        "SELECT name, version, app_id FROM applications WHERE computer_id = ? ORDER BY name COLLATE NOCASE",
        (computer['id'],)).fetchall()
    updates = db.execute(
        "SELECT id, name, app_id, status, current_version, available_version, update_type FROM updates WHERE computer_id = ? ORDER BY update_type, name COLLATE NOCASE",
        (computer['id'],)
    ).fetchall()

    return render_template('computer.html', computer=computer, apps=apps, updates=updates)


# --- API DO ZARZĄDZANIA ZADANIAMI ---
@app.route('/computer/<int:computer_id>/update', methods=['POST'])
def request_update(computer_id):
    """Endpoint wywoływany przez przycisk 'Aktualizuj' w interfejsie."""
    data = request.get_json()
    package_id = data.get('package_id')
    update_id = data.get('update_id')

    if not package_id or not update_id:
        return jsonify({"status": "error", "message": "Brak ID pakietu lub ID aktualizacji"}), 400

    db = get_db()
    db.execute("INSERT INTO tasks (computer_id, command, payload) VALUES (?, ?, ?)",
               (computer_id, 'update_package', package_id))
    db.execute("UPDATE updates SET status = 'Oczekuje' WHERE id = ?", (update_id,))
    db.commit()

    logging.info(f"Zlecono zadanie aktualizacji pakietu {package_id} dla komputera ID {computer_id}")
    return jsonify({"status": "success", "message": "Zadanie aktualizacji zlecone"})


@app.route('/computer/<int:computer_id>/refresh', methods=['POST'])
def request_refresh(computer_id):
    """Endpoint wywoływany przez przycisk 'Odśwież' na stronie głównej."""
    db = get_db()
    # Sprawdź, czy komputer istnieje
    computer = db.execute("SELECT id FROM computers WHERE id = ?", (computer_id,)).fetchone()
    if not computer:
        return jsonify({"status": "error", "message": "Nie znaleziono komputera"}), 404

    # Zleć zadanie odświeżenia raportu
    db.execute(
        "INSERT INTO tasks (computer_id, command, payload) VALUES (?, ?, ?)",
        (computer_id, 'force_report', '{}')  # Payload jest pusty, ale wymagany
    )
    db.commit()
    logging.info(f"Zlecono zadanie odświeżenia raportu dla komputera ID {computer_id}")
    return jsonify({"status": "success", "message": "Zadanie odświeżenia zlecone"})


@app.route('/api/tasks/<hostname>', methods=['GET'])
@require_api_key
def get_tasks(hostname):
    """Endpoint dla agenta do pobierania zadań do wykonania."""
    db = get_db()
    computer = db.execute("SELECT id FROM computers WHERE hostname = ?", (hostname,)).fetchone()
    if not computer:
        return jsonify([])

    tasks = db.execute("SELECT id, command, payload FROM tasks WHERE computer_id = ? AND status = 'oczekuje'",
                       (computer['id'],)).fetchall()

    if tasks:
        task_ids = [task['id'] for task in tasks]
        db.executemany("UPDATE tasks SET status = 'w toku', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       [(id,) for id in task_ids])
        db.commit()

    return jsonify([dict(row) for row in tasks])


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')


@app.route('/api/tasks/result', methods=['POST'])
@require_api_key
def task_result():
    """Endpoint dla agenta do raportowania wyników wykonanych zadań."""
    data = request.get_json()
    task_id = data.get('task_id')
    status = data.get('status')
    details = data.get('details', {})

    if not task_id or not status: return "Bad Request", 400

    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task: return "Task not found", 404

    db.execute("UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, task_id))

    package_id = task['payload']
    if status == 'zakończone':
        db.execute(
            "INSERT INTO update_history (computer_id, name, old_version, new_version) VALUES (?, ?, ?, ?)",
            (task['computer_id'], details.get('name'), details.get('old_version'), details.get('new_version'))
        )
    elif status == 'błąd':
        db.execute(
            "UPDATE updates SET status = 'Niepowodzenie' WHERE computer_id = ? AND app_id = ?",
            (task['computer_id'], package_id)
        )

    db.commit()
    return "Result received", 200


# --- GŁÓWNY ENDPOINT API DLA RAPORTÓW OD AGENTÓW ---
@app.route('/api/report', methods=['POST'])
@require_api_key
def receive_report():
    data = request.get_json()
    if not data or 'hostname' not in data:
        return "Bad Request: No data or hostname missing", 400

    hostname = data.get('hostname')
    ip_address = data.get('ip_address')
    logging.info(f"Odebrano raport od: {hostname} ({ip_address})")

    db = get_db()
    cur = db.cursor()

    try:
        cur.execute("SELECT id FROM computers WHERE hostname = ?", (hostname,))
        computer = cur.fetchone()
        if computer:
            computer_id = computer['id']
            cur.execute("UPDATE computers SET ip_address = ?, last_report = CURRENT_TIMESTAMP WHERE id = ?",
                        (ip_address, computer_id))
        else:
            cur.execute("INSERT INTO computers (hostname, ip_address) VALUES (?, ?)", (hostname, ip_address))
            computer_id = cur.lastrowid

        cur.execute("DELETE FROM applications WHERE computer_id = ?", (computer_id,))
        cur.execute("DELETE FROM updates WHERE computer_id = ?", (computer_id,))

        for app_data in data.get('installed_apps', []):
            cur.execute("INSERT INTO applications (computer_id, name, app_id, version) VALUES (?, ?, ?, ?)",
                        (computer_id, app_data.get('name'), app_data.get('id'), app_data.get('version')))

        for update_data in data.get('available_app_updates', []):
            cur.execute(
                """INSERT INTO updates (computer_id, name, app_id, current_version, available_version, update_type) 
                   VALUES (?, ?, ?, ?, ?, 'APP')""",
                (computer_id, update_data.get('name'), update_data.get('id'),
                 update_data.get('current_version'), update_data.get('available_version'))
            )

        pending_os_updates = data.get('pending_os_updates', [])
        if isinstance(pending_os_updates, dict):
            pending_os_updates = [pending_os_updates]

        for os_update in pending_os_updates:
            if not isinstance(os_update, dict):
                logging.warning(f"Pominięto nieprawidłowy wpis aktualizacji systemu: {os_update}")
                continue

            kb = ", ".join(os_update.get('KB', [])) if isinstance(os_update.get('KB'), list) else os_update.get('KB',
                                                                                                                'N/A')
            title = os_update.get('Title', 'Brak tytułu')

            cur.execute(
                "INSERT INTO updates (computer_id, name, available_version, update_type) VALUES (?, ?, ?, 'OS')",
                (computer_id, title, kb))

        db.commit()
    except sqlite3.Error as e:
        db.rollback()
        logging.error(f"Błąd bazy danych podczas przetwarzania raportu od {hostname}: {e}")
        return "Internal Server Error", 500

    return "Report received successfully", 200


# --- ENDPOINTY DO GENEROWANIA RAPORTÓW ---
def generate_report_content(computer_ids):
    """Pomocnicza funkcja do generowania treści raportu dla podanych ID komputerów."""
    db = get_db()
    content = []

    for computer_id in computer_ids:
        computer = db.execute("SELECT * FROM computers WHERE id = ?", (computer_id,)).fetchone()
        if not computer: continue

        content.append(f"# RAPORT DLA KOMPUTERA: {computer['hostname']} ({computer['ip_address']})")
        content.append(f"Data wygenerowania: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\n")

        # Historia aktualizacji
        content.append("## Historia pomyślnych aktualizacji")
        history = db.execute(
            "SELECT name, old_version, new_version, completed_at FROM update_history WHERE computer_id = ? ORDER BY completed_at DESC",
            (computer_id,)).fetchall()
        if history:
            for item in history:
                content.append(
                    f"* {item['name']}: {item['old_version']} -> {item['new_version']} (dnia {to_local_time_filter(item['completed_at'])})")
        else:
            content.append("* Brak zarejestrowanych aktualizacji w historii.")
        content.append("\n")

        # Oczekujące aktualizacje
        content.append("## Oczekujące aktualizacje")
        updates = db.execute("SELECT name, current_version, available_version FROM updates WHERE computer_id = ?",
                             (computer_id,)).fetchall()
        if updates:
            for item in updates:
                content.append(f"* {item['name']}: {item['current_version']} -> {item['available_version']}")
        else:
            content.append("* Brak oczekujących aktualizacji.")
        content.append("\n")

        # Zainstalowane aplikacje
        content.append("## Zainstalowane aplikacje (przefiltrowana lista)")
        apps = db.execute("SELECT name, version FROM applications WHERE computer_id = ?", (computer_id,)).fetchall()
        if apps:
            for item in apps:
                content.append(f"* {item['name']} ({item['version']})")
        else:
            content.append("* Brak aplikacji.")
        content.append("\n" + "=" * 80 + "\n")

    return "\n".join(content)


@app.route('/report/computer/<int:computer_id>')
def report_single(computer_id):
    """Generuje raport tekstowy dla jednego komputera."""
    db = get_db()
    computer = db.execute("SELECT hostname FROM computers WHERE id = ?", (computer_id,)).fetchone()
    if not computer: abort(404)

    content = generate_report_content([computer_id])
    filename = f"report_{computer['hostname']}_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


@app.route('/report/all')
def report_all():
    """Generuje raport zbiorczy dla wszystkich komputerów."""
    db = get_db()
    computers = db.execute("SELECT id FROM computers ORDER BY hostname").fetchall()
    computer_ids = [c['id'] for c in computers]

    content = generate_report_content(computer_ids)
    filename = f"report_zbiorczy_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


# --- GŁÓWNE URUCHOMIENIE ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)