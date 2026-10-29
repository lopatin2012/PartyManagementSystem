# app_uip/management/commands/reserve_required_drafts.py

"""
Резервирование в ЧЗ черновиков с датой производства от 01.11.2026.

По правилу с 01.11.2026 УИП не должен оставаться черновиком — он
резервируется в Честном Знаке. Команда находит такие черновики и
резервирует их (по товарным группам, пачками).

Примеры:
    python manage.py reserve_required_drafts --dry-run
    python manage.py reserve_required_drafts
    python manage.py reserve_required_drafts --limit 50
    python manage.py reserve_required_drafts --date 2026-11-01
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from app_uip.services.reserve_accumulation import reserve_drafts_from_date


class Command(BaseCommand):
    help = 'Резервирует в ЧЗ черновики УИП с датой производства от 01.11.2026'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только показать количество черновиков, без запросов в ЧЗ',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Максимум черновиков за запуск (0 — без ограничения)',
        )
        parser.add_argument(
            '--date',
            default='',
            help='Дата производства, с которой резервировать (YYYY-MM-DD)',
        )

    def handle(self, *args, **options):
        cutoff = None
        if options['date']:
            try:
                cutoff = date.fromisoformat(options['date'])
            except ValueError as exc:
                raise CommandError(
                    f'Некорректная дата: {options["date"]} (ожидается YYYY-MM-DD)'
                ) from exc

        result = reserve_drafts_from_date(
            cutoff_date=cutoff,
            dry_run=options['dry_run'],
            limit=options['limit'],
        )

        message = result['message']
        if result.get('is_error'):
            self.stderr.write(self.style.ERROR(message))
            for error in result.get('errors', []):
                self.stderr.write(f'  {error}')
            return
        if result.get('dry_run'):
            self.stdout.write(self.style.WARNING(message))
        elif result.get('skipped_full'):
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.SUCCESS(message))
