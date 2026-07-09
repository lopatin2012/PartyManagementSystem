from uuid_utils import uuid7

from django.db import models

class UUIDModel(models.Model):
    """Абстрактная модель с UUID в качестве первичного ключа."""
    id = models.UUIDField(
        primary_key=True,
        default=uuid7,
        editable=False,
        help_text='Уникальный идентификатор'
    )

    class Meta:
        abstract = True
