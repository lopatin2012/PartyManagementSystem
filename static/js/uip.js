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

// ==========================================
// ГЕНЕРАЦИЯ УИП
// ==========================================
let availableProducts = [];   // полный список
let filteredProducts = [];    // отфильтрованный список
let selectedSkuId = null;     // сохранённый выбор
let lastQuery = '';           // последний поисковый запрос (для подсветки)
const MAX_VISIBLE_PRODUCTS = 50;  // сколько элементов рендерим за раз

function initGenerateModal() {
    const dataEl = document.getElementById('available-products-data');
    if (dataEl) {
        try {
            availableProducts = JSON.parse(dataEl.textContent);
        } catch (e) {
            availableProducts = [];
        }
    }
    filteredProducts = availableProducts;
    renderProductList();

    const dateInput = document.getElementById('productionDate');
    if (dateInput) {
        dateInput.valueAsDate = new Date();
        dateInput.addEventListener('change', updatePreview);
    }
}

/** Фильтрация по артикулу и названию. */
function filterProducts(query) {
    lastQuery = query.trim();
    const q = lastQuery.toLowerCase();

    if (!q) {
        filteredProducts = availableProducts;
    } else {
        filteredProducts = availableProducts.filter(p =>
            p.article.toLowerCase().includes(q) ||
            p.name.toLowerCase().includes(q)
        );
    }
    renderProductList();
}

/** Рендерим только первые MAX_VISIBLE_PRODUCTS совпадений. */
function renderProductList() {
    const list = document.getElementById('productList');
    if (!list) return;

    updateProductCount();

    if (!filteredProducts.length) {
        list.innerHTML = '<div class="empty-products">Ничего не найдено</div>';
        return;
    }

    const visible = filteredProducts.slice(0, MAX_VISIBLE_PRODUCTS);

    // Сохраняем выбор, если выбранный продукт виден; иначе — первый.
    let checkedIndex = visible.findIndex(p => p.sku_id === selectedSkuId);
    if (checkedIndex === -1) checkedIndex = 0;

    list.innerHTML = visible.map((p, i) => `
        <label class="product-item">
            <input type="radio" name="product" value="${p.sku_id}"
                   data-gtin="${p.gtin}" data-article="${escapeHtml(p.article)}"
                   ${i === checkedIndex ? 'checked' : ''} onchange="onProductSelect(this)">
            <div class="product-info">
                <span class="product-name">${highlightMatch(p.name, lastQuery)}</span>
                <span class="product-meta">GTIN: ${p.gtin} · Арт: ${highlightMatch(p.article, lastQuery)}</span>
            </div>
        </label>
    `).join('');

    const checked = list.querySelector('input[type=radio]:checked');
    if (checked) {
        selectedSkuId = checked.value;
        updatePreview();
    }
}

/** Счётчик найденного. */
function updateProductCount() {
    const countEl = document.getElementById('productCount');
    if (!countEl) return;

    const total = availableProducts.length;
    const found = filteredProducts.length;

    if (!lastQuery) {
        countEl.textContent = `Всего продуктов: ${total}`;
    } else if (found > MAX_VISIBLE_PRODUCTS) {
        countEl.textContent = `Найдено: ${found} · показано первых ${MAX_VISIBLE_PRODUCTS}`;
    } else {
        countEl.textContent = `Найдено: ${found} из ${total}`;
    }
}

/** Безопасная подсветка совпадений. */
function highlightMatch(text, query) {
    if (!query) return escapeHtml(text);
    const idx = text.toLowerCase().indexOf(query.toLowerCase());
    if (idx === -1) return escapeHtml(text);

    const before = text.slice(0, idx);
    const match = text.slice(idx, idx + query.length);
    const after = text.slice(idx + query.length);
    return escapeHtml(before) + '<mark>' + escapeHtml(match) + '</mark>' + escapeHtml(after);
}

function onProductSelect(radio) {
    selectedSkuId = radio.value;
    updatePreview();
}

function buildLocalNumber(gtin, dateStr, article) {
    // dateStr: YYYY-MM-DD -> ГГММДД
    const [yyyy, mm, dd] = dateStr.split('-');
    const datePart = yyyy.slice(2) + mm + dd;
    const articlePart = (article || '').slice(0, 5).padEnd(5, '0');
    const base = gtin + datePart + articlePart;
    return base.padEnd(32, '0').slice(0, 32);
}

function updatePreview() {
    const previewNumber = document.getElementById('previewNumber');
    const previewHint = document.getElementById('previewHint');
    if (!previewNumber) return;

    const mode = document.querySelector('input[name="genMode"]:checked').value;

    if (mode === 'cz') {
        previewNumber.textContent = 'Будет получен от Честного Знака';
        previewHint.textContent = 'Формат ЧЗ: GTIN(14) + дата(6) + случайный серийный номер. ' +
            'Примеры: 080051252763252607244PW4fsYzRsV7, 08005125276325260722r43';
        return;
    }

    // Локальный режим — живой предпросмотр.
    const radio = document.querySelector('input[name="product"]:checked');
    const dateInput = document.getElementById('productionDate');

    if (!radio || !dateInput.value) {
        previewNumber.textContent = '—';
        previewHint.textContent = '';
        return;
    }

    const gtin = radio.dataset.gtin;
    const article = radio.dataset.article;
    const number = buildLocalNumber(gtin, dateInput.value, article);

    previewNumber.textContent = number;

    // Разбор номера по частям с подсветкой.
    const [yyyy, mm, dd] = dateInput.value.split('-');
    const datePart = yyyy.slice(2) + mm + dd;
    const articlePart = (article || '').slice(0, 5).padEnd(5, '0');
    const zeros = number.slice(25);

    previewHint.innerHTML =
        `<span class="part-gtin">${gtin}</span>` +
        `<span class="part-date">${datePart}</span>` +
        `<span class="part-article">${articlePart}</span>` +
        `<span class="part-zeros">${zeros}</span><br>` +
        `<small>GTIN(14) + дата ГГММДД(6) + артикул(5) + нули до 32</small>`;
}

function openGenerateModal() {
    document.getElementById('generateModal').style.display = 'flex';
    document.getElementById('generateStatus').style.display = 'none';

    // Сброс поиска при открытии (выбор продукта сохраняется через selectedSkuId).
    const searchInput = document.getElementById('productSearch');
    if (searchInput) searchInput.value = '';
    filterProducts('');

    updatePreview();
}

function closeGenerateModal() {
    document.getElementById('generateModal').style.display = 'none';
}

async function submitGenerate() {
    const btn = document.getElementById('generateSubmitBtn');
    const statusDiv = document.getElementById('generateStatus');
    const radio = document.querySelector('input[name="product"]:checked');
    const dateInput = document.getElementById('productionDate');
    const mode = document.querySelector('input[name="genMode"]:checked').value;

    if (!radio) {
        showGenerateStatus('Выберите продукт', true);
        return;
    }
    if (!dateInput.value) {
        showGenerateStatus('Укажите дату производства', true);
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Генерация...';
    statusDiv.style.display = 'none';

    try {
        const response = await fetch(btn.dataset.url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': btn.dataset.csrf,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                product_sku_id: radio.value,
                production_date: dateInput.value,
                mode: mode
            })
        });

        const result = await response.json();
        showGenerateStatus(result.message, result.is_error);

        if (!result.is_error) {
            setTimeout(() => window.location.reload(), 1800);
        }
    } catch (error) {
        showGenerateStatus('Ошибка соединения с сервером', true);
        console.error('Generate error:', error);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Сгенерировать';
    }
}

function showGenerateStatus(message, isError) {
    const statusDiv = document.getElementById('generateStatus');
    statusDiv.style.display = 'block';
    statusDiv.className = 'sync-status ' + (isError ? 'error' : 'success');
    statusDiv.textContent = message;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ==========================================
// ФИЛЬТРЫ ТАБЛИЦЫ УИП
// ==========================================

/** Закрыть все открытые панели фильтров. */
function closeAllFilterPanels() {
    document.querySelectorAll('.th-filter').forEach(p => {
        p.style.display = 'none';
        const parentTh = p.closest('th');
        if (parentTh) parentTh.classList.remove('open');
    });
}

/** Раскрыть/скрыть панель фильтра в заголовке колонки. */
function toggleFilter(col) {
    const panel = document.getElementById('thf-' + col);
    if (!panel) return;

    const th = panel.closest('th');
    const isVisible = panel.style.display !== 'none';

    closeAllFilterPanels();

    if (!isVisible) {
        // Убираем inline-скрытие — CSS сам задаст block/flex.
        panel.style.display = '';
        if (th) th.classList.add('open');
        const firstInput = panel.querySelector('input, select');
        if (firstInput) firstInput.focus();
    }
}

/** Применить фильтры (отправить форму). */
function applyFilters() {
    document.getElementById('uipFilterForm').submit();
}

/** Сбросить все фильтры — переход на чистый URL. */
function resetFilters() {
    window.location.href = window.location.pathname;
}

// Закрытие панелей фильтров по клику вне таблицы.
document.addEventListener('click', function (event) {
    if (!event.target.closest('.uip-table')) {
        closeAllFilterPanels();
    }
});
// Инициализация при загрузке страницы.
document.addEventListener('DOMContentLoaded', initGenerateModal);
