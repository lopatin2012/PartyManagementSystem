# app_uip/management/commands/restore_pg_failed_drafts.py

"""
Возврат в черновики УИП, ошибочно помеченных зарегистрированными из-за
отсутствия товарной группы (pg).

Старая логика резервирования черновиков считала любой отказ ЧЗ поводом
поставить «Зарегистрирован». Для ошибки `ReservationRequest["pg"]` это
неверно: без товарной группы резерв невозможен. Команда находит такие УИП
по истории статусов и возвращает их в черновики, чтобы после заполнения
товарной группы зарезервировать заново.

Примеры:
    python manage.py restore_pg_failed_drafts            # показать список
    python manage.py restore_pg_failed_drafts --apply    # вернуть в черновики
"""

from django.core.management.base import BaseCommand

from app_uip.services.reserve_accumulation import restore_pg_failed_drafts


class Command(BaseCommand):
    help = 'Возвращает в черновики УИП, ошибочно помеченные зарегистрированными из-за отсутствия товарной группы'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Применить изменения (по умолчанию только показать список)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Максимум УИП за запуск (0 — без ограничения)',
        )

    def handle(self, *args, **options):
        result = restore_pg_failed_drafts(
            dry_run=not options['apply'],
            limit=options['limit'],
        )

        for number in result.get('numbers', [])[:200]:
            self.stdout.write(f'  {number}')

        message = result['message']
        if result.get('is_error'):
            self.stderr.write(self.style.ERROR(message))
        elif result.get('dry_run'):
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.SUCCESS(message))
