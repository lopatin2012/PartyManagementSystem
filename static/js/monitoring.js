// static/js/monitoring.js

(function () {
    'use strict';

    const REFRESH_MS = 30000;
    const state = {range: '1h', timer: null};
    const numberFormat = new Intl.NumberFormat('ru-RU');

    function el(id) {
        return document.getElementById(id);
    }

    function fmt(value) {
        return numberFormat.format(Math.round(Number(value) || 0));
    }

    function fmtPct(value) {
        return (Number(value || 0) * 100).toFixed(1).replace('.', ',') + '%';
    }

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function card(label, value, hint, tone) {
        return (
            '<div class="mon-card ' + (tone || '') + '">' +
                '<div class="mon-card-label">' + esc(label) + '</div>' +
                '<div class="mon-card-value">' + value + '</div>' +
                '<div class="mon-card-hint">' + (hint || '') + '</div>' +
            '</div>'
        );
    }

    function bucketLabel(iso, resolution) {
        const date = new Date(iso);
        if (isNaN(date.getTime())) {
            return iso;
        }
        if (resolution === 'hour') {
            return date.toLocaleString('ru-RU', {
                day: '2-digit', month: '2-digit',
                hour: '2-digit', minute: '2-digit',
            });
        }
        return date.toLocaleTimeString('ru-RU', {
            hour: '2-digit', minute: '2-digit',
        });
    }

    function renderChart(container, points, series, resolution, formatValue, forcedMax) {
        const width = 800;
        const height = 220;
        const padLeft = 60;
        const padRight = 12;
        const padTop = 12;
        const padBottom = 26;
        const innerWidth = width - padLeft - padRight;
        const innerHeight = height - padTop - padBottom;
        const count = points.length;

        if (!count) {
            container.innerHTML = '<div class="mon-empty">Нет данных за период</div>';
            return;
        }

        let max = 0;
        series.forEach(function (line) {
            points.forEach(function (point) {
                max = Math.max(max, Number(point[line.key]) || 0);
            });
        });
        max = Math.max(1, max);
        if (forcedMax) {
            max = Math.max(max, forcedMax);
        }

        const stepX = count > 1 ? innerWidth / (count - 1) : 0;
        const x = function (index) {
            return padLeft + (count > 1 ? index * stepX : innerWidth);
        };
        const y = function (value) {
            return padTop + innerHeight - (value / max) * innerHeight;
        };

        const parts = [];
        parts.push(
            '<svg viewBox="0 0 ' + width + ' ' + height + '" ' +
            'role="img">'
        );

        [0, 0.5, 1].forEach(function (share) {
            const value = max * share;
            const lineY = y(value);
            parts.push(
                '<line class="mon-grid" x1="' + padLeft + '" y1="' + lineY +
                '" x2="' + (width - padRight) + '" y2="' + lineY + '"/>'
            );
            parts.push(
                '<text class="mon-axis" x="' + (padLeft - 6) + '" y="' +
                (lineY + 3) + '" text-anchor="end">' +
                esc(formatValue(value)) + '</text>'
            );
        });

        const labelIndexes = [0];
        if (count > 2) {
            labelIndexes.push(Math.floor((count - 1) / 2));
        }
        if (count > 1) {
            labelIndexes.push(count - 1);
        }
        labelIndexes.forEach(function (index) {
            let anchor = 'middle';
            let labelX = x(index);
            if (index === 0 && count > 1) {
                anchor = 'start';
                labelX = padLeft;
            } else if (index === count - 1 && count > 1) {
                anchor = 'end';
                labelX = width - padRight;
            }
            parts.push(
                '<text class="mon-axis" x="' + labelX + '" y="' +
                (height - 8) + '" text-anchor="' + anchor + '">' +
                esc(bucketLabel(points[index].bucket, resolution)) + '</text>'
            );
        });

        series.forEach(function (line) {
            let path = '';
            points.forEach(function (point, index) {
                const value = Number(point[line.key]) || 0;
                path += (index ? ' L' : 'M') + x(index).toFixed(1) + ' ' +
                    y(value).toFixed(1);
            });
            parts.push(
                '<path class="mon-line" d="' + path + '" stroke="' +
                line.color + '"/>'
            );
            points.forEach(function (point, index) {
                const value = Number(point[line.key]) || 0;
                const label = line.label + ': ' + formatValue(value) + ' (' +
                    bucketLabel(point.bucket, resolution) + ')';
                parts.push(
                    '<circle class="mon-point" cx="' + x(index).toFixed(1) +
                    '" cy="' + y(value).toFixed(1) + '" r="2.2" fill="' +
                    line.color + '"><title>' + esc(label) + '</title></circle>'
                );
            });
        });

        parts.push('</svg>');
        container.innerHTML = parts.join('');
    }

    function renderExternalTable(tableId, services) {
        const table = el(tableId);
        if (!services || !services.length) {
            table.innerHTML = '<tbody><tr><td class="mon-empty">' +
                'Нет данных о внешних вызовах</td></tr></tbody>';
            return;
        }
        const head = ['Сервис', 'Вызовов', 'Ошибок', 'Расшифровка', 'Среднее', 'p95', 'Макс'];
        const html = ['<thead><tr>'];
        head.forEach(function (title, index) {
            html.push('<th class="' + (index ? 'num' : '') + '">' +
                title + '</th>');
        });
        html.push('</tr></thead><tbody>');
        services.forEach(function (row) {
            const label = row.label || row.service;
            const details = [];
            (row.error_outcomes || []).forEach(function (outcome) {
                details.push(outcome.label + ' — ' + fmt(outcome.errors));
            });
            if (row.top_error_endpoint && row.top_error_endpoint.endpoint) {
                details.push(
                    row.top_error_endpoint.method + ' ' +
                    row.top_error_endpoint.endpoint
                );
            }
            html.push('<tr>');
            html.push('<td title="' + esc(row.service) + '">' + esc(label) + '</td>');
            html.push('<td class="num">' + fmt(row.count) + '</td>');
            html.push(
                '<td class="num">' + fmt(row.errors) +
                ' (' + fmtPct(row.error_rate) + ')</td>'
            );
            html.push('<td>' + esc(details.join('; ') || '—') + '</td>');
            html.push('<td class="num">' + fmt(row.avg_ms) + ' мс</td>');
            html.push('<td class="num">' + fmt(row.p95_ms) + ' мс</td>');
            html.push('<td class="num">' + fmt(row.max_ms) + ' мс</td>');
            html.push('</tr>');
        });
        html.push('</tbody>');
        table.innerHTML = html.join('');
    }

    function renderTable(tableId, rows, emptyText) {
        const table = el(tableId);
        if (!rows || !rows.length) {
            table.innerHTML = '<tbody><tr><td class="mon-empty">' +
                esc(emptyText) + '</td></tr></tbody>';
            return;
        }
        const head = ['Endpoint', 'Запросов', 'p95', 'Среднее', 'Ошибки', 'Медленных'];
        const html = ['<thead><tr>'];
        head.forEach(function (title, index) {
            html.push('<th class="' + (index ? 'num' : '') + '">' +
                title + '</th>');
        });
        html.push('</tr></thead><tbody>');
        rows.forEach(function (row) {
            html.push('<tr>');
            html.push('<td title="' + esc(row.group) + '">' + esc(row.group) + '</td>');
            html.push('<td class="num">' + fmt(row.count) + '</td>');
            html.push('<td class="num">' + fmt(row.p95_ms) + ' мс</td>');
            html.push('<td class="num">' + fmt(row.avg_ms) + ' мс</td>');
            html.push(
                '<td class="num">' + fmt(row.errors) +
                (row.count ? ' (' + fmtPct(row.error_rate) + ')' : '') +
                '</td>'
            );
            html.push('<td class="num">' + fmt(row.slow_count) + '</td>');
            html.push('</tr>');
        });
        html.push('</tbody>');
        table.innerHTML = html.join('');
    }

    function metricValue(system, name) {
        const items = (system && system.metrics) || [];
        for (let index = 0; index < items.length; index += 1) {
            if (items[index].metric === name) {
                return items[index];
            }
        }
        return null;
    }

    function renderOps(data) {
        const cards = [];
        const system = data.system || {};
        const queue = data.queue || {};

        const cpu = metricValue(system, 'cpu_percent');
        const mem = metricValue(system, 'mem_percent');
        const load = metricValue(system, 'load1');
        const rss = metricValue(system, 'web_rss_mb');
        const active = metricValue(system, 'db_active_connections');
        const idleTx = metricValue(system, 'db_idle_in_tx');
        const maxConn = metricValue(system, 'db_max_connections');
        const dbSize = metricValue(system, 'db_size_mb');
        const commits = metricValue(system, 'db_xact_commit');
        const rollbacks = metricValue(system, 'db_xact_rollback');
        const temp = metricValue(system, 'db_temp_bytes');

        if (cpu) {
            cards.push(card(
                'CPU', fmt(cpu.value) + ' %',
                'макс ' + fmt(cpu.max) + ' %',
                cpu.max >= 90 ? 'bad' : ''
            ));
        }
        if (mem) {
            cards.push(card(
                'RAM', fmt(mem.value) + ' %',
                'макс ' + fmt(mem.max) + ' %',
                mem.max >= 90 ? 'bad' : ''
            ));
        }
        if (load) {
            cards.push(card(
                'Load average (1м)', String(load.value), 'макс ' + load.max
            ));
        }
        if (rss) {
            cards.push(card('RSS web-процесса', fmt(rss.value) + ' МБ', ''));
        }
        if (active) {
            cards.push(card(
                'PG активных', fmt(active.value),
                'idle in tx: ' + fmt(idleTx ? idleTx.value : 0) +
                ' / max ' + fmt(maxConn ? maxConn.value : 0)
            ));
        }
        if (dbSize) {
            cards.push(card('Размер БД', fmt(dbSize.value) + ' МБ', ''));
        }
        if (commits) {
            cards.push(card(
                'Транзакций/мин', fmt(commits.value),
                'откатов: ' + fmt(rollbacks ? rollbacks.value : 0)
            ));
        }
        if (temp) {
            cards.push(card('Temp, МБ/мин', String(temp.value), ''));
        }

        if (queue.available) {
            cards.push(card(
                'Очередь: готовы', fmt(queue.ready),
                'выполняются: ' + fmt(queue.running),
                queue.ready > 20 ? 'warn' : ''
            ));
            cards.push(card(
                'Старейшая задача',
                fmt(Math.round((queue.oldest_ready_age_seconds || 0) / 60)) + ' мин',
                'порог 10 мин',
                (queue.oldest_ready_age_seconds || 0) > 600 ? 'bad' : ''
            ));
            cards.push(card(
                'Задачи за час', fmt(queue.finished_hour),
                'ошибок: ' + fmt(queue.failed_hour),
                (queue.failed_hour || 0) > 0 ? 'warn' : ''
            ));
            cards.push(card(
                'p95 задачи', fmt(queue.p95_duration_ms) + ' мс',
                'макс ' + fmt(queue.max_duration_ms) + ' мс'
            ));
        }

        el('monOpsCards').innerHTML = cards.join('');

        const resources = data.system_series || {};
        const block = el('monResourcesBlock');
        if (resources.available) {
            block.classList.remove('hidden');
            renderChart(
                el('monResourcesChart'), resources.points,
                [
                    {key: 'cpu_percent', label: 'CPU', color: '#BBCF32'},
                    {key: 'mem_percent', label: 'RAM', color: '#8fb3ff'},
                ],
                resources.resolution,
                function (value) { return fmt(value) + ' %'; },
                100
            );
        } else {
            block.classList.add('hidden');
        }
    }

    function render(data) {
        const errorBox = el('monError');
        errorBox.classList.add('hidden');

        const summary = data.summary || {};
        const thresholds = data.thresholds || {};
        const clientCount = (summary.user || 0) + (summary.bot || 0);

        const cards = [];
        if (data.source && data.source !== 'db') {
            cards.push(card(
                'Источник', 'live',
                'таблица метрик недоступна — только счётчики процесса',
                'warn'
            ));
        }
        cards.push(card(
            'Запросов за период', fmt(clientCount),
            'пользователи + боты'
        ));
        cards.push(card(
            'Служебных', fmt(summary.monitoring),
            'health, статус, прогресс НК'
        ));
        cards.push(card(
            'Пик, запросов/мин', fmt(summary.peak_per_minute),
            'сейчас: ' + fmt(summary.requests_per_minute) + '/мин'
        ));
        cards.push(card(
            'p95 ответа', fmt(summary.p95_ms) + ' мс',
            'среднее: ' + fmt(summary.avg_ms) + ' мс',
            (summary.p95_ms || 0) > (thresholds.p95_ms || 0) ? 'bad' : 'good'
        ));
        cards.push(card(
            'Ошибки 5xx', fmtPct(summary.error_rate),
            'всего: ' + fmt(summary.errors),
            (summary.error_rate || 0) > (thresholds.error_rate || 0) ? 'bad' : 'good'
        ));
        cards.push(card(
            'Медленные (≥ 1 c)', fmt(summary.slow_count),
            'запросов дольше секунды'
        ));
        cards.push(card(
            'Время в БД', fmt(summary.avg_db_ms) + ' мс',
            'запросов к БД: ' + (Number(summary.avg_db_queries) || 0).toFixed(2)
        ));

        const external = data.external || {};
        if (external.total) {
            const externalErrors = external.errors || 0;
            cards.push(card(
                'Внешние API', fmt(external.total),
                'ошибок: ' + fmt(externalErrors) + ' (' +
                fmtPct(external.total ? externalErrors / external.total : 0) + ')'
            ));
        }
        el('monCards').innerHTML = cards.join('');

        renderChart(
            el('monRequestsChart'), data.series || [],
            [
                {key: 'user', label: 'Пользователи', color: '#BBCF32'},
                {key: 'bot', label: 'Боты', color: '#FABB1C'},
                {key: 'monitoring', label: 'Служебные', color: 'rgba(255,255,255,0.55)'},
            ],
            data.resolution,
            fmt
        );

        renderChart(
            el('monLatencyChart'), data.series || [],
            [{key: 'p95_ms', label: 'p95', color: '#8fb3ff'}],
            data.resolution,
            function (value) { return fmt(value) + ' мс'; }
        );

        renderTable(
            'monTopCount', data.top_groups,
            'Нет данных за период'
        );
        renderTable(
            'monTopSlow', data.top_slow,
            'Недостаточно запросов для p95'
        );

        renderExternalTable('monExternal', (data.external || {}).services);

        renderOps(data);

        const info = el('monSeriesInfo');
        if (info) {
            info.textContent = (data.range_label || '') +
                (data.source === 'db' ? '' : ' · live-данные процесса');
        }
    }

    async function load() {
        const errorBox = el('monError');
        try {
            const response = await fetch(
                '/monitoring/api/metrics/?range=' + encodeURIComponent(state.range),
                {headers: {'X-Requested-With': 'XMLHttpRequest'}}
            );
            const data = await response.json();
            if (!response.ok || data.is_error) {
                throw new Error(data.message || ('HTTP ' + response.status));
            }
            render(data);
        } catch (error) {
            errorBox.textContent = 'Не удалось загрузить метрики: ' + error.message;
            errorBox.classList.remove('hidden');
        }
    }

    function setRange(range) {
        state.range = range;
        document.querySelectorAll('#monRanges button').forEach(function (button) {
            button.classList.toggle('active', button.dataset.range === range);
        });
        load();
    }

    document.addEventListener('DOMContentLoaded', function () {
        const ranges = el('monRanges');
        if (!ranges) {
            return;
        }
        ranges.addEventListener('click', function (event) {
            const button = event.target.closest('button[data-range]');
            if (button) {
                setRange(button.dataset.range);
            }
        });

        el('monRefresh').addEventListener('click', load);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) {
                load();
            }
        });

        load();
        state.timer = setInterval(load, REFRESH_MS);
    });
})();
