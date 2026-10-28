# app_event/management/commands/cleanup_logs.py

"""
Сроки хранения логов/метрик и очистка устаревших записей.

Без флагов — только отчёт: сколько строк в каждой «логовой» таблице, самая
старая запись и настроенный срок хранения.

С `--cleanup` — удаляет устаревшие записи по всем таблицам (то же, что
ежедневные задачи планировщика, но сразу и с итогом).

Примеры:
    python manage.py cleanup_logs
    python manage.py cleanup_logs --cleanup
"""

from django.core.management.base import BaseCommand

from app_event.services import retention


class Command(BaseCommand):
    help = 'Отчёт по срокам хранения логов/метрик; --cleanup удаляет устаревшее'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Удалить записи старше настроенных сроков хранения',
        )

    def handle(self, *args, **options):
        if options['cleanup']:
            self._cleanup()
        else:
            self._report()

    def _report(self):
        self.stdout.write('Таблица               Строк      Старейшая    Хранить')
        for row in retention.retention_report():
            oldest = (
                row['oldest'].strftime('%d.%m.%Y')
                if row['oldest'] else '—'
            )
            self.stdout.write(
                f'{row["table"]:<20} {row["rows"]:>8}  '
                f'{oldest:<12} {row["retention_days"]} дн.'
            )
        self.stdout.write(
            '\nЗапустите с --cleanup, чтобы удалить устаревшие записи.'
        )

    def _cleanup(self):
        has_error = False
        for result in retention.cleanup_all():
            if result.get('is_error'):
                has_error = True
                self.stderr.write(
                    f'{result["table"]}: ошибка — {result.get("message", "")}'
                )
                continue
            self.stdout.write(
                f'{result["table"]}: удалено {result["deleted"]} '
                f'(старше {result["days"]} дн.)'
            )
        if has_error:
            self.stderr.write('Часть очисток завершилась с ошибкой.')
        else:
            self.stdout.write(self.style.SUCCESS('Очистка завершена.'))
