# app_cz/management/commands/sync_completed_party_codes.py

"""
Досинхронизация кодов завершённых заданий.

Исправляет задания, которые были помечены «Синхронизировано» без кодов,
когда синхронизация кодов выполнялась только для «В работе»/«Закрыто».
По умолчанию берёт завершённые задания за последние 30 дней.

Примеры:
    python manage.py sync_completed_party_codes
    python manage.py sync_completed_party_codes --days 90 --limit 500
    python manage.py sync_completed_party_codes --dry-run
"""

from django.core.management.base import BaseCommand

from app_cz.services.code_sync import (
    COMPLETED_BACKFILL_DAYS,
    backfill_completed_party_codes,
)


class Command(BaseCommand):
    help = 'Досинхронизирует коды завершённых заданий, у которых их нет'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=COMPLETED_BACKFILL_DAYS,
            help=(
                'Глубина истории по дате создания задания '
                f'(по умолчанию {COMPLETED_BACKFILL_DAYS})'
            ),
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Максимум заданий за запуск (0 — без ограничения)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только подсчёт кандидатов без запросов к заводским серверам',
        )

    def handle(self, *args, **options):
        result = backfill_completed_party_codes(
            days=options['days'],
            limit=options['limit'],
            dry_run=options['dry_run'],
        )

        if result['dry_run']:
            self.stdout.write(self.style.WARNING(result['message']))
            return

        if result['is_error']:
            self.stderr.write(self.style.ERROR(result['message']))
            for error in result['error_details'][:20]:
                self.stderr.write(
                    f'  {error["party"]} ({error["factory"]}): '
                    f'{error["message"]}'
                )
        else:
            self.stdout.write(self.style.SUCCESS(result['message']))
