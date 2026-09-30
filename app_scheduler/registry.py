# app_scheduler/registry.py

"""
Единый реестр периодических задач: имя из `SCHEDULE` → функция задачи.

Используется планировщиком (`run_scheduler`), API расписания и ручным запуском,
чтобы набор задач и их привязка не расходились между собой.
"""


def get_scheduled_tasks() -> dict:
    """Возвращает {имя_задачи: task_func} для всех задач из `SCHEDULE`."""
    from app_scheduler.tasks import (
        accumulate_short_shelf_life_reserve_task,
        archive_old_codes_task,
        archive_stale_closed_uips_task,
        check_system_health_task,
        check_uip_burn_task,
        check_uip_reserve_task,
        cleanup_expired_reserved_uips_task,
        cleanup_old_logs_task,
        cleanup_old_task_results_task,
        close_unused_registered_uips_task,
        refresh_suz_token_task,
        register_reserved_uips_task,
        sync_external_parties_codes_task,
        sync_molvest_reference_task,
        sync_national_catalog_task,
        sync_parties_task,
        sync_product_activity_task,
    )

    return {
        'refresh_suz_token': refresh_suz_token_task,
        'cleanup_expired_reserved': cleanup_expired_reserved_uips_task,
        'close_unused_registered': close_unused_registered_uips_task,
        'archive_stale_closed': archive_stale_closed_uips_task,
        'cleanup_old_logs': cleanup_old_logs_task,
        'cleanup_old_task_results': cleanup_old_task_results_task,
        'sync_external_parties_codes': sync_external_parties_codes_task,
        'sync_molvest_reference': sync_molvest_reference_task,
        'check_uip_reserve': check_uip_reserve_task,
        'check_uip_burn': check_uip_burn_task,
        'register_reserved_uips': register_reserved_uips_task,
        'sync_parties': sync_parties_task,
        'check_system_health': check_system_health_task,
        'sync_product_activity': sync_product_activity_task,
        'accumulate_short_shelf_life_reserve': accumulate_short_shelf_life_reserve_task,
        'sync_national_catalog': sync_national_catalog_task,
        'archive_old_codes': archive_old_codes_task,
    }
