# app_factory/management/commands/sync_molvest_reference.py


"""
Синхронизация справочников цехов/линий/продуктов из Molvest.Маркировка в СУП.

Добавляет недостающие данные в СУП и после создания возвращает uuid объектов
обратно в Molvest (заполняется uuid_str в Molvest.Маркировка).

Примеры:
    python manage.py sync_molvest_reference
    python manage.py sync_molvest_reference --factory-id <uuid>
    python manage.py sync_molvest_reference --dry-run
"""

from django.core.management.base import BaseCommand

from app_factory.services.molvest_reference_sync import sync_molvest_reference


class Command(BaseCommand):
    help = 'Синхронизация цехов, линий и продуктов из Molvest.Маркировка в СУП'

    def add_arguments(self, parser):
        parser.add_argument(
            '--factory-id',
            action='append',
            dest='factory_ids',
            help='ID завода для синхронизации (можно указать несколько)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только подсчёт без изменений',
        )

    def handle(self, *args, **options):
        result = sync_molvest_reference(
            factory_ids=options.get('factory_ids'),
            dry_run=options['dry_run'],
        )

        if options['dry_run'] or result['factories'] == 0:
            self.stdout.write(self.style.WARNING(result['message']))
            if result['factories'] == 0:
                return

        for factory_id, ws in result['workshops'].items():
            self.stdout.write(
                f'Завод {factory_id}: цеха — получено {ws["fetched"]}, '
                f'создано {ws["created"]}, обновлено {ws["updated"]}, '
                f'uuid возвращено {ws["uuids_pushed"]}'
            )
        for factory_id, ln in result['lines'].items():
            self.stdout.write(
                f'Завод {factory_id}: линии — получено {ln["fetched"]}, '
                f'создано {ln["created"]}, обновлено {ln["updated"]}, '
                f'uuid возвращено {ln["uuids_pushed"]}'
            )
        for factory_id, pr in result['products'].items():
            self.stdout.write(
                f'Завод {factory_id}: продукты — получено {pr["fetched"]}, '
                f'создано {pr["created"]}, обновлено {pr["updated"]}, '
                f'SKU {pr["skus_created"]}, упаковок {pr["packagings_created"]}, '
                f'мест производства {pr["locations_created"]}, '
                f'uuid возвращено {pr["uuids_pushed"]}'
            )

        if result['is_error']:
            self.stderr.write(self.style.ERROR(result['message']))
        else:
            self.stdout.write(self.style.SUCCESS(result['message']))