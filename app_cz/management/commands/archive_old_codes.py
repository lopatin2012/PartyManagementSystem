# app_cz/management/commands/archive_old_codes.py


"""
Архивация устаревших кодов маркировки.

Переносит коды старше N дней (по умолчанию 45) из рабочей таблицы
в архивную базу ('archive'). Историчность сохраняется — безвозвратное
удаление не выполняется, чистка архива производится вручную.

Примеры:
    python manage.py archive_old_codes
    python manage.py archive_old_codes --days 60 --batch-size 10000
    python manage.py archive_old_codes --dry-run
    python manage.py archive_old_codes --limit 50000
"""

from django.core.management.base import BaseCommand

from app_cz.services.code_archive import archive_old_codes


class Command(BaseCommand):
    help = 'Архивирует коды маркировки старше N дней в архивную базу'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=45,
            help='Возраст кода в днях (по умолчанию 45)',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=5000,
            help='Размер батча (по умолчанию 5000)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Максимум кодов за запуск (0 — без ограничения)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только подсчёт кандидатов без переноса и удаления',
        )

    def handle(self, *args, **options):
        result = archive_old_codes(
            days=options['days'],
            batch_size=options['batch_size'],
            limit=options['limit'],
            dry_run=options['dry_run'],
        )

        if result['dry_run']:
            self.stdout.write(self.style.WARNING(result['message']))
        elif result['is_error']:
            self.stderr.write(self.style.ERROR(result['message']))
        else:
            self.stdout.write(self.style.SUCCESS(result['message']))