import sqlite3
import json
import logging
from flask import Flask, request, g, render_template, abort, Response, jsonify, send_from_directory
from functools import wraps
from datetime import datetime, UTC
from zoneinfo import ZoneInfo
import os

# --- Konfiguracja ---
DATABASE = 'winget_dashboard.db'
API_KEY = "test1234"

app = Flask(__name__)
app.config.from_object(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- Filtr Czasu i Procesor Kontekstu ---
@app.template_filter('to_local_time')
def to_local_time_filter(utc_str):
    if not utc_str: return ""
    try:
        # Konwertujemy string na obiekt datetime. Dodajemy obsługę różnych formatów, jeśli to konieczne.
        utc_dt = datetime.fromisoformat(str(utc_str).split('.')[0]).replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(ZoneInfo("Europe/Warsaw"))
        return local_dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return utc_str


@app.context_processor
def inject_year():
    return {'current_year': datetime.now(UTC).year}


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
    db = sqlite3.connect(app.config['DATABASE'])
    with app.open_resource('schema.sql', mode='r') as f:
        db.cursor().executescript(f.read())
    db.commit()
    db.close()
    print('Zainicjowano bazę danych.')


# --- Bezpieczeństwo i Favicon ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.headers.get('X-API-Key') and request.headers.get('X-API-Key') == app.config['API_KEY']:
            return f(*args, **kwargs)
        else:
            abort(401)

    return decorated_function


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')


# --- GŁÓWNE STRONY (FRONTEND) ---
@app.route('/')
def index():
    computers = get_db().execute(
        "SELECT id, hostname, ip_address, last_report FROM computers ORDER BY hostname COLLATE NOCASE").fetchall()
    return render_template('index.html', computers=computers)


@app.route('/computer/<hostname>')
def computer_details(hostname):
    db = get_db()
    computer = db.execute("SELECT * FROM computers WHERE hostname = ?", (hostname,)).fetchone()
    if not computer: abort(404)
    latest_report = db.execute("SELECT id FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC LIMIT 1",
                               (computer['id'],)).fetchone()
    apps, updates = [], []
    if latest_report:
        report_id = latest_report['id']
        apps = db.execute(
            "SELECT name, version, app_id FROM applications WHERE report_id = ? ORDER BY name COLLATE NOCASE",
            (report_id,)).fetchall()
        updates = db.execute(
            "SELECT id, name, app_id, status, current_version, available_version, update_type FROM updates WHERE report_id = ? ORDER BY update_type, name COLLATE NOCASE",
            (report_id,)).fetchall()
    return render_template('computer.html', computer=computer, apps=apps, updates=updates)


@app.route('/computer/<hostname>/history')
def computer_history(hostname):
    db = get_db()
    computer = db.execute("SELECT * FROM computers WHERE hostname = ?", (hostname,)).fetchone()
    if not computer: abort(404)
    reports = db.execute(
        "SELECT id, report_timestamp FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC",
        (computer['id'],)).fetchall()
    return render_template('history.html', computer=computer, reports=reports)


@app.route('/settings')
def settings():
    """Wyświetla stronę z ustawieniami."""
    return render_template('settings.html')


@app.route('/report/<int:report_id>')
def view_report(report_id):
    db = get_db()
    report = db.execute(
        "SELECT r.id, r.report_timestamp, c.hostname, c.ip_address FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report: abort(404)
    apps = db.execute("SELECT name, version, app_id FROM applications WHERE report_id = ? ORDER BY name COLLATE NOCASE",
                      (report_id,)).fetchall()
    updates = db.execute(
        "SELECT name, app_id, current_version, available_version, update_type FROM updates WHERE report_id = ? ORDER BY update_type, name COLLATE NOCASE",
        (report_id,)).fetchall()
    return render_template('report_view.html', report=report, apps=apps, updates=updates)


# --- GŁÓWNY ENDPOINT API DLA RAPORTÓW OD AGENTÓW ---
@app.route('/api/report', methods=['POST'])
@require_api_key
def receive_report():
    data, db = request.get_json(), get_db()
    if not data or 'hostname' not in data: return "Bad Request", 400

    hostname = data.get('hostname')
    logging.info(f"Przetwarzanie raportu od: {hostname}")

    try:
        cur = db.cursor()
        computer = cur.execute("SELECT id FROM computers WHERE hostname = ?", (hostname,)).fetchone()
        if computer:
            computer_id = computer['id']
            cur.execute("UPDATE computers SET ip_address = ?, last_report = CURRENT_TIMESTAMP WHERE id = ?",
                        (data.get('ip_address'), computer_id))
        else:
            cur.execute("INSERT INTO computers (hostname, ip_address) VALUES (?, ?)",
                        (hostname, data.get('ip_address')))
            computer_id = cur.lastrowid

        previous_report = db.execute(
            "SELECT id FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC LIMIT 1",
            (computer_id,)).fetchone()

        cur.execute("INSERT INTO reports (computer_id) VALUES (?)", (computer_id,))
        report_id = cur.lastrowid

        apps_to_insert = [(report_id, app.get('name'), app.get('id'), app.get('version')) for app in
                          data.get('installed_apps', [])]
        if apps_to_insert: cur.executemany(
            "INSERT INTO applications (report_id, name, app_id, version) VALUES (?, ?, ?, ?)", apps_to_insert)

        app_updates_to_insert = [
            (report_id, upd.get('name'), upd.get('id'), upd.get('current_version'), upd.get('available_version')) for
            upd in data.get('available_app_updates', [])]
        if app_updates_to_insert: cur.executemany(
            "INSERT INTO updates (report_id, name, app_id, current_version, available_version, update_type) VALUES (?, ?, ?, ?, ?, 'APP')",
            app_updates_to_insert)

        os_updates_to_insert = []
        pending_os_updates = data.get('pending_os_updates', [])
        if isinstance(pending_os_updates, dict): pending_os_updates = [pending_os_updates]
        for os_update in pending_os_updates:
            if not isinstance(os_update, dict): continue
            kb = ", ".join(os_update.get('KB', [])) if isinstance(os_update.get('KB'), list) else os_update.get('KB',
                                                                                                                'N/A')
            title = os_update.get('Title', 'Brak tytułu')
            os_updates_to_insert.append((report_id, title, kb))
        if os_updates_to_insert: cur.executemany(
            "INSERT INTO updates (report_id, name, available_version, update_type) VALUES (?, ?, ?, 'OS')",
            os_updates_to_insert)

        if previous_report:
            old_updates_q = db.execute("SELECT name FROM updates WHERE report_id = ? AND update_type = 'OS'",
                                       (previous_report['id'],)).fetchall()
            old_updates = {u['name'] for u in old_updates_q}
            new_updates_list = [u for u in data.get('pending_os_updates', []) if isinstance(u, dict)]
            new_updates = {u.get('Title') for u in new_updates_list}
            installed_updates = old_updates - new_updates
            for update_name in installed_updates:
                cur.execute("INSERT INTO action_history (computer_id, action_type, details) VALUES (?, ?, ?)",
                            (computer_id, 'OS_UPDATE_SUCCESS', json.dumps({"name": update_name})))
                logging.info(f"Wykryto pomyślną instalację aktualizacji OS '{update_name}' na komputerze {hostname}")

        db.commit()
    except sqlite3.Error as e:
        db.rollback();
        logging.error(f"Błąd bazy danych (receive_report): {e}")
        return "Internal Server Error", 500
    return "Report received successfully", 200


# --- API DO ZARZĄDZANIA ZADANIAMI ---
@app.route('/computer/<int:computer_id>/update', methods=['POST'])
def request_update(computer_id):
    data, db = request.get_json(), get_db()
    db.execute("INSERT INTO tasks (computer_id, command, payload) VALUES (?, ?, ?)",
               (computer_id, 'update_package', data.get('package_id')))
    db.execute("UPDATE updates SET status = 'Oczekuje' WHERE id = ?", (data.get('update_id'),))
    db.commit()
    return jsonify({"status": "success", "message": "Zadanie aktualizacji zlecone"})


@app.route('/computer/<int:computer_id>/uninstall', methods=['POST'])
def request_uninstall(computer_id):
    data, db = request.get_json(), get_db()
    if not db.execute("SELECT id FROM computers WHERE id = ?", (computer_id,)).fetchone(): abort(404)
    db.execute("INSERT INTO tasks (computer_id, command, payload) VALUES (?, ?, ?)",
               (computer_id, 'uninstall_package', data.get('package_id')))
    db.commit()
    return jsonify({"status": "success", "message": "Zadanie deinstalacji zlecone"})


@app.route('/computer/<int:computer_id>/refresh', methods=['POST'])
def request_refresh(computer_id):
    db = get_db()
    if not db.execute("SELECT id FROM computers WHERE id = ?", (computer_id,)).fetchone(): abort(404)
    db.execute("INSERT INTO tasks (computer_id, command, payload) VALUES (?, ?, ?)",
               (computer_id, 'force_report', '{}'))
    db.commit()
    return jsonify({"status": "success", "message": "Zadanie odświeżenia zlecone"})


@app.route('/api/tasks/<hostname>', methods=['GET'])
@require_api_key
def get_tasks(hostname):
    db = get_db()
    computer = db.execute("SELECT id FROM computers WHERE hostname = ?", (hostname,)).fetchone()
    if not computer: return jsonify([])
    tasks = db.execute("SELECT id, command, payload FROM tasks WHERE computer_id = ? AND status = 'oczekuje'",
                       (computer['id'],)).fetchall()
    if tasks:
        db.executemany("UPDATE tasks SET status = 'w toku', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                       [(t['id'],) for t in tasks])
        db.commit()
    return jsonify([dict(row) for row in tasks])


@app.route('/api/tasks/result', methods=['POST'])
@require_api_key
def task_result():
    data, db = request.get_json(), get_db()
    task_id, status = data.get('task_id'), data.get('status')
    if not task_id or not status: return "Bad Request", 400
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task: return "Task not found", 404
    db.execute("UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, task_id))
    computer_id, command, package_id = task['computer_id'], task['command'], task['payload']
    action_type, details_dict = "", {}
    latest_report_id = db.execute("SELECT id FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC LIMIT 1",
                                  (computer_id,)).fetchone()['id']

    if command == 'update_package':
        app_details = db.execute(
            "SELECT name, current_version, available_version FROM updates WHERE report_id = ? AND app_id = ?",
            (latest_report_id, package_id)).fetchone()
        app_name = app_details['name'] if app_details else package_id
        if status == 'zakończone':
            action_type = 'APP_UPDATE_SUCCESS'
            details_dict = {"name": app_name, "from": app_details['current_version'],
                            "to": app_details['available_version']}
        else:
            action_type = 'APP_UPDATE_FAILURE'
            details_dict = {"name": app_name}
            db.execute("UPDATE updates SET status = 'Niepowodzenie' WHERE report_id = ? AND app_id = ?",
                       (latest_report_id, package_id))
    elif command == 'uninstall_package':
        app_info = db.execute("SELECT name FROM applications WHERE report_id = ? AND app_id = ?",
                              (latest_report_id, package_id)).fetchone()
        app_name_to_uninstall = app_info['name'] if app_info else package_id
        action_type = 'APP_UNINSTALL_SUCCESS' if status == 'zakończone' else 'APP_UNINSTALL_FAILURE'
        details_dict = {"name": app_name_to_uninstall}

    if action_type:
        db.execute("INSERT INTO action_history (computer_id, action_type, details) VALUES (?, ?, ?)",
                   (computer_id, action_type, json.dumps(details_dict)))
    db.commit()
    return "Result received", 200


# --- ENDPOINTY DO GENEROWANIA RAPORTÓW ---
def generate_report_content(computer_ids):
    db, content = get_db(), []
    for cid in computer_ids:
        computer = db.execute("SELECT * FROM computers WHERE id = ?", (cid,)).fetchone()
        if not computer: continue
        content.append(f"# RAPORT DLA KOMPUTERA: {computer['hostname']} ({computer['ip_address']})")
        content.append(f"Data wygenerowania: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\n")
        content.append("## Dziennik Zdarzeń (ostatnie 20)")
        history = db.execute(
            "SELECT timestamp, action_type, details FROM action_history WHERE computer_id = ? ORDER BY timestamp DESC LIMIT 20",
            (cid,)).fetchall()
        if history:
            for item in history:
                details, log_time, log_entry = json.loads(item['details']), to_local_time_filter(
                    item['timestamp']), f"* [{to_local_time_filter(item['timestamp'])}] "
                if item['action_type'] == 'APP_UPDATE_SUCCESS':
                    log_entry += f"Sukces aktualizacji: {details.get('name', '')} (z {details.get('from', '?')} do {details.get('to', '?')})"
                elif item['action_type'] == 'APP_UPDATE_FAILURE':
                    log_entry += f"Błąd aktualizacji: {details.get('name', '')}"
                elif item['action_type'] == 'APP_UNINSTALL_SUCCESS':
                    log_entry += f"Sukces deinstalacji: {details.get('name', '')}"
                elif item['action_type'] == 'APP_UNINSTALL_FAILURE':
                    log_entry += f"Błąd deinstalacji: {details.get('name', '')}"
                elif item['action_type'] == 'OS_UPDATE_SUCCESS':
                    log_entry += f"Sukces aktualizacji systemu: {details.get('name', '')}"
                content.append(log_entry)
        else:
            content.append("* Brak zarejestrowanych zdarzeń w historii.")
        latest_report = db.execute(
            "SELECT id FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC LIMIT 1", (cid,)).fetchone()
        if latest_report:
            content.append("\n## Oczekujące aktualizacje (wg ostatniego raportu)")
            updates = db.execute("SELECT name, current_version, available_version FROM updates WHERE report_id = ?",
                                 (latest_report['id'],)).fetchall()
            if updates:
                [content.append(f"* {item['name']}: {item['current_version']} -> {item['available_version']}") for item
                 in updates]
            else:
                content.append("* Brak oczekujących aktualizacji.")
            content.append("\n## Zainstalowane aplikacje (wg ostatniego raportu)")
            apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?",
                              (latest_report['id'],)).fetchall()
            if apps:
                [content.append(f"* {item['name']} ({item['version']})") for item in apps]
            else:
                content.append("* Brak aplikacji.")
        content.append("\n" + "=" * 80 + "\n")
    return "\n".join(content)


@app.route('/report/computer/<int:computer_id>')
def report_single(computer_id):
    computer = get_db().execute("SELECT hostname FROM computers WHERE id = ?", (computer_id,)).fetchone()
    if not computer: abort(404)
    content = generate_report_content([computer_id])
    filename = f"report_{computer['hostname']}_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


@app.route('/report/all')
def report_all():
    computer_ids = [c['id'] for c in get_db().execute("SELECT id FROM computers ORDER BY hostname").fetchall()]
    content = generate_report_content(computer_ids)
    filename = f"report_zbiorczy_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


def generate_snapshot_report_content(report_id):
    """Pomocnicza funkcja do generowania treści raportu dla jednej, konkretnej migawki."""
    db = get_db()
    report = db.execute(
        "SELECT r.id, r.report_timestamp, c.hostname, c.ip_address FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)
    ).fetchone()

    if not report:
        return "Nie znaleziono raportu o podanym ID."

    content = []
    content.append(f"# RAPORT HISTORYCZNY DLA: {report['hostname']} ({report['ip_address']})")
    content.append(f"# Migawka z dnia: {to_local_time_filter(report['report_timestamp'])}\n")
    content.append(
        f"Data wygenerowania pliku: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Oczekujące aktualizacje w momencie tworzenia migawki
    content.append("## Oczekujące aktualizacje w tym raporcie")
    updates = db.execute(
        "SELECT name, current_version, available_version, update_type FROM updates WHERE report_id = ?",
        (report_id,)).fetchall()
    if updates:
        for item in updates:
            if item['update_type'] == 'OS':
                content.append(f"* [System] {item['name']} (KB: {item['available_version']})")
            else:
                content.append(
                    f"* [Aplikacja] {item['name']}: {item['current_version']} -> {item['available_version']}")
    else:
        content.append("* Brak.")
    content.append("\n")

    # Zainstalowane aplikacje w momencie tworzenia migawki
    content.append("## Zainstalowane aplikacje w tym raporcie")
    apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?", (report_id,)).fetchall()
    if apps:
        for item in apps:
            content.append(f"* {item['name']} ({item['version']})")
    else:
        content.append("* Brak.")

    return "\n".join(content)


@app.route('/report/snapshot/<int:report_id>')
def report_snapshot(report_id):
    """Generuje raport tekstowy dla jednej, konkretnej migawki historycznej."""
    report = get_db().execute(
        "SELECT c.hostname FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report:
        abort(404)

    content = generate_snapshot_report_content(report_id)
    filename = f"report_snapshot-{report_id}_{report['hostname']}_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


# --- GŁÓWNE URUCHOMIENIE ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)