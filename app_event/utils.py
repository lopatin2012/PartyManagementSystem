# app_event/utils.py

from app_event.models import EventLog


def log_event(module, level, message, actor='system', metadata=None):
    """Создать запись в журнале событий."""
    EventLog.objects.create(
        module=module,
        level=level,
        message=message,
        actor=actor,
        metadata=metadata or {},
    )
