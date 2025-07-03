import sqlite3
import json
import logging
import os
import subprocess
import tempfile
import shutil
import uuid
from dotenv import load_dotenv
from flask import Flask, request, g, render_template, abort, Response, jsonify, send_from_directory, flash, redirect, \
    url_for, send_file
from functools import wraps
from datetime import datetime, UTC
from zoneinfo import ZoneInfo

# Ładowanie zmiennych z pliku .env na samym początku
load_dotenv()

AGENT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "agent_template.py.txt")
with open(AGENT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
    AGENT_TEMPLATE = f.read()

# --- Konfiguracja odczytywana z pliku .env ---
DATABASE = os.getenv('DATABASE_FILE', 'winget_dashboard.db')
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY', 'default-secret-key-for-dev-only')

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Funkcje i reszta aplikacji (bez zmian) ---

@app.after_request
def add_header(response):
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response

@app.template_filter('to_local_time')
def to_local_time_filter(utc_str):
    if not utc_str: return ""
    try:
        utc_dt = datetime.fromisoformat(str(utc_str).split('.')[0]).replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(ZoneInfo("Europe/Warsaw"))
        return local_dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return utc_str

@app.context_processor
def inject_year():
    return {'current_year': datetime.now(UTC).year}

def get_db():
    if not hasattr(g, 'sqlite_db'):
        g.sqlite_db = sqlite3.connect(DATABASE)
        g.sqlite_db.row_factory = sqlite3.Row
    return g.sqlite_db

@app.teardown_appcontext
def close_db(error):
    if hasattr(g, 'sqlite_db'):
        g.sqlite_db.close()

@app.cli.command('init-db')
def init_db_command():
    db = sqlite3.connect(DATABASE)
    with app.open_resource('schema.sql', mode='r') as f:
        db.cursor().executescript(f.read())
    db.commit();
    db.close()
    print('Zainicjowano bazę danych.')

def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.headers.get('X-API-Key') and request.headers.get('X-API-Key') == API_KEY:
            return f(*args, **kwargs)
        else:
            abort(401)
    return decorated_function

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')

@app.route('/')
def index():
    computers = get_db().execute(
        "SELECT id, hostname, ip_address, last_report, reboot_required FROM computers ORDER BY hostname COLLATE NOCASE").fetchall()
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


@app.route('/settings')
def settings():
    default_blacklist_keywords = """
redistributable
visual c++
.net framework
microsoft
windows
bing
edge
onedrive
office
teams
outlook
store
    """
    return render_template('settings.html', server_api_key=API_KEY, default_blacklist_keywords=default_blacklist_keywords)

@app.route('/api/report', methods=['POST'])
@require_api_key
def receive_report():

    data, db = request.get_json(), get_db()
    if not data or 'hostname' not in data: return "Bad Request", 400
    hostname = data.get('hostname')
    logging.info(f"Przetwarzanie raportu od: {hostname}")
    logging.info("[DEBUG] SERVER odebrał raport: installed_apps=%d, available_app_updates=%d, pending_os_updates=%d",
                 len(data.get("installed_apps", [])),
                 len(data.get("available_app_updates", [])),
                 len(data.get("pending_os_updates", [])) if isinstance(data.get("pending_os_updates", []), list) else 1
                 )
    # (opcjonalnie, dla podglądu co w środku)
    logging.info("[DEBUG] Przykład installed_apps: %s",
                 json.dumps(data.get("installed_apps", [])[:3], ensure_ascii=False))
    logging.info("[DEBUG] Przykład available_app_updates: %s",
                 json.dumps(data.get("available_app_updates", [])[:3], ensure_ascii=False))
    logging.info("[DEBUG] Przykład pending_os_updates: %s",
                 json.dumps(data.get("pending_os_updates", []), ensure_ascii=False)[:500])
    try:
        cur = db.cursor()
        computer = cur.execute("SELECT id FROM computers WHERE hostname = ?", (hostname,)).fetchone()
        if computer:
            computer_id = computer['id']
            cur.execute(
                "UPDATE computers SET ip_address = ?, reboot_required = ?, last_report = CURRENT_TIMESTAMP WHERE id = ?",
                (data.get('ip_address'), data.get('reboot_required', False), computer_id))
        else:
            cur.execute("INSERT INTO computers (hostname, ip_address, reboot_required) VALUES (?, ?, ?)",
                        (hostname, data.get('ip_address'), data.get('reboot_required', False)))
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
                # Sprawdzaj, czy identyczny wpis był już w ostatnich 7 dniach dla tego komputera
                exists = cur.execute("""
                    SELECT 1 FROM action_history
                    WHERE computer_id = ? AND action_type = 'OS_UPDATE_SUCCESS'
                      AND json_extract(details, '$.name') = ?
                      AND timestamp > datetime('now', '-7 days')
                """, (computer_id, update_name)).fetchone()
                if exists:
                    continue  # pomiń duplikat
                cur.execute("INSERT INTO action_history (computer_id, action_type, details) VALUES (?, ?, ?)",
                            (computer_id, 'OS_UPDATE_SUCCESS', json.dumps({"name": update_name})))
        db.commit()
    except Exception as e:
        db.rollback();
        logging.error(f"Krytyczny błąd podczas przetwarzania raportu od {hostname}: {e}", exc_info=True)
        return "Internal Server Error", 500
    return "Report received successfully", 200

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
    latest_report_q = db.execute("SELECT id FROM reports WHERE computer_id = ? ORDER BY report_timestamp DESC LIMIT 1",
                                 (computer_id,)).fetchone()
    if not latest_report_q:
        db.commit()
        return "Result received, but no report found to enrich data.", 200
    latest_report_id = latest_report_q['id']
    action_type, details_dict = "", {}
    if command == 'update_package':
        app_details = db.execute(
            "SELECT name, current_version, available_version FROM updates WHERE report_id = ? AND app_id = ?",
            (latest_report_id, package_id)).fetchone()
        app_name = app_details['name'] if app_details else package_id
        if status == 'zakończone' and app_details:
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


@app.route('/report/snapshot/<int:report_id>')
def report_snapshot(report_id):
    report = get_db().execute(
        "SELECT c.hostname FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report: abort(404)
    content = generate_snapshot_report_content(report_id)
    filename = f"report_snapshot-{report_id}_{report['hostname']}_{datetime.now().strftime('%Y%m%d')}.txt"
    return Response(content, mimetype='text/plain', headers={"Content-disposition": f"attachment; filename={filename}"})


def generate_report_content(computer_ids):
    db, content = get_db(), []
    for cid in computer_ids:
        computer = db.execute("SELECT * FROM computers WHERE id = ?", (cid,)).fetchone()
        if not computer: continue
        content.append(f"# RAPORT DLA KOMPUTERA: {computer['hostname']} ({computer['ip_address']})")
        content.append(f"Data wygenerowania: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}")
        content.append("")
        content.append("## Dziennik Zdarzeń (ostatnie 20, bez powtórek)")
        history = db.execute(
            "SELECT timestamp, action_type, details FROM action_history WHERE computer_id = ? ORDER BY timestamp DESC",
            (cid,)).fetchall()
        # FILTR NA UNIKALNE ZDARZENIA:
        seen = set()
        unique_events = []
        for item in history:
            details = json.loads(item['details'])
            # Możesz zbudować klucz z typu zdarzenia i nazwy aktualizacji
            key = (item['action_type'], details.get('name', ''))
            if key not in seen:
                seen.add(key)
                unique_events.append(item)
            if len(unique_events) >= 20:  # limit jak wcześniej
                break
        if unique_events:
            for item in unique_events:
                details = json.loads(item['details'])
                log_entry = f"* [{to_local_time_filter(item['timestamp'])}] "
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
            content.append("")
            content.append("## Oczekujące aktualizacje (wg ostatniego raportu)")
            updates = db.execute("SELECT name, current_version, available_version FROM updates WHERE report_id = ?",
                                 (latest_report['id'],)).fetchall()
            if updates:
                [content.append(f"* {item['name']}: {item['current_version']} -> {item['available_version']}") for item
                 in updates]
            else:
                content.append("* Brak oczekujących aktualizacji.")
            content.append("")
            content.append("## Zainstalowane aplikacje (wg ostatniego raportu)")
            apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?",
                              (latest_report['id'],)).fetchall()
            if apps:
                [content.append(f"* {item['name']} ({item['version']})") for item in apps]
            else:
                content.append("* Brak aplikacji.")
        content.append("")
        content.append("=" * 80)
        content.append("")
    return "\n".join(content)


def generate_snapshot_report_content(report_id):
    db, content = get_db(), []
    report = db.execute(
        "SELECT r.id, r.report_timestamp, c.hostname, c.ip_address FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report: return "Nie znaleziono raportu."
    content.append(f"# RAPORT HISTORYCZNY DLA: {report['hostname']} ({report['ip_address']})")
    content.append(f"# Migawka z dnia: {to_local_time_filter(report['report_timestamp'])}")
    content.append(f"Data wygenerowania pliku: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}")
    content.append("")
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
    content.append("")
    content.append("## Zainstalowane aplikacje w tym raporcie")
    apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?", (report_id,)).fetchall()
    if apps:
        [content.append(f"* {item['name']} ({item['version']})") for item in apps]
    else:
        content.append("* Brak.")
    content.append("")
    return "\n".join(content)

@app.route('/settings/generate_exe', methods=['POST'])
def generate_exe():
    if not shutil.which("pyinstaller"):
        flash("Błąd serwera: Program 'pyinstaller' nie jest zainstalowany.", "error")
        return redirect(url_for('settings'))


    config = {
        "api_endpoint_1": request.form.get('api_endpoint_1', ''),
        "api_endpoint_2": request.form.get('api_endpoint_2', ''),
        "api_key": request.form.get('api_key'),
        "loop_interval": int(request.form.get('loop_interval', 15)),
        "report_interval": int(request.form.get('report_interval', 240)),
        "winget_path": request.form.get('winget_path', ''),  # jeśli masz
        "blacklist_keywords": request.form.get('blacklist_keywords', '')
    }

    blacklist_str = ', '.join([f"'{kw.strip()}'" for kw in config['blacklist_keywords'].splitlines() if kw.strip()])

    logging.info(
        f"GENERATOR: endpoint1={config['api_endpoint_1']} endpoint2={config['api_endpoint_2']} key={config['api_key']}")

    final_agent_code = AGENT_TEMPLATE \
        .replace('__BLACKLIST_KEYWORDS__', blacklist_str) \
        .replace('__API_ENDPOINT_1__', config['api_endpoint_1']) \
        .replace('__API_ENDPOINT_2__', config['api_endpoint_2']) \
        .replace('__API_KEY__', config['api_key']) \
        .replace('__LOOP_INTERVAL__', str(config['loop_interval'])) \
        .replace('__REPORT_INTERVAL__', str(config['report_interval'])) \
        .replace('__WINGET_PATH__', config['winget_path'])

    build_dir = os.path.abspath("C:\\tmp\\winget_agent_build")
    os.makedirs(build_dir, exist_ok=True)
    script_path = os.path.join(build_dir, "agent.py")  # << NAZWA agent.py

    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(final_agent_code)

        command = [
            "pyinstaller", "--onefile",
            "--hidden-import=win32timezone",
            # "--noconsole", # Pamiętaj o usunięciu/zakomentowaniu tej linii do debugowania
            "--distpath", os.path.join(build_dir, 'dist'),
            "--workpath", os.path.join(build_dir, 'build'),
            "--specpath", build_dir,
            script_path
        ]

        logging.info(f"Uruchamianie PyInstaller: {' '.join(command)}")
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"PyInstaller output: {result.stdout}")

        exe_name = "agent.exe"
        output_exe_path = os.path.join(build_dir, 'dist', exe_name)

        if not os.path.exists(output_exe_path):
            # Na wszelki wypadek, jak pyinstaller wygenerował .exe o nazwie agent.py.exe
            exe_files = [f for f in os.listdir(os.path.join(build_dir, 'dist')) if f.endswith('.exe')]
            if exe_files:
                output_exe_path = os.path.join(build_dir, 'dist', exe_files[0])
            else:
                raise FileNotFoundError(f"PyInstaller nie stworzył pliku .exe. Logi: {result.stderr}")

        return send_file(
            output_exe_path,
            as_attachment=True,
            download_name='agent.exe',
            mimetype='application/vnd.microsoft.portable-executable'
        )
    except subprocess.CalledProcessError as e:
        logging.error(f"Błąd kompilacji PyInstaller: {e.stderr}")
        return f"<h1>Błąd podczas kompilacji</h1><pre>{e.stderr}</pre>", 500
    except Exception as e:
        logging.error(f"Wystąpił nieoczekiwany błąd: {e}", exc_info=True)
        return "Wystąpił nieoczekiwany błąd serwera.", 500
    # finally:
    #     logging.info(f"Usuwanie folderu tymczasowego: {build_dir}")
    #     shutil.rmtree(build_dir, ignore_errors=True)
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)