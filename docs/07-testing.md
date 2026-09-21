# 07. Тестирование

## Запуск

```bash
# Все тесты
python manage.py test

# По приложениям
python manage.py test app_uip
python manage.py test app_cz app_scheduler

# Отдельный класс/метод
python manage.py test app_uip.tests.ReserveUipsServiceTests
python manage.py test app_uip.tests.ReserveUipsServiceTests.test_generate_count_loops
```

Требуется доступный PostgreSQL — Django создаёт и удаляет временную тестовую БД. Внешние вызовы
(ЧЗ, HTTP) мокаются через `unittest.mock.patch` — сеть и учётная запись СУЗ не нужны.

## Где живут тесты

| Файл | Что покрывает |
| --- | --- |
| `app_uip/tests.py` | Ядро УИП, сериализаторы, резервирование, API статусов, роли/доступ, карточки поиска |
| `app_cz/tests.py` | Устойчивость синхронизации с внешним сервисом: персональная метка завода, поведение при сбоях |
| `app_scheduler/tests.py` | Логика планировщика (`_effective_interval`, быстрый повтор) |

Остальные приложения (`app_factory`, `app_page`, `app_helper`, `app_wms`) — пустые заглушки
`tests.py`.

## Что важно проверять

- **Резервирование УИП** (`app_uip/services/uip_reserve.py`): генерация, свои номера, лимиты,
  повторное резервирование сгоревших.
- **Синхронизация с внешним сервисом** (`app_cz/services/code_sync.py`): метка `changed_since`
  не сдвигается при сбое; изоляция заводов; ошибки обработки удерживают метку.
- **Роли/доступ** (`app_helper/access.py`): `is_admin`/`can_view_uip`/`can_generate_uip`,
  доступ к `/uip/`, генерация по праву записи.
- **Поиск**: карточки содержат внешний номер задания и место производства.

## Правило проекта

Любой баг и новая функциональность должны сопровождаться тестами:

- при исправлении бага — тест, воспроизводящий ошибку и подтверждающий фикс;
- при новой функциональности — happy path, ошибки и граничные случаи;
- при изменении поведения — обновить соответствующие тесты.

## Рендер-тесты и статика

В тестах `DEBUG` выключен, а `ManifestStaticFilesStorage` требует собранный манифест. Для тестов,
которые рендерят шаблоны, используется `override_settings(STORAGES=...)` с простым
`StaticFilesStorage` (см. `STATIC_OVERRIDE` в `app_uip/tests.py`).

## Проверки перед коммитом

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

> Линтеры/форматтеры/тайпчекеры в проекте не настроены (нет ruff/flake8/mypy).
