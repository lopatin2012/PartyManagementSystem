// static/js/sync.js
// Управление страницей «Синхронизация заданий».

function showSyncStatus(container, message, isError) {
    container.className = 'sync-status ' + (isError ? 'sync-error' : 'sync-ok');
    container.classList.remove('hidden');
    container.textContent = message;
}

/**
 * Полная синхронизация с внешним сервисом.
 */
async function syncAllTasks(btn) {
    const statusEl = document.getElementById('syncStatus');
    const url = btn.dataset.url;
    const csrf = btn.dataset.csrf;

    btn.disabled = true;
    btn.classList.add('syncing');
    showSyncStatus(statusEl, 'Синхронизация запущена…', false);

    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrf,
                'Content-Type': 'application/json'
            },
            credentials: 'same-origin'
        });

        const data = await resp.json().catch(() => ({}));

        if (!resp.ok || data.is_error) {
            showSyncStatus(
                statusEl,
                'Ошибка: ' + (data.message || ('HTTP ' + resp.status)),
                true
            );
        } else {
            showSyncStatus(statusEl, data.message || 'Синхронизация завершена', false);
            // Обновляем страницу, чтобы показать актуальные статусы.
            setTimeout(() => window.location.reload(), 1200);
        }
    } catch (e) {
        showSyncStatus(statusEl, 'Ошибка сети: ' + e.message, true);
    } finally {
        btn.disabled = false;
        btn.classList.remove('syncing');
    }
}

/**
 * Синхронизация кодов одной производственной партии.
 */
async function syncPartyCodes(btn) {
    const partyId = btn.dataset.partyId;
    const url = btn.dataset.url;
    const csrf = btn.dataset.csrf;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Синхронизация…';

    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrf,
                'Content-Type': 'application/json'
            },
            credentials: 'same-origin',
            body: JSON.stringify({ production_party_id: partyId })
        });

        const data = await resp.json().catch(() => ({}));

        if (!resp.ok || data.has_error) {
            alert('Ошибка синхронизации кодов:\n' + (data.message || ('HTTP ' + resp.status)));
        } else {
            alert(data.message || 'Коды синхронизированы');
            window.location.reload();
        }
    } catch (e) {
        alert('Ошибка сети: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}
