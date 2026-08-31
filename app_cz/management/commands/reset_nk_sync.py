# app_cz/management/commands/reset_nk_sync.py

"""
Сброс «зависшей» блокировки синхронизации Национального каталога.

Если процесс/контейнер упал во время синхронизации НК, в таблице
NKSyncState остаётся is_running=True и новые синхронизации отклоняются
до истечения heartbeat-таймаута (LOCK_STALE_TIMEOUT). Команда принудительно
снимает блокировку.

Использование:
    python manage.py reset_nk_sync
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Сбрасывает зависшую блокировку синхронизации Национального каталога'

    def handle(self, *args, **options):
        from app_cz.services.nk_sync_state import get_sync_state, finish_sync

        state = get_sync_state()
        if not state['running']:
            self.stdout.write(self.style.SUCCESS(
                'Блокировка синхронизации НК не активна — сброс не требуется.'
            ))
            return

        started = state.get('started_at') or 'неизвестно'
        by = state.get('started_by_display') or state.get('started_by') or '—'
        finish_sync(message='Блокировка сброшена вручную (management command)')
        self.stdout.write(self.style.WARNING(
            f'Блокировка синхронизации НК была активна (запущена {by}, '
            f'начата {started}) — принудительно сброшена. '
            f'Теперь синхронизацию можно запустить заново.'
        ))
