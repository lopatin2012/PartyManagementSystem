// ==========================================
// Расписание фоновых задач (виджет + модалка)
// ==========================================
let schedulerData = [];
let schedulerFilter = 'all';
let nkSyncState = null;

async function loadSchedulerStatus() {
    try {
        const response = await fetch('/scheduler/status/');
        if (!response.ok) return;

        const data = await response.json();
        if (data.is_error) return;

        schedulerData = data.schedule || [];
        nkSyncState = data.nk_sync || null;
        updateSchedulerWidget();
        renderSchedulerModal();
    } catch (error) {
        console.error('Ошибка загрузки расписания:', error);
    }
}

function updateSchedulerWidget() {
    const stats = document.getElementById('schedulerStats');
    const nextEl = document.getElementById('schedulerNext');

    const total = schedulerData.length;
    const errors = schedulerData.filter(t => t.has_recent_error).length;

    stats.innerHTML = `
        <span class="stat-pill stat-total" title="Всего задач: ${total}">
            <span class="stat-value">${total}</span>
        </span>
        ${errors > 0 ? `
            <span class="stat-pill stat-errors" title="С ошибками: ${errors}">
                <span class="stat-value">${errors}</span>
            </span>
        ` : ''}
        ${nkSyncState && nkSyncState.running ? `
            <span class="stat-pill stat-errors" title="Синхронизация НК выполняется (${nkSyncState.started_by_display})">
                <span class="stat-value">⏳</span>
            </span>
        ` : ''}
    `;

    if (nkSyncState && nkSyncState.running) {
        nextEl.innerHTML =
            `<span class="next-label">НК:</span> <strong>синхронизация выполняется</strong>` +
            ` <span class="next-time">(${nkSyncState.started_by_display})</span>`;
    } else {
        // Ближайший следующий запуск
        const now = new Date();
        const soonest = schedulerData
            .filter(t => t.next_run && t.next_run !== 'Первый запуск' && !t.next_run.includes('Сейчас'))
            .map(t => ({ ...t, nextDate: parseNextRun(t.next_run) }))
            .filter(t => t.nextDate && t.nextDate > now)
            .sort((a, b) => a.nextDate - b.nextDate)[0];

        if (soonest) {
            const diff = soonest.nextDate - now;
            const timeLeft = formatTimeLeft(diff);
            nextEl.innerHTML = `<span class="next-label">Следующий:</span> <strong>${soonest.description}</strong> <span class="next-time">(через ${timeLeft})</span>`;
        } else {
            nextEl.textContent = 'Нет запланированных запусков';
        }
    }
}

function parseNextRun(str) {
    if (!str) return null;
    const parts = str.match(/(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})/);
    if (!parts) return null;
    return new Date(+parts[3], +parts[2] - 1, +parts[1], +parts[4], +parts[5], +parts[6]);
}

function formatTimeLeft(ms) {
    if (ms < 0) return 'сейчас';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `${sec} сек`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} мин`;
    const hours = Math.floor(min / 60);
    if (hours < 24) return `${hours} ч ${min % 60} мин`;
    const days = Math.floor(hours / 24);
    return `${days} д ${hours % 24} ч`;
}

let schedulerPollTimer = null;

function openSchedulerModal() {
    document.getElementById('schedulerModal').classList.remove('hidden');
    loadSchedulerStatus();
    // Пока окно открыто — обновляем чаще, чтобы видеть прогресс задач.
    if (!schedulerPollTimer) {
        schedulerPollTimer = setInterval(loadSchedulerStatus, 5000);
    }
}

function closeSchedulerModal() {
    document.getElementById('schedulerModal').classList.add('hidden');
    if (schedulerPollTimer) {
        clearInterval(schedulerPollTimer);
        schedulerPollTimer = null;
    }
}

function setSchedulerFilter(filter) {
    schedulerFilter = filter;
    document.querySelectorAll('.scheduler-filters .filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    renderSchedulerModal();
}

function filterSchedulerTasks(query) {
    window._schedulerQuery = (query || '').toLowerCase();
    renderSchedulerModal();
}

function renderSchedulerModal() {
    const container = document.getElementById('schedulerGroups');
    if (!container) return;

    // Баннер о выполняющейся синхронизации НК (если есть).
    const nkBanner = nkSyncState && nkSyncState.running ? `
        <div class="nk-sync-banner">
            ⏳ Синхронизация Национального каталога выполняется
            ${nkSyncState.started_by_display || ''}.
            ${nkSyncState.message ? 'Сообщение: ' + nkSyncState.message : ''}
        </div>
    ` : '';

    // Фильтрация
    let filtered = schedulerData.slice();
    const query = (window._schedulerQuery || '').toLowerCase();
    if (query) {
        filtered = filtered.filter(t =>
            t.description.toLowerCase().includes(query) ||
            t.name.toLowerCase().includes(query)
        );
    }
    if (schedulerFilter === 'errors') {
        filtered = filtered.filter(t => t.has_recent_error);
    } else if (schedulerFilter === 'soon') {
        const now = new Date();
        const in1hour = new Date(now.getTime() + 60 * 60 * 1000);
        filtered = filtered.filter(t => {
            const d = parseNextRun(t.next_run);
            return d && d > now && d < in1hour;
        });
    }

    // Счётчики
    document.getElementById('countAll').textContent = schedulerData.length;
    document.getElementById('countErrors').textContent = schedulerData.filter(t => t.has_recent_error).length;
    const now = new Date();
    const in1hour = new Date(now.getTime() + 60 * 60 * 1000);
    document.getElementById('countSoon').textContent = schedulerData.filter(t => {
        const d = parseNextRun(t.next_run);
        return d && d > now && d < in1hour;
    }).length;

    // Группировка (по категории из name)
    const groups = categorizeTasks(filtered);

    if (Object.keys(groups).length === 0 && !nkBanner) {
        container.innerHTML = '<div class="scheduler-empty">Нет задач по выбранным фильтрам</div>';
        return;
    }

    // Рендеринг
    container.innerHTML = nkBanner + Object.entries(groups).map(([groupName, tasks]) => `
        <div class="scheduler-group">
            <div class="scheduler-group-title">
                <span>${groupName}</span>
                <span class="group-count">${tasks.length}</span>
            </div>
            <div class="scheduler-task-list">
                ${tasks.map(renderTaskCard).join('')}
            </div>
        </div>
    `).join('');

    document.getElementById('schedulerUpdated').textContent =
        `Обновлено: ${new Date().toLocaleTimeString('ru-RU')}`;
}

function categorizeTasks(tasks) {
    // Группируем по префиксу в name
    const groups = {};
    tasks.forEach(task => {
        let category = 'Другие';
        const name = task.name.toLowerCase();
        if (name.includes('suz') || name.includes('cz') || name.includes('parties')) {
            category = 'Честный Знак';
        } else if (name.includes('uip') || name.includes('reserved') || name.includes('registered') || name.includes('closed')) {
            category = 'Управление УИП';
        } else if (name.includes('cleanup') || name.includes('logs')) {
            category = 'Очистка данных';
        } else if (name.includes('sync')) {
            category = 'Синхронизация';
        }
        if (!groups[category]) groups[category] = [];
        groups[category].push(task);
    });

    // Сортировка групп
    const order = ['Честный Знак', 'Управление УИП', 'Синхронизация', 'Очистка данных', 'Другие'];
    const sorted = {};
    order.forEach(cat => { if (groups[cat]) sorted[cat] = groups[cat]; });
    return sorted;
}

function renderTaskCard(task) {
    const running = !!task.is_running;

    let statusClass, statusIcon, statusText;
    if (running) {
        statusClass = 'status-running';
        statusIcon = '⟳';
        statusText = 'Выполняется';
    } else if (task.has_recent_error) {
        statusClass = 'status-error';
        statusIcon = '⚠';
        statusText = 'Ошибка';
    } else {
        statusClass = 'status-ok';
        statusIcon = '✓';
        statusText = 'OK';
    }

    const nextBlock = running
        ? `<div class="next-label">Выполняется с:</div>
           <div class="next-value next-running">${task.started_at || 'сейчас'}</div>`
        : `<div class="next-label">Следующий:</div>
           <div class="next-value">${task.next_run}</div>`;

    return `
        <div class="scheduler-task-card ${task.has_recent_error ? 'has-error' : ''} ${running ? 'is-running' : ''}">
            <div class="task-main">
                <span class="task-status ${statusClass}" title="${statusText}">${statusIcon}</span>
                <div class="task-info">
                    <div class="task-name">${task.description}</div>
                    <div class="task-meta">
                        <span class="meta-interval">⏱ ${task.interval_display}</span>
                        <span class="meta-last">Прошлый: ${task.last_run || '—'}</span>
                        ${running ? '<span class="meta-running">⏳ выполняется</span>' : ''}
                    </div>
                    ${running ? renderTaskProgress(task) : ''}
                </div>
            </div>
            <div class="task-next">
                ${nextBlock}
                <button type="button" class="task-run" title="Запустить сейчас"
                        onclick="runScheduledTask('${task.name}', this)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <polygon points="6 3 20 12 6 21"></polygon>
                    </svg>
                    <span>Запуск</span>
                </button>
            </div>
        </div>
    `;
}

function renderTaskProgress(task) {
    const progress = task.progress;
    if (!progress) {
        return '<div class="task-progress"><div class="task-progress-message">Выполняется…</div></div>';
    }

    const hasCount = progress.total != null && progress.current != null
        && progress.total > 0;
    const percent = progress.percent != null
        ? progress.percent
        : (hasCount ? Math.min(100, Math.round(progress.current / progress.total * 100)) : 0);

    return `
        <div class="task-progress">
            <div class="task-progress-head">
                <span class="task-progress-phase">${progress.phase || 'Выполняется'}</span>
                ${hasCount ? `<span class="task-progress-count">${progress.current}/${progress.total}</span>` : ''}
            </div>
            ${hasCount ? `<div class="task-progress-bar"><div class="task-progress-fill" style="width:${percent}%"></div></div>` : ''}
            ${progress.message ? `<div class="task-progress-message">${progress.message}</div>` : ''}
        </div>
    `;
}

async function runScheduledTask(name, btn) {
    if (!confirm('Запустить задачу вручную сейчас?')) return;

    const tokenEl = document.getElementById('schedulerCsrfToken');
    const token = tokenEl ? tokenEl.value : '';

    btn.disabled = true;
    try {
        const response = await fetch(`/scheduler/run/${encodeURIComponent(name)}/`, {
            method: 'POST',
            headers: {
                'X-CSRFToken': token,
                'X-Requested-With': 'XMLHttpRequest',
            },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.is_error) {
            alert(data.message || 'Не удалось запустить задачу.');
        } else {
            await loadSchedulerStatus();
        }
    } catch (error) {
        console.error('Ошибка запуска задачи:', error);
        alert('Ошибка запуска задачи.');
    } finally {
        btn.disabled = false;
    }
}

// Закрытие модалки по клику вне контента
document.getElementById('schedulerModal')?.addEventListener('click', function (e) {
    if (e.target === this) closeSchedulerModal();
});

// Закрытие по Escape
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeSchedulerModal();
});

// Запуск
loadSchedulerStatus();
setInterval(loadSchedulerStatus, 60000);