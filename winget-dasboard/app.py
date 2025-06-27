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

# --- Konfiguracja odczytywana z pliku .env ---
DATABASE = os.getenv('DATABASE_FILE', 'winget_dashboard.db')
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY', 'default-secret-key-for-dev-only')

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Szablon kodu agenta (używany przez generator) ---
AGENT_CODE_TEMPLATE = """
import os, json, socket, time, logging, subprocess, requests, sys
# Ta wersja agenta nie używa dotenv, bo konfiguracja jest wkompilowana
# --- Konfiguracja wstrzyknięta przez serwer ---
API_ENDPOINT = "{api_endpoint}"
API_KEY = "{api_key}"
LOOP_INTERVAL_SECONDS = {loop_interval}
FULL_REPORT_INTERVAL_LOOPS = {report_interval}

# --- Reszta kodu agenta ---
log_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(log_dir, 'agent.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
def run_command(command):
    try:
        full_command = (f"[System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                        f"$OutputEncoding = [System.Text.Encoding]::UTF8; "
                        f"{{command}}")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", full_command], 
            capture_output=True, text=True, check=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Błąd podczas wykonywania polecenia '{{command}}': {{e.stderr}}")
        return None
    except FileNotFoundError:
        logging.error("Nie znaleziono polecenia 'powershell.exe'.")
        return None
def get_system_info():
    hostname = socket.gethostname()
    try: ip_address = socket.gethostbyname(hostname)
    except socket.gaierror: ip_address = '127.0.0.1'
    return {{"hostname": hostname, "ip_address": ip_address}}
def get_reboot_status():
    logging.info("Sprawdzanie statusu wymaganego restartu...")
    command = "(New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired"
    output = run_command(command)
    return "true" in output.lower() if output else False
def get_installed_apps():
    logging.info("Pobieranie i filtrowanie listy zainstalowanych aplikacji...")
    BLACKLIST_KEYWORDS = [
        'redistributable', 'visual c++', '.net framework', 'update for windows', 'host controller', 
        'java runtime', 'driver', 'odbc', 'provider', 'service pack', 'hevc', 'heif', 'vp9', 
        'webp', 'host experience', 'appruntime', 'web components', 'web plugins', 'windows package manager'
    ]
    MICROSOFT_APP_WHITELIST = ['office', '365', 'teams', 'visual studio', 'sql server', 'powertoys', 'edge']
    output = run_command("winget list --accept-source-agreements")
    if not output: return []
    apps, lines = [], output.strip().split('\\r\\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line: header_line = line; break
    if not header_line: return []
    pos_id, pos_version, pos_available, pos_source = header_line.find("Id"), header_line.find("Version"), header_line.find("Available"), header_line.find("Source")
    if pos_available == -1: pos_available = pos_source if pos_source != -1 else len(header_line) + 20
    for line in lines:
        if (line.strip().startswith("---") or not line.strip() or line == header_line or len(line) < pos_version): continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            version, source = line[pos_version:pos_available].strip(), line[pos_source:].strip() if pos_source != -1 else ""
            if not name or name.lower() == 'name': continue
            name_lower = name.lower()
            if any(k in name_lower for k in BLACKLIST_KEYWORDS): continue
            if name_lower.startswith('microsoft') and not any(w in name_lower for w in MICROSOFT_APP_WHITELIST): continue
            if source.lower() == 'msstore' and not any(w in name_lower for w in MICROSOFT_APP_WHITELIST): continue
            apps.append({{"name": name, "id": id_, "version": version}})
        except Exception as e: logging.warning(f"Nie udało się sparsować linii aplikacji: {{line}} | Błąd: {{e}}")
    logging.info(f"Znaleziono {{len(apps)}} przefiltrowanych aplikacji.")
    return apps
def get_available_updates():
    logging.info("Sprawdzanie dostępnych aktualizacji aplikacji...")
    output = run_command("winget upgrade --accept-source-agreements")
    if not output: return []
    updates, lines = [], output.strip().split('\\r\\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line: header_line = line; break
    if not header_line: return []
    pos_id, pos_version, pos_available, pos_source = header_line.find("Id"), header_line.find("Version"), header_line.find("Available"), header_line.find("Source")
    for line in lines:
        if line.strip().startswith("---") or "upgrades available" in line.lower() or line == header_line or len(line) < pos_available: continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            current_version, available_version = line[pos_version:pos_available].strip(), line[pos_available:pos_source].strip()
            if name and name != 'Name':
                updates.append({{"name": name, "id": id_, "current_version": current_version, "available_version": available_version}})
        except Exception as e: logging.warning(f"Nie udało się inteligentnie sparsować linii aktualizacji: {{line}} | Błąd: {{e}}")
    return updates
def get_windows_updates():
    logging.info("Sprawdzanie aktualizacji systemu Windows (z filtrem RebootRequired)...")
    command = "try {{(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search(\\"IsInstalled=0 and Type='Software' and IsHidden=0 and RebootRequired=0\\").Updates | ForEach-Object {{ [PSCustomObject]@{{ Title = $_.Title; KB = $_.KBArticleIDs; Description = $_.Description }} }} | ConvertTo-Json -Depth 3 }} catch {{ return '[]' }}"
    output = run_command(command)
    if output:
        try: return json.loads(output)
        except json.JSONDecodeError: logging.error("Błąd dekodowania JSON z Windows Updates."); return []
    return []
def collect_and_report():
    logging.info("Rozpoczynanie cyklu pełnego raportowania.")
    system_info = get_system_info()
    payload = {{
        "hostname": system_info["hostname"], "ip_address": system_info["ip_address"],
        "reboot_required": get_reboot_status(), "installed_apps": get_installed_apps(),
        "available_app_updates": get_available_updates(), "pending_os_updates": get_windows_updates()
    }}
    headers = {{"Content-Type": "application/json", "X-API-Key": API_KEY}}
    try:
        logging.info(f"Wysyłanie pełnego raportu do {{API_ENDPOINT}} dla {{system_info['hostname']}}")
        requests.post(API_ENDPOINT, data=json.dumps(payload), headers=headers, timeout=60).raise_for_status()
        logging.info("Pełny raport wysłany pomyślnie.")
    except Exception as e:
        logging.error(f"Nie udało się wysłać pełnego raportu. Błąd: {{e}}")
def process_tasks(hostname):
    logging.info("Sprawdzanie dostępnych zadań...")
    base_url = API_ENDPOINT.replace('/report', '')
    headers = {{"Content-Type": "application/json", "X-API-Key": API_KEY}}
    try:
        response = requests.get(f"{{base_url}}/tasks/{{hostname}}", headers=headers, timeout=15)
        response.raise_for_status()
        tasks = response.json()
        if not tasks: logging.info("Brak nowych zadań."); return
        for task in tasks:
            logging.info(f"Odebrano zadanie ID {{task['id']}}: {{task['command']}} z payloadem {{task['payload']}}")
            task_result_payload, status_final = {{"task_id": task['id']}}, 'błąd'
            if task['command'] == 'update_package':
                package_id = task['payload']
                update_command = f'winget upgrade --id "{{package_id}}" --accept-package-agreements --accept-source-agreements --disable-interactivity'
                if run_command(update_command) is not None:
                    status_final = 'zakończone'; task_result_payload['details'] = {{"name": package_id}}
            elif task['command'] == 'uninstall_package':
                package_id = task['payload']
                uninstall_command = f'winget uninstall --id "{{package_id}}" --accept-source-agreements --disable-interactivity --silent'
                if run_command(uninstall_command) is not None:
                    status_final = 'zakończone'
            elif task['command'] == 'force_report':
                collect_and_report(); status_final = 'zakończone'
            task_result_payload['status'] = status_final
            requests.post(f"{{base_url}}/tasks/result", headers=headers, data=json.dumps(task_result_payload))
            logging.info(f"Zakończono przetwarzanie zadania {{task['id']}} ze statusem: {{status_final}}")
    except Exception as e: 
        logging.error(f"Nie udało się pobrać lub przetworzyć zadań: {{e}}")
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run_once':
        logging.info("Agent uruchomiony w trybie jednorazowym.")
        try:
            collect_and_report()
            hostname = get_system_info()["hostname"]
            process_tasks(hostname)
        except Exception as e:
            logging.critical(f"Wystąpił błąd podczas jednorazowego uruchomienia: {{e}}", exc_info=True)
        logging.info("Zakończono działanie w trybie jednorazowym.")
    else:
        logging.info("Agent został uruchomiony w trybie pętli (usługi).")
        try:
            logging.info("Wysyłanie początkowego raportu przy starcie..."); collect_and_report()
            logging.info("Początkowy raport wysłany pomyślnie.")
        except Exception as e: logging.critical(f"Nie udało się wysłać raportu początkowego: {{e}}", exc_info=True)
        report_counter = 0
        while True:
            try:
                hostname = get_system_info()["hostname"]
                process_tasks(hostname)
                report_counter += 1
                if report_counter >= FULL_REPORT_INTERVAL_LOOPS:
                    collect_and_report(); report_counter = 0
                logging.info(f"Cykl zakończony. Następne sprawdzenie za {{LOOP_INTERVAL_SECONDS}}s. Pełny raport za {{FULL_REPORT_INTERVAL_LOOPS - report_counter}} cykli.")
                time.sleep(LOOP_INTERVAL_SECONDS)
            except Exception as e:
                logging.critical(f"Krytyczny błąd w głównej pętli agenta: {{e}}", exc_info=True)
                time.sleep(LOOP_INTERVAL_SECONDS)
"""

@app.after_request
def add_header(response):
    """Dodaje nagłówki do każdej odpowiedzi, aby zapobiec cachowaniu przez przeglądarkę."""
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response


# --- Funkcje pomocnicze, filtry i procesory kontekstu ---
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


# --- Obsługa Bazy Danych ---
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


# --- Bezpieczeństwo i Favicon ---
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


# --- GŁÓWNE STRONY (FRONTEND) ---
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
    """Wyświetla stronę z ustawieniami i formularzem generatora."""
    return render_template('settings.html', server_api_key=API_KEY)


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
                cur.execute("INSERT INTO action_history (computer_id, action_type, details) VALUES (?, ?, ?)",
                            (computer_id, 'OS_UPDATE_SUCCESS', json.dumps({"name": update_name})))
        db.commit()
    except Exception as e:
        db.rollback();
        logging.error(f"Krytyczny błąd podczas przetwarzania raportu od {hostname}: {e}", exc_info=True)
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


# --- ENDPOINTY DO GENEROWANIA RAPORTÓW ---
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
        content.append(f"Data wygenerowania: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\n")
        content.append("## Dziennik Zdarzeń (ostatnie 20)")
        history = db.execute(
            "SELECT timestamp, action_type, details FROM action_history WHERE computer_id = ? ORDER BY timestamp DESC LIMIT 20",
            (cid,)).fetchall()
        if history:
            for item in history:
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


def generate_snapshot_report_content(report_id):
    db, content = get_db(), []
    report = db.execute(
        "SELECT r.id, r.report_timestamp, c.hostname, c.ip_address FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report: return "Nie znaleziono raportu."
    content.append(f"# RAPORT HISTORYCZNY DLA: {report['hostname']} ({report['ip_address']})")
    content.append(f"# Migawka z dnia: {to_local_time_filter(report['report_timestamp'])}\n")
    content.append(
        f"Data wygenerowania pliku: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\n")
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
    content.append("\n## Zainstalowane aplikacje w tym raporcie")
    apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?", (report_id,)).fetchall()
    if apps:
        [content.append(f"* {item['name']} ({item['version']})") for item in apps]
    else:
        content.append("* Brak.")
    return "\n".join(content)


# --- NOWA FUNKCJA DO ZARZĄDZANIA CACHE ---
@app.after_request
def add_header(response):
    """Dodaje nagłówki do każdej odpowiedzi, aby zapobiec cachowaniu przez przeglądarkę."""
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response


@app.route('/settings/generate_exe', methods=['POST'])
def generate_exe():
    """Odbiera dane z formularza, generuje kod agenta, kompiluje go i oferuje do pobrania."""
    if not shutil.which("pyinstaller"):
        flash(
            "Błąd serwera: Program 'pyinstaller' nie jest zainstalowany lub nie znajduje się w ścieżce systemowej PATH.",
            "error")
        return redirect(url_for('settings'))

    config = {
        "api_endpoint": request.form.get('api_endpoint'),
        "api_key": request.form.get('api_key'),
        "loop_interval": int(request.form.get('loop_interval', 15)),
        "report_interval": int(request.form.get('report_interval', 240)),
    }

    final_agent_code = AGENT_CODE_TEMPLATE.format(**config)

    build_dir = tempfile.mkdtemp()
    script_path = os.path.join(build_dir, f"agent_{uuid.uuid4().hex}.py")

    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(final_agent_code)

        command = [
            "pyinstaller", "--onefile", "--windowed",
            "--distpath", os.path.join(build_dir, 'dist'),
            "--workpath", os.path.join(build_dir, 'build'),
            "--specpath", build_dir,
            script_path
        ]

        logging.info(f"Uruchamianie PyInstaller: {' '.join(command)}")
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.info(f"PyInstaller output: {result.stdout}")

        exe_name = os.path.basename(script_path).replace('.py', '.exe')
        output_exe_path = os.path.join(build_dir, 'dist', exe_name)

        if not os.path.exists(output_exe_path):
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
    finally:
        logging.info(f"Usuwanie folderu tymczasowego: {build_dir}")
        shutil.rmtree(build_dir, ignore_errors=True)


# --- GŁÓWNE URUCHOMIENIE ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)