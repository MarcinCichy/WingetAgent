import os
import json
import socket
import time
import logging
import subprocess
import sys  # <-- TO JEST BRAKUJĄCY, KLUCZOWY IMPORT
from dotenv import load_dotenv
import requests

# Ładowanie zmiennych z pliku .env
load_dotenv()

# --- Konfiguracja odczytywana z pliku .env ---
API_ENDPOINT = os.getenv('AGENT_API_ENDPOINT', "http://127.0.0.1:5000/api/report")
API_KEY = os.getenv('API_KEY')
LOOP_INTERVAL_SECONDS = int(os.getenv('AGENT_LOOP_INTERVAL', 15))
FULL_REPORT_INTERVAL_LOOPS = int(os.getenv('AGENT_FULL_REPORT_INTERVAL', 240))

# Konfiguracja logowania
log_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(log_dir, 'agent.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def run_command(command):
    """Uruchamia polecenie w powłoce, wymuszając kodowanie UTF-8."""
    try:
        full_command = (
            f"[System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"$OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"{command}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", full_command],
            capture_output=True, text=True, check=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Błąd podczas wykonywania polecenia '{command}': {e.stderr}")
        return None
    except FileNotFoundError:
        logging.error("Nie znaleziono polecenia 'powershell.exe'.")
        return None


def get_system_info():
    hostname = socket.gethostname()
    try:
        ip_address = socket.gethostbyname(hostname)
    except socket.gaierror:
        ip_address = '127.0.0.1'
    return {"hostname": hostname, "ip_address": ip_address}


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

    apps, lines = [], output.strip().split('\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line: header_line = line; break
    if not header_line: return []

    pos_id, pos_version, pos_available, pos_source = header_line.find("Id"), header_line.find(
        "Version"), header_line.find("Available"), header_line.find("Source")
    if pos_available == -1: pos_available = pos_source if pos_source != -1 else len(header_line) + 20

    for line in lines:
        if (line.strip().startswith("---") or not line.strip() or line == header_line or len(
            line) < pos_version): continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            version, source = line[pos_version:pos_available].strip(), line[
                                                                       pos_source:].strip() if pos_source != -1 else ""
            if not name or name.lower() == 'name': continue
            name_lower = name.lower()
            if any(k in name_lower for k in BLACKLIST_KEYWORDS): continue
            if name_lower.startswith('microsoft') and not any(
                w in name_lower for w in MICROSOFT_APP_WHITELIST): continue
            if source.lower() == 'msstore' and not any(w in name_lower for w in MICROSOFT_APP_WHITELIST): continue
            apps.append({"name": name, "id": id_, "version": version})
        except Exception as e:
            logging.warning(f"Nie udało się sparsować linii aplikacji: {line} | Błąd: {e}")

    logging.info(f"Znaleziono {len(apps)} przefiltrowanych aplikacji.")
    return apps


def get_available_updates():
    logging.info("Sprawdzanie dostępnych aktualizacji aplikacji...")
    output = run_command("winget upgrade --accept-source-agreements")
    if not output: return []

    updates, lines = [], output.strip().split('\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line: header_line = line; break
    if not header_line: return []

    pos_id, pos_version, pos_available, pos_source = header_line.find("Id"), header_line.find(
        "Version"), header_line.find("Available"), header_line.find("Source")

    for line in lines:
        if line.strip().startswith("---") or "upgrades available" in line.lower() or line == header_line or len(
            line) < pos_available: continue
        try:
            name, id_ = line[:pos_id].strip(), line[pos_id:pos_version].strip()
            current_version, available_version = line[pos_version:pos_available].strip(), line[
                                                                                          pos_available:pos_source].strip()
            if name and name != 'Name':
                updates.append({"name": name, "id": id_, "current_version": current_version,
                                "available_version": available_version})
        except Exception as e:
            logging.warning(f"Nie udało się inteligentnie sparsować linii aktualizacji: {line} | Błąd: {e}")
    return updates


def get_windows_updates():
    logging.info("Sprawdzanie aktualizacji systemu Windows (z filtrem RebootRequired)...")
    command = "try { (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search(\"IsInstalled=0 and Type='Software' and IsHidden=0 and RebootRequired=0\").Updates | ForEach-Object { [PSCustomObject]@{ Title = $_.Title; KB = $_.KBArticleIDs; Description = $_.Description } } | ConvertTo-Json -Depth 3 } catch { return '[]' }"
    output = run_command(command)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            logging.error("Błąd dekodowania JSON z Windows Updates."); return []
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
    try:
        logging.info(f"Wysyłanie pełnego raportu do {API_ENDPOINT} dla {system_info['hostname']}")
        requests.post(API_ENDPOINT, data=json.dumps(payload), headers=headers, timeout=60).raise_for_status()
        logging.info("Pełny raport wysłany pomyślnie.")
    except Exception as e:
        logging.error(f"Nie udało się wysłać pełnego raportu. Błąd: {e}")


def process_tasks(hostname):
    logging.info("Sprawdzanie dostępnych zadań...")
    base_url = API_ENDPOINT.replace('/report', '')
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}
    try:
        response = requests.get(f"{base_url}/tasks/{hostname}", headers=headers, timeout=15)
        response.raise_for_status()
        tasks = response.json()
        if not tasks: logging.info("Brak nowych zadań."); return
        for task in tasks:
            logging.info(f"Odebrano zadanie ID {task['id']}: {task['command']} z payloadem {task['payload']}")
            task_result_payload, status_final = {"task_id": task['id']}, 'błąd'
            if task['command'] == 'update_package':
                package_id = task['payload']
                update_command = f'winget upgrade --id "{package_id}" --accept-package-agreements --accept-source-agreements --disable-interactivity'
                if run_command(update_command) is not None:
                    status_final = 'zakończone';
                    task_result_payload['details'] = {"name": package_id}
            elif task['command'] == 'uninstall_package':
                package_id = task['payload']
                uninstall_command = f'winget uninstall --id "{package_id}" --accept-source-agreements --disable-interactivity --silent'
                if run_command(uninstall_command) is not None:
                    status_final = 'zakończone'
            elif task['command'] == 'force_report':
                collect_and_report();
                status_final = 'zakończone'
            task_result_payload['status'] = status_final
            requests.post(f"{base_url}/tasks/result", headers=headers, data=json.dumps(task_result_payload))
            logging.info(f"Zakończono przetwarzanie zadania {task['id']} ze statusem: {status_final}")
    except Exception as e:
        logging.error(f"Nie udało się pobrać lub przetworzyć zadań: {e}")


if __name__ == '__main__':

    def run_agent_once():
        logging.info("Agent uruchomiony w trybie jednorazowym.")
        try:
            logging.info("Wysyłanie raportu...")
            collect_and_report()
            logging.info("Raport wysłany pomyślnie.")
            hostname = get_system_info()["hostname"]
            process_tasks(hostname)
        except Exception as e:
            logging.critical(f"Wystąpił błąd podczas jednorazowego uruchomienia: {e}", exc_info=True)
        logging.info("Zakończono działanie w trybie jednorazowym.")


    if len(sys.argv) > 1 and sys.argv[1] == 'run_once':
        run_agent_once()
    else:
        logging.info("Agent został uruchomiony w trybie pętli (usługi).")
        try:
            logging.info("Wysyłanie początkowego raportu przy starcie...");
            collect_and_report()
            logging.info("Początkowy raport wysłany pomyślnie.")
        except Exception as e:
            logging.critical(f"Nie udało się wysłać raportu początkowego: {e}", exc_info=True)

        report_counter = 0
        while True:
            try:
                hostname = get_system_info()["hostname"]
                process_tasks(hostname)
                report_counter += 1
                if report_counter >= FULL_REPORT_INTERVAL_LOOPS:
                    collect_and_report();
                    report_counter = 0
                logging.info(
                    f"Cykl zakończony. Następne sprawdzenie za {LOOP_INTERVAL_SECONDS}s. Pełny raport za {FULL_REPORT_INTERVAL_LOOPS - report_counter} cykli.")
                time.sleep(LOOP_INTERVAL_SECONDS)
            except Exception as e:
                logging.critical(f"Krytyczny błąd w głównej pętli agenta: {e}", exc_info=True)
                time.sleep(LOOP_INTERVAL_SECONDS)