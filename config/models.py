# config/models.py

from django.db import models

from app_helper.models import UUIDModel


class TypeServiceChoices(models.IntegerChoices):
    """Тип внешнего сервиса."""
    SIGNATURE = 1, 'Сервис подписей'
    WMS = 2, 'WMS'
    PRINT = 3, 'Сервис печати'


class ExternalService(UUIDModel):
    """Внешний сервис (подписание ЭЦП и др.)."""
    service_type = models.IntegerField(
        choices=TypeServiceChoices.choices,
        unique=True,
        verbose_name='Тип сервиса',
        help_text='Тип внешнего сервиса. Должен быть уникальным.'
    )
    name = models.CharField(max_length=50, unique=True, verbose_name='Наименование')
    photo = models.ImageField(
        blank=True, null=True,
        upload_to='factory_image',
        verbose_name='Фотография'
    )
    ip_address = models.GenericIPAddressField(
        verbose_name='IP-адрес сервиса',
        help_text='IP-адрес, на котором запущен внешний сервис'
    )
    port_address = models.IntegerField(
        verbose_name='Порт сервиса',
        help_text='Порт, на котором запущен внешний сервис'
    )
    is_active = models.BooleanField(default=True, verbose_name='Действующий')

    class Meta:
        verbose_name = 'Внешний сервис'
        verbose_name_plural = '1. Внешние сервисы'
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(
                fields=['ip_address', 'port_address'],
                name='unique_ip_address_and_port_address'
            )
        ]

    def __str__(self) -> str:
        return self.name
