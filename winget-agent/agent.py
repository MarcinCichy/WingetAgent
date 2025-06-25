import subprocess
import json
import socket
import requests
import time
import logging
import os

# --- Konfiguracja ---
# WAŻNE: Ustaw poprawny adres URL serwera API oraz klucz!
API_ENDPOINT = "http://192.168.93.133:5000/api/report"
API_KEY = "test1234"
# Agent będzie działał w pętli co 60 sekund, sprawdzając zadania
LOOP_INTERVAL_SECONDS = 60
# Pełny raport będzie wysyłany co 60 pętli (60 * 60s = 1 godzina)
FULL_REPORT_INTERVAL_LOOPS = 60

# Konfiguracja logowania do pliku w tym samym folderze co skrypt
log_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(log_dir, 'agent.log')
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def run_command(command):
    """Uruchamia polecenie w powłoce, wymuszając kodowanie UTF-8 na dwa sposoby dla maksymalnej kompatybilności."""
    try:
        full_command = (
            "[System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
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
        logging.error("Nie znaleziono polecenia 'powershell.exe'. Upewnij się, że jest w zmiennej środowiskowej PATH.")
        return None


def get_system_info():
    """Pobiera podstawowe informacje o systemie."""
    hostname = socket.gethostname()
    try:
        ip_address = socket.gethostbyname(hostname)
    except socket.gaierror:
        ip_address = '127.0.0.1'
    return {"hostname": hostname, "ip_address": ip_address}


def get_installed_apps():
    """Pobiera listę zainstalowanych aplikacji, z zaawansowanym filtrowaniem aplikacji systemowych."""
    logging.info("Pobieranie i filtrowanie listy zainstalowanych aplikacji (metoda zaawansowana)...")
    BLACKLIST_KEYWORDS = [
        'redistributable', 'visual c++', '.net framework', 'update for windows',
        'host controller', 'java runtime', 'driver', 'odbc', 'provider', 'service pack',
        'hevc', 'heif', 'vp9', 'webp'
    ]
    output = run_command("winget list --accept-source-agreements")
    if not output: return []

    apps, lines = [], output.strip().split('\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line:
            header_line = line
            break
    if not header_line: return []

    pos_id = header_line.find("Id")
    pos_version = header_line.find("Version")
    pos_available = header_line.find("Available")
    pos_source = header_line.find("Source")
    if pos_available == -1: pos_available = pos_source if pos_source != -1 else len(header_line) + 20

    for line in lines:
        if line.strip().startswith("---") or line.strip() == "" or line == header_line or len(line) < pos_version:
            continue
        try:
            name = line[:pos_id].strip()
            id_ = line[pos_id:pos_version].strip()
            version = line[pos_version:pos_available].strip()
            source = line[pos_source:].strip() if pos_source != -1 else ""
            if not name or name.lower() == 'name': continue

            is_microsoft_app = id_.lower().startswith('microsoft.') and 'office' not in id_.lower()
            is_msstore_app = source.lower() == 'msstore'
            is_blacklisted = any(keyword in name.lower() for keyword in BLACKLIST_KEYWORDS)

            if not is_microsoft_app and not is_msstore_app and not is_blacklisted:
                apps.append({"name": name, "id": id_, "version": version})
        except Exception as e:
            logging.warning(f"Nie udało się sparsować linii aplikacji: {line} | Błąd: {e}")

    logging.info(f"Znaleziono {len(apps)} przefiltrowanych aplikacji (bez systemowych).")
    return apps


def get_available_updates():
    """Pobiera listę dostępnych aktualizacji aplikacji, używając inteligentnego parsowania."""
    logging.info("Sprawdzanie dostępnych aktualizacji aplikacji...")
    output = run_command("winget upgrade --accept-source-agreements")
    if not output: return []

    updates, lines = [], output.strip().split('\n')
    header_line = ""
    for line in lines:
        if "Name" in line and "Id" in line and "Version" in line:
            header_line = line
            break
    if not header_line: return []

    pos_id = header_line.find("Id")
    pos_version = header_line.find("Version")
    pos_available = header_line.find("Available")
    pos_source = header_line.find("Source")

    for line in lines:
        if line.strip().startswith("---") or "upgrades available" in line.lower() or line == header_line or len(
                line) < pos_available:
            continue
        try:
            name = line[:pos_id].strip()
            id_ = line[pos_id:pos_version].strip()
            current_version = line[pos_version:pos_available].strip()
            available_version = line[pos_available:pos_source].strip()
            if name and name != 'Name':
                updates.append({"name": name, "id": id_, "current_version": current_version,
                                "available_version": available_version})
        except Exception as e:
            logging.warning(f"Nie udało się inteligentnie sparsować linii aktualizacji: {line} | Błąd: {e}")
    return updates


def get_windows_updates():
    """Pobiera informacje o oczekujących aktualizacjach Windows."""
    logging.info("Sprawdzanie aktualizacji systemu Windows...")
    command = """
    $updateSession = New-Object -ComObject Microsoft.Update.Session;
    $updateSearcher = $updateSession.CreateUpdateSearcher();
    try {
        $searchResult = $updateSearcher.Search("IsInstalled=0 and Type='Software' and IsHidden=0");
        $pending_updates = @();
        foreach ($update in $searchResult.Updates) {
            $pending_updates += [PSCustomObject]@{ Title = $update.Title; KB = $update.KBArticleIDs; Description = $update.Description }
        }
        return @($pending_updates) | ConvertTo-Json -Depth 3
    } catch { return "[]" }
    """
    output = run_command(command)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return []
    return []


def collect_and_report():
    """Główna funkcja zbierająca i wysyłająca pełny raport o stanie systemu."""
    logging.info("Rozpoczynanie cyklu pełnego raportowania.")
    system_info = get_system_info()
    payload = {
        "hostname": system_info["hostname"],
        "ip_address": system_info["ip_address"],
        "installed_apps": get_installed_apps(),
        "available_app_updates": get_available_updates(),
        "pending_os_updates": get_windows_updates()
    }
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}
    try:
        logging.info(f"Wysyłanie pełnego raportu do {API_ENDPOINT} dla {system_info['hostname']}")
        response = requests.post(API_ENDPOINT, data=json.dumps(payload), headers=headers, timeout=60)
        response.raise_for_status()
        logging.info(f"Pełny raport wysłany pomyślnie. Status serwera: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Nie udało się wysłać pełnego raportu. Błąd sieciowy: {e}")


def process_tasks(hostname):
    """Sprawdza serwer w poszukiwaniu nowych zadań i wykonuje je."""
    logging.info("Sprawdzanie dostępnych zadań...")
    base_url = API_ENDPOINT.replace('/report', '')
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}

    try:
        response = requests.get(f"{base_url}/tasks/{hostname}", headers=headers, timeout=15)
        response.raise_for_status()
        tasks = response.json()

        if not tasks:
            logging.info("Brak nowych zadań.")
            return

        for task in tasks:
            logging.info(f"Odebrano zadanie ID {task['id']}: {task['command']} z payloadem {task['payload']}")
            task_result_payload = {"task_id": task['id']}
            status_final = 'błąd'  # Domyślnie ustaw status na błąd

            # --- POCZĄTEK ZMIAN ---
            if task['command'] == 'update_package':
                package_id = task['payload']
                update_command = f"winget upgrade --id \"{package_id}\" --accept-package-agreements --accept-source-agreements --disable-interactivity"

                result_output = run_command(update_command)

                if result_output is not None:
                    status_final = 'zakończone'
                    task_result_payload['details'] = {"name": package_id, "old_version": "N/A", "new_version": "N/A"}

            elif task['command'] == 'force_report':
                # Po prostu wykonaj pełne raportowanie
                collect_and_report()
                status_final = 'zakończone'
                task_result_payload['details'] = {"message": "Raport został wysłany."}
            # --- KONIEC ZMIAN ---

            # Wyślij wynik z powrotem do serwera
            task_result_payload['status'] = status_final
            requests.post(f"{base_url}/tasks/result", headers=headers, data=json.dumps(task_result_payload))
            logging.info(f"Zakończono przetwarzanie zadania {task['id']} ze statusem: {status_final}")


    except requests.exceptions.RequestException as e:
        logging.error(f"Nie udało się pobrać lub przetworzyć zadań: {e}")

if __name__ == '__main__':
    logging.info("Agent został uruchomiony.")

    # --- POPRAWIONA LOGIKA ---
    # Krok 1: Wyślij jeden pełny raport od razu po starcie agenta.
    # To zapewni, że dane w panelu pojawią się natychmiast.
    try:
        logging.info("Wysyłanie początkowego raportu przy starcie...")
        collect_and_report()
        logging.info("Początkowy raport wysłany pomyślnie.")
    except Exception as e:
        logging.critical(f"Nie udało się wysłać raportu początkowego: {e}")

    # Krok 2: Wejdź w główną pętlę, która sprawdza zadania i cyklicznie raportuje.
    report_counter = 0
    while True:
        try:
            hostname = get_system_info()["hostname"]

            # Zawsze sprawdzaj zadania w każdej pętli
            process_tasks(hostname)

            # Inkrementuj licznik do następnego pełnego raportu
            report_counter += 1

            # Wysyłaj pełny raport co określony interwał
            if report_counter >= FULL_REPORT_INTERVAL_LOOPS:
                collect_and_report()
                report_counter = 0  # Zresetuj licznik

            logging.info(
                f"Cykl zakończony. Następne sprawdzenie zadań za {LOOP_INTERVAL_SECONDS}s. Pełny raport za {FULL_REPORT_INTERVAL_LOOPS - report_counter} cykli.")
            time.sleep(LOOP_INTERVAL_SECONDS)

        except Exception as e:
            logging.critical(f"Krytyczny błąd w głównej pętli agenta: {e}")
            # W razie błędu poczekaj, aby uniknąć zapętlenia i 100% CPU
            time.sleep(LOOP_INTERVAL_SECONDS)