document.addEventListener('DOMContentLoaded', () => {

    // Logika dla przełącznika motywu
    const toggleButton = document.getElementById('theme-toggle');
    if (toggleButton) {
        toggleButton.addEventListener('click', () => {
            document.documentElement.classList.toggle('dark-mode');
            localStorage.setItem('theme', document.documentElement.classList.contains('dark-mode') ? 'dark' : 'light');
        });
    }

    // Ulepszona logika dla przycisków Odśwież
    document.querySelectorAll('.refresh-btn').forEach(button => {
        button.addEventListener('click', function() {
            const computerId = this.dataset.computerId;
            const buttonCell = this.closest('.actions-cell'); // Działa na stronie głównej
            const notificationBar = document.getElementById('notification-bar'); // Działa na stronie szczegółów

            this.textContent = 'Wysyłanie...';
            this.disabled = true;

            fetch(`/computer/${computerId}/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    const message = 'Zlecono odświeżenie. Dane pojawią się po następnym raporcie agenta.';
                    // Dedykowana obsługa dla strony głównej
                    if (buttonCell) {
                        buttonCell.innerHTML = `<span class="status-pending" style="font-size: 12px;">${message}</span>`;
                    }
                    // Dedykowana obsługa dla strony szczegółów
                    if (notificationBar) {
                        notificationBar.textContent = message;
                        notificationBar.style.backgroundColor = '#007bff';
                        notificationBar.style.display = 'block';
                    }
                    // Nie przeładowujemy strony, aby użytkownik widział status
                } else {
                    this.textContent = 'Błąd!';
                    this.disabled = false;
                }
            }).catch(error => {
                this.textContent = 'Błąd sieci!';
                this.disabled = false;
            });
        });
    });

    // Logika dla przycisków "Aktualizuj" (bez zmian)
    document.querySelectorAll('.update-btn:not(.uninstall-btn)').forEach(button => {
        button.addEventListener('click', function() {
            const computerId = this.dataset.computerId;
            const updateId = this.dataset.updateId;
            const packageId = this.dataset.packageId;
            const appName = this.closest('tr').cells[1].textContent;
            if (!confirm(`Czy na pewno chcesz zlecić aktualizację aplikacji "${appName}"?`)) return;
            this.textContent = 'Zlecanie...';
            this.disabled = true;
            fetch(`/computer/${computerId}/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ package_id: packageId, update_id: updateId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    this.textContent = 'Zlecono';
                    setTimeout(() => location.reload(), 1500);
                } else {
                    this.textContent = 'Błąd!';
                    this.disabled = false;
                    alert('Wystąpił błąd po stronie serwera: ' + data.message);
                }
            }).catch(error => {
                this.textContent = 'Błąd sieci!';
                this.disabled = false;
            });
        });
    });

    // Logika dla przycisków "Odinstaluj" (bez zmian)
    document.querySelectorAll('.uninstall-btn').forEach(button => {
        button.addEventListener('click', function() {
            const computerId = this.dataset.computerId;
            const packageId = this.dataset.packageId;
            const appName = this.closest('tr').cells[0].textContent;
            const notificationBar = document.getElementById('notification-bar');
            if (!confirm(`Czy na pewno chcesz zlecić deinstalację aplikacji "${appName}"?\\n\\nUWAGA: Ta akcja jest nieodwracalna!`)) return;
            this.textContent = 'Zlecanie...';
            this.disabled = true;
            fetch(`/computer/${computerId}/uninstall`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ package_id: package_id })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    this.textContent = 'Zlecono';
                    notificationBar.textContent = `Zlecono deinstalację dla "${appName}". Strona odświeży się automatycznie za ok. 20 sekund.`;
                    notificationBar.style.backgroundColor = '#ffc107';
                    notificationBar.style.display = 'block';
                    setTimeout(() => { location.reload(); }, 20000);
                } else {
                    this.textContent = 'Błąd!';
                    this.disabled = false;
                    notificationBar.textContent = `Wystąpił błąd podczas zlecania deinstalacji: ${data.message}`;
                    notificationBar.style.backgroundColor = '#dc3545';
                    notificationBar.style.display = 'block';
                }
            }).catch(error => {
                this.textContent = 'Błąd sieci!';
                this.disabled = false;
            });
        });
    });
});