# app_cz/services/reserve_monitor.py

"""
Мониторинг резерва УИП в «Честном Знаке».

- Подсчёт зарезервированных УИП относительно лимита (по умолчанию 10000, задан ЧЗ).
- Email-уведомления при превышении порогов заполнения:
  > 50% — предупреждение, > 80% — тревога, > 90% — тревога + снятие с резерва устаревших УИП.
- Снятие с резерва устаревших УИП (до сгорания которых осталось немного времени)
  через отчёт о нанесении:
  * используются коды DataMatrix из заданий УИП, не находящиеся в статусе «нанесён»
    (по умолчанию это все коды);
  * после успешного отчёта коды помечаются как нанесённые;
  * если у УИП нет заданий — у внешнего сервиса «Молвест.Маркировка» запрашивается
    код по GTIN и УИП привязывается к нему в отчёте о нанесении.
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
from app_uip.models import UIP, PartyStatusChoices

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


def _release_uip(uip: UIP) -> dict:
    """
    Снимает один УИП с резерва через отчёт о нанесении.

    Коды для отчёта:
    1. DataMatrix из заданий УИП, не в статусе «нанесён» (по умолчанию — все);
    2. если заданий/кодов нет — один код, запрошенный у внешнего сервиса по GTIN.

    После успешного отчёта локальные коды помечаются как нанесённые,
    УИП переводится в статус REGISTERED.
    """
    # 1. Коды DataMatrix из заданий УИП (не нанесённые).
    codes = list(
        CISCode.objects.filter(
            production_party__uip=uip,
        )
        .exclude(production_status=ProductionCodeStatusChoices.APPLIED)
        .values_list('code', flat=True)[:1000]
    )

    external_code = None
    if not codes:
        # Нет заданий — запрашиваем код у внешнего сервиса по GTIN.
        external_code = _fetch_code_by_gtin(uip)
        if external_code:
            codes = [external_code]

    if not codes:
        return {
            'released': False,
            'number': uip.number,
            'reason': (
                'Нет кодов для отчёта о нанесении '
                '(нет заданий и не получен код по GTIN)'
            ),
        }

    # 2. Отправка отчёта о нанесении.
    try:
        result = send_application_report(
            sntins=codes,
            batch_number=uip.number,
        )
    except Exception as e:
        logger.exception(f'УИП {uip.number}: ошибка отправки отчёта о нанесении')
        return {
            'released': False,
            'number': uip.number,
            'reason': f'Ошибка отправки отчёта: {str(e)}',
        }

    if result.get('has_error'):
        return {
            'released': False,
            'number': uip.number,
            'reason': result.get('message', 'Ошибка отчёта о нанесении'),
        }

    # 3. Успех: помечаем локальные коды как нанесённые, статус УИП → REGISTERED.
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
            source='auto',
            note='Снят с резерва автоматически (переполнение резерва УИП)',
        )

    return {
        'released': True,
        'number': uip.number,
        'codes_count': len(codes),
        'external_code': bool(external_code),
    }


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
