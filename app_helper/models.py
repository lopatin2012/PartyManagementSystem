import uuid

from uuid_utils import uuid7

from django.db import models

class UUID7Field(models.UUIDField):
    """Кастомный UUIDv7"""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('default', uuid7)
        kwargs.setdefault('editable', False)
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if value is None:
            return None

        if isinstance(value, uuid.UUID):
            return value

        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Значение не является корректным UUID.')

class UUIDModel(models.Model):
    """Абстрактная модель с UUID в качестве первичного ключа."""
    id = UUID7Field(
        primary_key=True,
        default=uuid7,
        editable=False,
        help_text='Уникальный идентификатор'
    )

    class Meta:
        abstract = True
