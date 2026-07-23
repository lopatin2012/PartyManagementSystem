// static/js/suz_status.js

// 1. Обратный отсчёт токена.
document.addEventListener('DOMContentLoaded', () => {
    const timerElement = document.getElementById('suz-countdown');
    if (timerElement) {
        let seconds = parseInt(timerElement.getAttribute('data-seconds'), 10);

        const updateTimer = () => {
            if (seconds <= 0) {
                timerElement.textContent = 'ИСТЁК';
                timerElement.style.color = '#dc3545';
                return;
            }

            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = seconds % 60;

            timerElement.textContent =
                `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;

            seconds--;
        };

        updateTimer();
        setInterval(updateTimer, 1000);
    }
});

// 2. Модальное окно.
function openSuzModal() {
    document.getElementById('suzModal').style.display = 'flex';
    loadCertificates();
}

function closeSuzModal() {
    document.getElementById('suzModal').style.display = 'none';
}

// 3. Загрузка сертификатов с детальной отладкой
async function loadCertificates() {
    const select = document.getElementById('certSelect');
    select.innerHTML = '<option value="">Загрузка...</option>';

    try {
        console.log("Запрос сертификатов к /cz/api/get-suz-certificates/...");
        const response = await fetch('/cz/api/get-suz-certificates/');

        console.log("Статус ответа:", response.status, response.statusText);
        const data = await response.json();
        console.log("Полученные данные:", data);

        if (response.ok && data.certificates) {
            select.innerHTML = '<option value="">-- Выберите сертификат --</option>';

            if (data.certificates.length === 0) {
                select.innerHTML = '<option value="">⚠️ Сертификаты не найдены (проверьте хранилище или логи сервера)</option>';
            } else {
                data.certificates.forEach(cert => {
                    const option = document.createElement('option');
                    option.value = cert.serial_number;
                    option.textContent = `${cert.fio} (до: ${cert.valid_for}, ${cert.valid_days} дн.)`;
                    select.appendChild(option);
                });
            }
        } else {
            select.innerHTML = `<option value="">❌ Ошибка: ${data.error || response.statusText}</option>`;
        }
    } catch (error) {
        console.error('Критическая ошибка при загрузке сертификатов:', error);
        select.innerHTML = '<option value="">❌ Ошибка сети или парсинга JSON</option>';
    }
}

// 4. Сохранение настроек СУЗ.
document.getElementById('suzSetupForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const data = Object.fromEntries(formData.entries());
    data.serial_number = document.getElementById('certSelect').value;

    const submitBtn = e.target.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Сохранение...';

    try {
        const response = await fetch('/cz/api/setup-suz-account/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(data)
        });

        const result = await response.json();
        if (result.success) {
            location.reload();
        } else {
            alert('Ошибка: ' + (result.error || 'Неизвестная ошибка'));
        }
    } catch (error) {
        alert('Ошибка сети');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Сохранить';
    }
});

// 5. Сброс СУЗ.
async function resetSuzData() {
    if (!confirm('Сбросить активную настройку СУЗ?')) return;

    try {
        const response = await fetch('/cz/api/reset-suz-account/', {
            method: 'POST',
            headers: { 'X-CSRFToken': getCookie('csrftoken') }
        });
        const result = await response.json();
        if (result.success) location.reload();
    } catch (error) {
        alert('Ошибка при сбросе');
    }
}

// 6. Заглушка обновления токена.
function refreshSuzToken() {
    alert('Здесь будет вызов получения нового токена через TrueAPI.');
}

// CSRF helper
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}
