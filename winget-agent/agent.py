import subprocess
import json
import socket
import requests
import time
import logging
import os
import sys
import threading

# --- KONFIGURACJA (podstawiana przez panel) ---
API_ENDPOINTS = ["http://TWOJ_ADRES_IP:5000/api/report"]
API_KEY = "TWÓJ_KLUCZ_API"
LOOP_INTERVAL_SECONDS = 60
FULL_REPORT_INTERVAL_LOOPS = 60
WINGET_PATH_CONF = r""

# --- Automatyczne wykrywanie lokalizacji winget.exe ---
def find_winget_path():
    # 1. Sprawdź czy jest ustawiona ścieżka z konfiguracji (panel)
    if WINGET_PATH_CONF and os.path.isfile(WINGET_PATH_CONF):
        return WINGET_PATH_CONF

    # 2. Szukaj w systemowym PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, "winget.exe")
        if os.path.isfile(candidate):
            return candidate

    # 3. Szukaj we wszystkich znanych folderach WindowsApps użytkowników
    try:
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

# --- Logowanie ---
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

log_file = os.path.join(application_path, 'agent.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_active_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
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
