# app_cz\routers.py


class ArchiveRouter:
    """
    Маршрутизация модели CISCodeArchive в отдельную архивную базу ('archive').

    Архивная копия кода маркировки — денормализованный снимок без внешних ключей,
    поэтому она живёт в своей базе и не создаётся в рабочей ('default').
    """

    archive_app = 'app_cz'
    archive_model = 'ciscodearchive'

    def db_for_read(self, model, **hints):
        if (
                model._meta.app_label == self.archive_app
                and model._meta.model_name == self.archive_model
        ):
            return 'archive'
        return None

    def db_for_write(self, model, **hints):
        if (
                model._meta.app_label == self.archive_app
                and model._meta.model_name == self.archive_model
        ):
            return 'archive'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # У архивной модели нет внешних ключей.
        if (
                obj1._meta.model_name == self.archive_model
                or obj2._meta.model_name == self.archive_model
        ):
            return False
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Архивная таблица создаётся только в базе 'archive'.
        if model_name == self.archive_model:
            return db == 'archive'
        # В архивную базу ничего кроме архивной таблицы не переносим.
        if db == 'archive':
            return False
        return None