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

# --- OSTATECZNA WERSJA SZABLONU AGENTA WBUDOWANA W PLIK ---
AGENT_CODE_FINAL = """
import os
import json
import socket
import time
import logging
import subprocess
import requests
import sys
import threading

# --- Konfiguracja wstrzyknięta przez serwer ---
API_ENDPOINTS = [ep for ep in ["__API_ENDPOINT_1__", "__API_ENDPOINT_2__"] if ep.strip()]
API_KEY = "__API_KEY__"
LOOP_INTERVAL_SECONDS = __LOOP_INTERVAL__
FULL_REPORT_INTERVAL_LOOPS = __REPORT_INTERVAL__
WINGET_PATH_CONF = r"__WINGET_PATH__"

# --- Automatyczne wykrywanie lokalizacji winget.exe ---
def find_winget_path():
    # 1. Sprawdź czy jest ustawiona ścieżka z konfiguracji (podanej przez panel)
    if WINGET_PATH_CONF and os.path.isfile(WINGET_PATH_CONF):
        return WINGET_PATH_CONF

    # 2. Szukaj w systemowym PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "winget.exe")
        if os.path.isfile(candidate):
            return candidate

    # 3. Szukaj we wszystkich znanych folderach WindowsApps użytkowników
    try:
        import winreg
        user_root = os.path.expandvars(r"C:\\Users")
        if os.path.isdir(user_root):
            for username in os.listdir(user_root):
                winapps = os.path.join(user_root, username, "AppData", "Local", "Microsoft", "WindowsApps", "winget.exe")
                if os.path.isfile(winapps):
                    return winapps
    except Exception:
        pass

    # 4. Ostatnia próba: po prostu wywołaj "where winget"
    try:
        result = subprocess.run(["where", "winget"], capture_output=True, text=True, encoding='utf-8')
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if os.path.isfile(line.strip()):
                    return line.strip()
    except Exception:
        pass

    return None

WINGET_PATH = find_winget_path()

# --- Konfiguracja logowania ---
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

log_file = os.path.join(application_path, 'agent.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- Funkcja do wykrywania AKTYWNEGO adresu IP ---
def get_active_ip():
    try:
        # IP "na zewnątrz" (adres przez który agent wychodzi do sieci)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            # Możesz tu użyć adresu dowolnego serwera, np. 8.8.8.8, nie wymaga połączenia
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip
    except Exception as e:
        logging.error(f"Błąd pobierania aktywnego IP: {e}")
        return "127.0.0.1"

def run_command(command):
    try:
        full_command = (
            "[System.Threading.Thread]::CurrentThread.CurrentUICulture = [System.Globalization.CultureInfo]::GetCultureInfo('en-US'); "
            "[System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            + command
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", full_command],
            capture_output=True, text=True, check=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error("Błąd podczas wykonywania polecenia '%s': %s", command, e.stderr)
        return None
    except FileNotFoundError:
        logging.error("Nie znaleziono polecenia 'powershell.exe'.")
        return None

def get_system_info():
    hostname = socket.gethostname()
    ip_address = get_active_ip()
    return {"hostname": hostname, "ip_address": ip_address}

def get_reboot_status():
    logging.info("Sprawdzanie statusu wymaganego restartu...")
    command = "(New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired"
    output = run_command(command)
    return "true" in output.lower() if output else False

def get_installed_apps():
    if not WINGET_PATH or not os.path.exists(WINGET_PATH):
        logging.error("Ścieżka do winget.exe jest nieprawidłowa lub plik nie istnieje: %s", WINGET_PATH)
        return []
    logging.info(f"Pobieranie i filtrowanie listy zainstalowanych aplikacji z: {WINGET_PATH}")
    command_to_run = f'& "{WINGET_PATH}" list --accept-source-agreements'
    output = run_command(command_to_run)
    if not output: return []
    apps, lines = [], output.strip().splitlines()
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line:
            header_line = line
            break
    if not header_line:
        logging.warning("Nie znaleziono linii nagłówka w wyniku polecenia winget list.")
        return []
    pos_id = header_line.find("Id")
    pos_version = header_line.find("Version")
    pos_available = header_line.find("Available")
    pos_source = header_line.find("Source")
    if pos_available == -1: pos_available = pos_source if pos_source != -1 else len(header_line) + 20
    for line in lines:
        if line.strip().startswith("---") or not line.strip() or line == header_line or len(line) < pos_version: continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            version, source = line[pos_version:pos_available].strip(), line[pos_available:].strip() if pos_source != -1 else ""
            if not name or name.lower() == 'name': continue
            name_lower = name.lower()
            BLACKLIST_KEYWORDS = ['redistributable', 'visual c++', '.net framework']
            if any(k in name_lower for k in BLACKLIST_KEYWORDS): continue
            apps.append({"name": name, "id": id_, "version": version})
        except Exception as e:
            logging.warning("Nie udało się sparsować linii aplikacji: %s | Błąd: %s", line, e)
    logging.info("Znaleziono %d przefiltrowanych aplikacji.", len(apps))
    return apps

def get_available_updates():
    if not WINGET_PATH or not os.path.exists(WINGET_PATH): return []
    logging.info("Sprawdzanie dostępnych aktualizacji aplikacji...")
    command_to_run = f'& "{WINGET_PATH}" upgrade --accept-source-agreements'
    output = run_command(command_to_run)
    if not output: return []
    updates, lines = [], output.strip().splitlines()
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line:
            header_line = line
            break
    if not header_line:
        logging.warning("Nie znaleziono linii nagłówka w wyniku polecenia winget upgrade.")
        return []
    pos_id = header_line.find("Id")
    pos_version = header_line.find("Version")
    pos_available = header_line.find("Available")
    pos_source = header_line.find("Source")
    for line in lines:
        if line.strip().startswith("---") or "upgrades available" in line.lower() or line == header_line or len(line) < pos_available: continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            current_version, available_version = line[pos_version:pos_available].strip(), line[pos_available:pos_source].strip()
            if name and name != 'Name':
                updates.append({"name": name, "id": id_, "current_version": current_version, "available_version": available_version})
        except Exception as e:
            logging.warning("Nie udało się inteligentnie sparsować linii aktualizacji: %s | Błąd: %s", line, e)
    return updates

def get_windows_updates():
    logging.info("Sprawdzanie aktualizacji systemu Windows...")
    command = '''try { (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software' and IsHidden=0 and RebootRequired=0").Updates | ForEach-Object { [PSCustomObject]@{ Title = $_.Title; KB = $_.KBArticleIDs } } | ConvertTo-Json -Depth 3 } catch { return '[]' }'''
    output = run_command(command)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            logging.error("Błąd dekodowania JSON z Windows Updates.")
            return []
    return []

def collect_and_report():
    logging.info("Rozpoczynanie cyklu pełnego raportowania.")
    system_info = get_system_info()
    payload = {
        "hostname": system_info["hostname"], "ip_address": system_info["ip_address"],
        "reboot_required": get_reboot_status(), "installed_apps": get_installed_apps(),
        "available_app_updates": get_available_updates(), "pending_os_updates": get_windows_updates()
    }
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}
    results = []

    def send_to_endpoint(endpoint):
        try:
            logging.info("Wysyłanie pełnego raportu do %s dla %s", endpoint, system_info['hostname'])
            r = requests.post(endpoint, data=json.dumps(payload), headers=headers, timeout=60)
            r.raise_for_status()
            logging.info("Pełny raport wysłany pomyślnie do %s.", endpoint)
            results.append((endpoint, True))
        except Exception as e:
            logging.error("Nie udało się wysłać pełnego raportu do %s. Błąd: %s", endpoint, e)
            results.append((endpoint, False))

    threads = []
    for endpoint in API_ENDPOINTS:
        if endpoint.strip():
            t = threading.Thread(target=send_to_endpoint, args=(endpoint.strip(),))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    return results

def process_tasks(hostname):
    if not WINGET_PATH or not os.path.exists(WINGET_PATH): return
    logging.info("Sprawdzanie dostępnych zadań...")
    # Zadania pobieramy ze wszystkich endpointów
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}
    tasks_list = []
    for endpoint in API_ENDPOINTS:
        base_url = endpoint.strip().replace('/report', '')
        try:
            response = requests.get(base_url + "/tasks/" + hostname, headers=headers, timeout=15)
            response.raise_for_status()
            tasks = response.json()
            if tasks:
                logging.info(f"Zadania z {base_url}: {tasks}")
                tasks_list.extend([(base_url, t) for t in tasks])
        except Exception as e:
            logging.error("Nie udało się pobrać zadań z %s: %s", base_url, e)

    for base_url, task in tasks_list:
        logging.info("Odebrano zadanie ID %s: %s z payloadem %s", task['id'], task['command'], task['payload'])
        task_result_payload, status_final = {"task_id": task['id']}, 'błąd'
        if task['command'] == 'update_package':
            package_id = task['payload']
            update_command = f'& "{WINGET_PATH}" upgrade --id "{package_id}" --accept-package-agreements --accept-source-agreements --disable-interactivity'
            if run_command(update_command) is not None:
                status_final = 'zakończone'
        elif task['command'] == 'uninstall_package':
            package_id = task['payload']
            uninstall_command = f'& "{WINGET_PATH}" uninstall --id "{package_id}" --accept-source-agreements --disable-interactivity --silent'
            if run_command(uninstall_command) is not None:
                status_final = 'zakończone'
        elif task['command'] == 'force_report':
            collect_and_report()
            status_final = 'zakończone'
        task_result_payload['status'] = status_final
        try:
            requests.post(base_url + "/tasks/result", headers=headers, data=json.dumps(task_result_payload))
            logging.info("Zakończono przetwarzanie zadania %s ze statusem: %s", task['id'], status_final)
        except Exception as e:
            logging.error(f"Nie udało się wysłać wyniku zadania do {base_url}: {e}")

if __name__ == '__main__':
    logging.info("Agent uruchomiony. Sprawdzanie ścieżki do winget...")
    if not WINGET_PATH or not os.path.exists(WINGET_PATH):
        logging.critical("Ścieżka WINGET_PATH jest nieprawidłowa lub nieustawiona. Agent nie będzie mógł zarządzać aplikacjami.")

    current_hostname = get_system_info()["hostname"]

    if len(sys.argv) > 1 and sys.argv[1] == 'run_once':
        logging.info("Agent uruchomiony w trybie jednorazowym.")
        collect_and_report()
        process_tasks(current_hostname)
        logging.info("Zakończono działanie w trybie jednorazowym.")
    else:
        logging.info("Agent uruchomiony w trybie pętli (usługi).")
        collect_and_report()
        report_counter = 0
        while True:
            process_tasks(current_hostname)
            report_counter += 1
            if report_counter >= FULL_REPORT_INTERVAL_LOOPS:
                collect_and_report()
                current_hostname = get_system_info()["hostname"]
                report_counter = 0
            logging.info("Cykl zakończony. Następne sprawdzenie za %ds. Pełny raport za %d cykli.", LOOP_INTERVAL_SECONDS, FULL_REPORT_INTERVAL_LOOPS - report_counter)
            time.sleep(LOOP_INTERVAL_SECONDS)
"""

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
    return render_template('settings.html', server_api_key=API_KEY)

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
        content.append(f"Data wygenerowania: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\\n")
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
            content.append("\\n## Oczekujące aktualizacje (wg ostatniego raportu)")
            updates = db.execute("SELECT name, current_version, available_version FROM updates WHERE report_id = ?",
                                 (latest_report['id'],)).fetchall()
            if updates:
                [content.append(f"* {item['name']}: {item['current_version']} -> {item['available_version']}") for item
                 in updates]
            else:
                content.append("* Brak oczekujących aktualizacji.")
            content.append("\\n## Zainstalowane aplikacje (wg ostatniego raportu)")
            apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?",
                              (latest_report['id'],)).fetchall()
            if apps:
                [content.append(f"* {item['name']} ({item['version']})") for item in apps]
            else:
                content.append("* Brak aplikacji.")
        content.append("\\n" + "=" * 80 + "\\n")
    return "\\n".join(content)


def generate_snapshot_report_content(report_id):
    db, content = get_db(), []
    report = db.execute(
        "SELECT r.id, r.report_timestamp, c.hostname, c.ip_address FROM reports r JOIN computers c ON r.computer_id = c.id WHERE r.id = ?",
        (report_id,)).fetchone()
    if not report: return "Nie znaleziono raportu."
    content.append(f"# RAPORT HISTORYCZNY DLA: {report['hostname']} ({report['ip_address']})")
    content.append(f"# Migawka z dnia: {to_local_time_filter(report['report_timestamp'])}\\n")
    content.append(
        f"Data wygenerowania pliku: {datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y-%m-%d %H:%M:%S')}\\n")
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
    content.append("\\n## Zainstalowane aplikacje w tym raporcie")
    apps = db.execute("SELECT name, version FROM applications WHERE report_id = ?", (report_id,)).fetchall()
    if apps:
        [content.append(f"* {item['name']} ({item['version']})") for item in apps]
    else:
        content.append("* Brak.")
    return "\\n".join(content)

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
    }

    logging.info(
        f"GENERATOR: endpoint1={config['api_endpoint_1']} endpoint2={config['api_endpoint_2']} key={config['api_key']}")

    final_agent_code = AGENT_CODE_FINAL.replace('__API_ENDPOINT_1__', config['api_endpoint_1']) \
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