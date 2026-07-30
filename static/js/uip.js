async function syncParties() {
    const btn = document.getElementById('syncBtn');
    const statusDiv = document.getElementById('syncStatus');

    // Получаем URL и CSRF токен из data-атрибутов кнопки.
    const syncUrl = btn.dataset.url;
    const csrfToken = btn.dataset.csrf;

    // Блокируем кнопку и показываем анимацию.
    btn.disabled = true;
    btn.classList.add('syncing');
    btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"></polyline>
            <polyline points="1 20 1 14 7 14"></polyline>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
        </svg>
        Синхронизация...
    `;

    // Скрываем предыдущий статус
    statusDiv.style.display = 'none';

    try {
        const response = await fetch(syncUrl, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrfToken,
                'Content-Type': 'application/json'
            }
        });

        const result = await response.json();

        // Показываем результат
        statusDiv.style.display = 'block';
        statusDiv.className = 'sync-status ' + (result.is_error ? 'error' : 'success');

        if (result.is_error) {
            statusDiv.textContent =  result.message;
        } else {
            statusDiv.textContent = result.message;

            // Перезагружаем страницу через 2 секунды для отображения новых данных
            if (!result.is_error && (result.created > 0 || result.updated > 0)) {
                setTimeout(() => {
                    window.location.reload();
                }, 2000);
            }
        }

    } catch (error) {
        statusDiv.style.display = 'block';
        statusDiv.className = 'sync-status error';
        statusDiv.textContent = 'Ошибка соединения с сервером';
        console.error('Sync error:', error);
    } finally {
        // Разблокируем кнопку
        btn.disabled = false;
        btn.classList.remove('syncing');
        btn.innerHTML = `
            <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10"></polyline>
                <polyline points="1 20 1 14 7 14"></polyline>
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
            </svg>
            Синхронизировать с ЧЗ
        `;
    }
}