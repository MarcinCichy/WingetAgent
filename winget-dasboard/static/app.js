// Czekaj, aż cała strona się załaduje, zanim podepniesz skrypty do przycisków
document.addEventListener('DOMContentLoaded', () => {

    // Logika dla przełącznika motywu - jest na każdej stronie
    const toggleButton = document.getElementById('theme-toggle');
    const htmlEl = document.documentElement;
    if(toggleButton) {
        toggleButton.addEventListener('click', () => {
            htmlEl.classList.toggle('dark-mode');
            localStorage.setItem('theme', htmlEl.classList.contains('dark-mode') ? 'dark' : 'light');
        });
    }

    // Logika dla przycisku Odśwież (działa na index.html i computer.html)
    document.querySelectorAll('.refresh-btn').forEach(button => {
        button.addEventListener('click', function() {
            const computerId = this.dataset.computerId;
            const notificationBar = document.getElementById('notification-bar'); // Może istnieć lub nie
            const buttonCell = this.parentElement;

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
                    this.textContent = 'Zlecono';
                    let message = `Zlecono odświeżenie. Strona przeładuje się automatycznie za ok. 20 sekund.`;
                    if (notificationBar) {
                        notificationBar.textContent = message;
                        notificationBar.style.backgroundColor = '#007bff';
                        notificationBar.style.display = 'block';
                    } else if(buttonCell) {
                         buttonCell.innerHTML = `<span class="status-pending">${message}</span>`;
                    }
                    setTimeout(() => { location.reload(); }, 20000);
                    //location.reload();
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

    // Logika dla przycisków "Aktualizuj" (tylko na computer.html)
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

    // Logika dla przycisków "Odinstaluj" (tylko na computer.html)
    document.querySelectorAll('.uninstall-btn').forEach(button => {
        button.addEventListener('click', function() {
            const computerId = this.dataset.computerId;
            const packageId = this.dataset.packageId;
            const appName = this.closest('tr').cells[0].textContent;
            const notificationBar = document.getElementById('notification-bar');
            if (!confirm(`Czy na pewno chcesz zlecić deinstalację aplikacji "${appName}"?\n\nUWAGA: Ta akcja jest nieodwracalna!`)) return;
            this.textContent = 'Zlecanie...';
            this.disabled = true;
            fetch(`/computer/${computerId}/uninstall`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ package_id: packageId })
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

document.addEventListener('DOMContentLoaded', function() {
    var textarea = document.getElementById('blacklist_keywords');
    if (textarea) {
        textarea.addEventListener('blur', function() {
            var cleaned = textarea.value.split('\n').map(line => line.trim()).join('\n');
            textarea.value = cleaned;
        });
    }
});