# app_cz/services/reserve_monitor.py

"""
Мониторинг резерва УИП в «Честном Знаке».

- Подсчёт зарезервированных УИП относительно лимита (по умолчанию 10000, задан ЧЗ).
- Email-уведомления при превышении порогов заполнения:
  > 50% — предупреждение, > 80% — тревога, > 90% — тревога + снятие с резерва устаревших УИП.
- Снятие с резерва устаревших УИП (до сгорания которых осталось немного времени)
  через отчёт о нанесении:
  * сначала запрашивается один код из задания у внешнего сервиса «Молвест.Маркировка»
    (codes/api/get_any_code_for_task/<uuid>/), если у УИП есть производственная партия;
  * иначе используются коды DataMatrix из заданий УИП, не находящиеся в статусе «нанесён»
    (по умолчанию это все коды);
  * если кодов нет вовсе — у внешнего сервиса запрашивается код по GTIN;
  * после успешного отчёта коды помечаются как нанесённые;
  * отчёт о нанесении требует срок годности — он берётся из производственной партии УИП;
  * если у УИП нет производственной партии со сроком годности — УИП не регистрируется
    и пропускается.
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from app_cz.models import CISCode, CISCodesStatusChoices, ProductionCodeStatusChoices
from app_cz.services.code_client import send_application_report
from app_factory.models import ProductProductionLocation
from app_uip.models import UIP, ProductionParty, PartyStatusChoices

logger = logging.getLogger(__name__)

# === Пороги заполнения резерва (проценты от лимита). ===
WARN_PERCENT = 50
CRITICAL_PERCENT = 80
RELEASE_PERCENT = 90

# === Параметры «сгорания» УИП. ===
# УИП сгорает через 30 дней после резервирования (установлено ЧЗ).
BURN_DAYS = 30
# Снимаем УИП, до сгорания которых осталось не более этого количества дней.
RELEASE_BEFORE_BURN_DAYS = 5
# Регистрируем УИП (отправляем отчёт о нанесении), до сгорания которых
# осталось менее этого количества дней.
REGISTER_BEFORE_BURN_DAYS = 3
# Предупреждаем по почте, если до сгорания осталось менее этого количества дней.
BURN_WARN_DAYS = 7
# Максимальное количество УИП за один запуск снятия с резерва.
RELEASE_BATCH_SIZE = 100

# Статусы, которые считаются «в резерве».
RESERVED_STATUSES = [
    PartyStatusChoices.RESERVED_CZ,
    PartyStatusChoices.RESERVED_LOCAL,
]


def get_reserve_stats() -> dict:
    """
    Статистика заполнения резерва УИП.

    :return: {'count': int, 'limit': int, 'percent': float}
    """
    limit = int(getattr(settings, 'UIP_RESERVE_LIMIT', 10000) or 10000)
    count = UIP.objects.filter(status__in=RESERVED_STATUSES).count()
    percent = round(count / limit * 100, 1) if limit else 0.0
    return {'count': count, 'limit': limit, 'percent': percent}


def _release_threshold_date():
    """Дата, раньше которой УИП считается «устаревшим» (скоро сгорит)."""
    return timezone.now().date() - timedelta(days=BURN_DAYS - RELEASE_BEFORE_BURN_DAYS)


def _register_threshold_date():
    """Дата резервирования, раньше которой УИП близок к сгоранию (<3 дней)."""
    return timezone.now().date() - timedelta(days=BURN_DAYS - REGISTER_BEFORE_BURN_DAYS)


def _burn_warn_threshold_date():
    """Дата резервирования, раньше которой УИП попадает в предупреждение о сгорании."""
    return timezone.now().date() - timedelta(days=BURN_DAYS - BURN_WARN_DAYS)


def _uip_effective_reservation_date(uip: UIP):
    """Дата резервирования УИП (с запасом на отсутствие даты)."""
    return uip.reservation_date or uip.created_at.date()


def _external_service_url_for_uip(uip: UIP) -> str:
    """
    Адрес сервера маркировки завода для УИП.

    У каждого завода свой локальный сервер (Factory.ip_address/port_address).
    Для УИП без производственных партий завод определяется через место
    производства SKU (ProductProductionLocation).
    """
    if not uip.product_sku:
        return None
    location = (
        ProductProductionLocation.objects.filter(
            product_sku=uip.product_sku,
            is_active=True,
            line__is_active=True,
            line__workshop__factory__ip_address__isnull=False,
            line__workshop__factory__port_address__isnull=False,
        )
        .select_related('line__workshop__factory')
        .first()
    )
    if not location or not location.line or not location.line.workshop:
        return None
    factory = location.line.workshop.factory
    if not factory or not factory.ip_address or not factory.port_address:
        return None
    return f'http://{factory.ip_address}:{factory.port_address}'


def _fetch_code_by_gtin(uip: UIP) -> str:
    """
    Запрашивает код DataMatrix у внешнего сервиса «Молвест.Маркировка» по GTIN УИП.

    Ожидаемый эндпоинт внешнего сервиса:
        GET {url}/codes/api/get_code_by_gtin/?gtin={gtin}
    Ответ: {'code': '...'} / {'codes': [...]} / список кодов.
    """
    gtin = uip.gtin
    if not gtin:
        return None

    url = _external_service_url_for_uip(uip)
    if not url:
        logger.warning(
            f'УИП {uip.number}: не найден адрес сервера маркировки завода '
            f'для запроса кода по GTIN {gtin}.'
        )
        return None

    api_url = f"{url.rstrip('/')}/codes/api/get_code_by_gtin/"
    try:
        logger.info(f'Запрос кода по GTIN {gtin} у внешнего сервиса ({url})')
        response = requests.get(
            api_url,
            params={'gtin': gtin},
            headers={'Accept': 'application/json'},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f'УИП {uip.number}: ошибка запроса кода по GTIN {gtin}: {e}')
        return None

    if isinstance(data, dict):
        code = data.get('code')
        if not code:
            codes = data.get('codes') or []
            code = codes[0] if codes else None
    elif isinstance(data, list):
        code = data[0] if data else None
    else:
        code = None

    if isinstance(code, dict):
        code = code.get('code')

    code = str(code).strip() if code else ''
    return code or None


def _fetch_code_for_task(uip: UIP, task_uuid: str) -> str:
    """
    Запрашивает один код DataMatrix у внешнего сервиса «Молвест.Маркировка»
    из задания по его uuid:
        GET {url}/codes/api/get_any_code_for_task/{task_uuid}/
    Ответ: {'detail': 'Код найден', 'code': '...', 'isError': False}
           / {'detail': '...', 'isError': True}
    """
    if not task_uuid:
        return None

    url = _external_service_url_for_uip(uip)
    if not url:
        logger.warning(
            f'УИП {uip.number}: не найден адрес сервера маркировки завода '
            f'для запроса кода из задания {task_uuid}.'
        )
        return None

    api_url = f"{url.rstrip('/')}/codes/api/get_any_code_for_task/{task_uuid}/"
    try:
        logger.info(f'Запрос кода из задания {task_uuid} у внешнего сервиса ({url})')
        response = requests.get(
            api_url,
            headers={'Accept': 'application/json'},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f'УИП {uip.number}: ошибка запроса кода из задания {task_uuid}: {e}')
        return None

    if not isinstance(data, dict):
        return None

    # Внешний сервис помечает возвращённый код как «нанесён» (status_application=True).
    if data.get('isError') or data.get('is_error'):
        return None

    code = data.get('code')
    if isinstance(code, dict):
        code = code.get('code')
    code = str(code).strip() if code else ''
    return code or None


def _get_expiration_date(uip: UIP):
    """
    Дата срока годности из производственной партии УИП (для отчёта о нанесении).

    Берётся из самой свежей производственной партии УИП, у которой указан срок
    годности. Если такой партии нет — возвращается None (УИП не регистрируем).
    """
    party = (
        ProductionParty.objects
        .filter(uip=uip, expiration_datetime__isnull=False)
        .order_by('-production_datetime_start', '-created_at')
        .first()
    )
    if not party or not party.expiration_datetime:
        return None
    return party.expiration_datetime.date().isoformat()


def register_uip(uip: UIP, source: str = 'auto', note: str = None) -> dict:
    """
    Регистрирует УИП через отчёт о нанесении (УИП → REGISTERED).

    Отчёт о нанесении требует срок годности — он берётся из производственной
    партии УИП. Если у УИП нет производственной партии со сроком годности,
    УИП НЕ регистрируется (пропускается).

    Код для отчёта:
    1. один код из задания внешнего сервиса (get_any_code_for_task);
    2. если задания/кода нет — DataMatrix из заданий УИП, не в статусе «нанесён»;
    3. если кодов нет вовсе — код по GTIN у внешнего сервиса.

    После успешного отчёта локальные коды помечаются как нанесённые,
    УИП переводится в статус REGISTERED.
    """
    # 1. Срок годности из производственной партии.
    exp_date = _get_expiration_date(uip)
    if exp_date is None:
        return {
            'registered': False,
            'number': uip.number,
            'reason': 'Нет производственной партии со сроком годности — УИП не регистрируется',
        }

    # 2. Коды для отчёта.
    codes = []

    # 2a. Пробуем получить один код из задания внешнего сервиса.
    external_code = None
    party = (
        ProductionParty.objects
        .filter(uip=uip, external_number_task__isnull=False)
        .exclude(external_number_task='')
        .order_by('-production_datetime_start', '-created_at')
        .first()
    )
    if party and party.external_number_task:
        external_code = _fetch_code_for_task(uip, party.external_number_task)

    if external_code:
        codes = [external_code]
    else:
        # 2b. Коды DataMatrix из локальных заданий УИП (не нанесённые).
        codes = list(
            CISCode.objects.filter(
                production_party__uip=uip,
            )
            .exclude(production_status=ProductionCodeStatusChoices.APPLIED)
            .values_list('code', flat=True)[:1000]
        )

    # 2c. Если кодов нет вовсе — код по GTIN.
    if not codes:
        external_code = _fetch_code_by_gtin(uip)
        if external_code:
            codes = [external_code]

    if not codes:
        return {
            'registered': False,
            'number': uip.number,
            'reason': (
                'Нет кодов для отчёта о нанесении '
                '(нет задания и не получен код по GTIN)'
            ),
        }

    # 3. Отправка отчёта о нанесении.
    try:
        result = send_application_report(
            sntins=codes,
            batch_number=uip.number,
            exp_date=exp_date,
        )
    except Exception as e:
        logger.exception(f'УИП {uip.number}: ошибка отправки отчёта о нанесении')
        return {
            'registered': False,
            'number': uip.number,
            'reason': f'Ошибка отправки отчёта: {str(e)}',
        }

    if result.get('has_error'):
        return {
            'registered': False,
            'number': uip.number,
            'reason': result.get('message', 'Ошибка отчёта о нанесении'),
        }

    # 4. Успех: помечаем локальные коды как нанесённые, статус УИП → REGISTERED.
    with transaction.atomic():
        CISCode.objects.filter(
            production_party__uip=uip,
            code__in=codes,
        ).exclude(
            production_status=ProductionCodeStatusChoices.APPLIED,
        ).update(
            production_status=ProductionCodeStatusChoices.APPLIED,
            cz_status=CISCodesStatusChoices.APPLIED,
        )
        uip.change_status(
            PartyStatusChoices.REGISTERED,
            source=source,
            note=note or 'УИП зарегистрирован (отчёт о нанесении отправлен)',
        )

    return {
        'registered': True,
        'number': uip.number,
        'codes_count': len(codes),
        'external_code': bool(external_code),
    }


def _release_uip(uip: UIP) -> dict:
    """
    Снимает один УИП с резерва через отчёт о нанесении (регистрация УИП).

    Требует срок годности из производственной партии УИП. Если у УИП нет
    производственной партии со сроком годности — УИП не регистрируется
    (пропускается).

    Коды для отчёта:
    1. один код из задания внешнего сервиса (get_any_code_for_task);
    2. иначе DataMatrix из заданий УИП, не в статусе «нанесён»;
    3. иначе код по GTIN у внешнего сервиса.

    После успешного отчёта локальные коды помечаются как нанесённые,
    УИП переводится в статус REGISTERED.
    """
    res = register_uip(
        uip,
        source='auto',
        note='Снят с резерва автоматически (переполнение резерва УИП)',
    )

    return {
        'released': res['registered'],
        'number': res['number'],
        'reason': res.get('reason', ''),
        'codes_count': res.get('codes_count', 0),
        'external_code': res.get('external_code', False)
    }


def register_eligible_reserved_uips(max_uips: int = RELEASE_BATCH_SIZE) -> dict:
    """
    Регистрирует зарезервированные УИП, близкие к сгоранию (до сгорания осталось
    менее REGISTER_BEFORE_BURN_DAYS дней), у которых есть производственная партия
    со сроком годности (т.е. есть всё необходимое для отчёта о нанесении).

    Отчёт о нанесении отправляется только когда УИП близок к сгоранию.
    УИП без производственной партии со сроком годности пропускаются.

    :param max_uips: Максимальное количество УИП за один запуск.
    :return: Сводка по регистрации.
    """
    threshold = _register_threshold_date()

    uips = (
        UIP.objects.filter(status__in=RESERVED_STATUSES)
        .filter(
            Q(reservation_date__lte=threshold)
            | Q(reservation_date__isnull=True, created_at__date__lte=threshold)
        )
        .filter(production_parties__expiration_datetime__isnull=False)
        .distinct()
        .order_by('reservation_date', 'created_at')[:max_uips]
    )

    summary = {
        'registered': 0,
        'failed': 0,
        'skipped': 0,
        'total': 0,
        'registered_uips': [],
        'errors': [],
    }

    for uip in uips:
        summary['total'] += 1
        res = register_uip(uip, source='service', note='УИП зарегистрирован (отчёт о нанесении)')
        if res['registered']:
            summary['registered'] += 1
            summary['registered_uips'].append(res['number'])
        else:
            summary['failed'] += 1
            summary['errors'].append(f"{res['number']}: {res.get('reason', 'ошибка')}")

    summary['message'] = (
        f'Зарегистрировано УИП: {summary["registered"]}, '
        f'не удалось: {summary["failed"]} (из {summary["total"]})'
    )
    logger.info(summary['message'])
    return summary


def release_obsolete_reserved_uips(max_uips: int = RELEASE_BATCH_SIZE) -> dict:
    """
    Снимает с резерва устаревшие УИП — зарезервированные, до сгорания которых
    осталось не более RELEASE_BEFORE_BURN_DAYS дней. Самые старые — первыми.

    :param max_uips: Максимальное количество УИП за один запуск.
    :return: Сводка по снятию.
    """
    threshold = _release_threshold_date()

    uips = (
        UIP.objects.filter(status__in=RESERVED_STATUSES)
        .filter(
            Q(reservation_date__lte=threshold)
            | Q(reservation_date__isnull=True, created_at__date__lte=threshold)
        )
        .order_by('reservation_date', 'created_at')[:max_uips]
    )

    summary = {
        'released': 0,
        'failed': 0,
        'total': 0,
        'released_uips': [],
        'errors': [],
    }

    for uip in uips:
        summary['total'] += 1
        res = _release_uip(uip)
        if res['released']:
            summary['released'] += 1
            summary['released_uips'].append(res['number'])
        else:
            summary['failed'] += 1
            summary['errors'].append(f"{res['number']}: {res['reason']}")

    summary['message'] = (
        f'Снято с резерва: {summary["released"]}, '
        f'не удалось: {summary["failed"]} (из {summary["total"]})'
    )
    logger.info(summary['message'])
    return summary


def _build_email_body(stats: dict, release: dict = None) -> str:
    """Текст письма о состоянии резерва УИП."""
    lines = [
        f'Количество зарезервированных УИП: {stats["count"]}',
        f'Лимит резерва (Честный Знак): {stats["limit"]}',
        f'Заполнение резерва: {stats["percent"]}%',
        '',
        f'Пороги: >{WARN_PERCENT}% — предупреждение, '
        f'>{CRITICAL_PERCENT}% — тревога, '
        f'>{RELEASE_PERCENT}% — снятие с резерва устаревших УИП.',
    ]

    if release:
        lines += [
            '',
            'Снятие с резерва устаревших УИП:',
            f'  Снято: {release["released"]}',
            f'  Не удалось: {release["failed"]}',
        ]
        if release.get('released_uips'):
            shown = release['released_uips'][:50]
            suffix = '…' if len(release['released_uips']) > 50 else ''
            lines.append('  Снятые УИП: ' + ', '.join(shown) + suffix)
        if release.get('errors'):
            lines.append('  Ошибки:')
            lines += [f'    {err}' for err in release['errors'][:20]]

    return '\n'.join(lines)


def _notify(subject: str, body: str, recipients: list) -> dict:
    """
    Отправляет письмо через фоновую задачу (очередь 'emails').
    """
    if not recipients:
        logger.warning(
            f'Не настроены получатели уведомлений '
            f'(UIP_RESERVE_NOTIFICATION_EMAILS). Письмо не отправлено: {subject}'
        )
        return {'sent': False, 'reason': 'Нет получателей'}

    from app_scheduler.tasks import send_email_task

    try:
        result = send_email_task.enqueue(
            subject=subject,
            message=body,
            recipient_list=list(recipients),
        )
        return {'sent': True, 'task_id': str(result.id) if result else None}
    except Exception as e:
        logger.error(f'Ошибка постановки email-задачи «{subject}»: {e}')
        return {'sent': False, 'reason': str(e)}


def check_uip_reserve_and_notify() -> dict:
    """
    Проверяет заполнение резерва УИП и при необходимости:
    - отправляет email-уведомление (>50% — предупреждение, >80% — тревога);
    - снимает с резерва устаревшие УИП (>90%) и сообщает о результате.

    :return: Сводка проверки.
    """
    stats = get_reserve_stats()
    percent = stats['percent']

    result = {
        'is_error': False,
        'count': stats['count'],
        'limit': stats['limit'],
        'percent': percent,
        'level': 'ok',
        'message': '',
        'release': None,
    }

    recipients = getattr(settings, 'UIP_RESERVE_NOTIFICATION_EMAILS', []) or []

    if percent > RELEASE_PERCENT:
        result['level'] = 'release'
        release = release_obsolete_reserved_uips()
        result['release'] = release
        if release.get('errors'):
            result['is_error'] = True

        subject = (
            f'КРИТИЧНО: резерв УИП заполнен на {percent}%. '
            f'Выполнено снятие с резерва: {release["released"]}.'
        )
        _notify(subject, _build_email_body(stats, release), recipients)
        result['message'] = (
            f'Резерв УИП: {stats["count"]}/{stats["limit"]} ({percent}%). '
            f'Снято с резерва: {release["released"]}, ошибок: {release["failed"]}.'
        )

    elif percent > CRITICAL_PERCENT:
        result['level'] = 'critical'
        subject = f'ТРЕВОГА: резерв УИП заполнен на {percent}%'
        _notify(subject, _build_email_body(stats), recipients)
        result['message'] = (
            f'Резерв УИП: {stats["count"]}/{stats["limit"]} ({percent}%). Тревога: '
            f'порог {CRITICAL_PERCENT}% превышен.'
        )

    elif percent > WARN_PERCENT:
        result['level'] = 'warning'
        subject = f'Предупреждение: резерв УИП заполнен на {percent}%'
        _notify(subject, _build_email_body(stats), recipients)
        result['message'] = (
            f'Резерв УИП: {stats["count"]}/{stats["limit"]} ({percent}%). '
            f'Порог {WARN_PERCENT}% превышен.'
        )

    else:
        result['message'] = (
            f'Резерв УИП в норме: {stats["count"]}/{stats["limit"]} ({percent}%).'
        )

    logger.info(result['message'])
    return result


def check_uip_burn_and_notify() -> dict:
    """
    Проверка «сгорания» УИП (раз в сутки).

    УИП в резерве сгорает через BURN_DAYS (30) дней после резервирования.
    Если до сгорания осталось менее BURN_WARN_DAYS (7) дней — отправляется
    email-предупреждение со списком таких УИП и оставшимися днями.

    :return: Сводка проверки.
    """
    threshold = _burn_warn_threshold_date()

    at_risk = list(
        UIP.objects.filter(status__in=RESERVED_STATUSES)
        .filter(
            Q(reservation_date__lte=threshold)
            | Q(reservation_date__isnull=True, created_at__date__lte=threshold)
        )
        .select_related('product_sku__product')
        .order_by('reservation_date', 'created_at')
    )

    today = timezone.now().date()
    records = []
    for uip in at_risk:
        reserve_date = _uip_effective_reservation_date(uip)
        burn_date = reserve_date + timedelta(days=BURN_DAYS)
        days_left = (burn_date - today).days
        records.append({
            'number': uip.number,
            'status': uip.get_status_display(),
            'product': str(uip.product_sku.product) if uip.product_sku_id else '-',
            'reservation_date': reserve_date,
            'burn_date': burn_date,
            'days_left': days_left,
        })

    result = {
        'is_error': False,
        'at_risk_count': len(records),
        'burn_warn_days': BURN_WARN_DAYS,
        'message': '',
    }

    if not records:
        result['message'] = (
            f'Сгорание УИП: нет УИП, до сгорания которых осталось '
            f'менее {BURN_WARN_DAYS} дней.'
        )
        logger.info(result['message'])
        return result

    subject = (
        f'ВНИМАНИЕ: до сгорания {len(records)} УИП осталось '
        f'менее {BURN_WARN_DAYS} дней'
    )
    body_lines = [
        f'До сгорания следующих УИП осталось менее {BURN_WARN_DAYS} дней:',
        f'УИП сгорает через {BURN_DAYS} дней после резервирования.',
        '',
        f'Всего УИП под угрозой: {len(records)}',
        '',
        '№  УИП  |  Продукт  |  Дата резервирования  |  Дата сгорания  |  Осталось дней',
    ]
    body_lines += [
        f'{i}. {r["number"]} | {r["product"]} | '
        f'{r["reservation_date"]:%d.%m.%Y} | {r["burn_date"]:%d.%m.%Y} | '
        f'{r["days_left"]}'
        for i, r in enumerate(records, start=1)
    ]

    recipients = getattr(settings, 'UIP_RESERVE_NOTIFICATION_EMAILS', []) or []
    sent = _notify(subject, '\n'.join(body_lines), recipients)

    result['is_error'] = not sent.get('sent')
    result['message'] = (
        f'Сгорание УИП: обнаружено УИП под угрозой: {len(records)}. '
        f'Письмо отправлено: {sent.get("sent", False)}.'
    )
    logger.info(result['message'])
    return result
