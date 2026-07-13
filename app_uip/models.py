# app_uip\models.py

from django.db import models
from django.core.validators import RegexValidator

from app_helper.models import UUIDModel

from app_factory.models import Factory, Workshop, Line, ProductSKU

party_number_validator = RegexValidator(
    regex=r'^[0-9]{14}[0-9]{6}[A-Za-z0-9/.,\-]{1,12}$',
    message='Номер партии должен состоять из 14 цифр GTIN, 6 цифр даты и 1-12 символов серийного номера'
)

class PartyStatusChoices(models.TextChoices):
    """Статусы производственной партии (согласно ТЗ)."""
    DRAFT = 'draft', 'Черновик, не отправлен в ЧЗ'
    RESERVED_CZ = 'reserved_cz', 'Зарезервирован в ЧЗ (сгенерирован)'
    RESERVED_LOCAL = 'reserved_local', 'Зарезервирован в ЧЗ (собственный)'
    ACTIVE = 'active', 'Отчёт о нанесении отправлен, партия в работе'
    CLOSED = 'closed', 'Партия закрыта'
    ARCHIVE = 'archive', 'В архиве'


class PartyNumberTypeChoices(models.TextChoices):
    """Тип номера партии."""
    CZ_AUTO = 'cz_auto', 'Сгенерирован ЧЗ'
    LOCAL = 'local', 'Собственный номер'


# Уникальный Идентификатор Партии.
class UIP(UUIDModel):
    """УИП — резервирование в Честном Знаке."""

    # Связи.
    product_sku = models.ForeignKey(
        to=ProductSKU,
        on_delete=models.PROTECT,
        verbose_name='Продукт (SKU)',
        related_name='uips'
    )

    # Идентификаторы.
    number = models.CharField(
        max_length=32,
        unique=True,
        validators=[party_number_validator],
        verbose_name='Номер УИП (partyNumber)',
        help_text='Формат: 14 цифр GTIN + 6 цифр даты (ГГММДД) + 1-12 символов серийного номера'
    )
    number_type = models.CharField(
        max_length=20,
        choices=PartyNumberTypeChoices.choices,
        verbose_name='Тип номера'
    )

    # Статус.
    status = models.CharField(
        max_length=20,
        choices=PartyStatusChoices.choices,
        default=PartyStatusChoices.DRAFT,
        verbose_name='Статус УИП',
        db_index=True
    )

    # Количества (суммарные по всем производственным партиям).
    planned_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Плановое количество, шт.',
        help_text='Общий план по всем производственным партиям'
    )
    produced_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Фактическое количество, шт.',
        help_text='Сумма по всем производственным партиям'
    )

    # Прочее.
    description = models.TextField(
        null=True,
        blank=True,
        verbose_name='Описание'
    )

    # Audit.
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата закрытия')
    archived_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата архивации')

    class Meta:
        verbose_name = 'УИП (уникальный идентификатор партии)'
        verbose_name_plural = 'УИП'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['product_sku', 'status']),
            models.Index(fields=['number'])
        ]

    def __str__(self) -> str:
        return f'{self.number} ({self.get_status_display()})'

    @property
    def can_be_deleted(self) -> bool:
        """УИП можно удалить только в статусе draft."""
        return self.status == PartyStatusChoices.DRAFT

    @property
    def is_reserved(self) -> bool:
        """УИП зарезервирован в ЧЗ."""
        return self.status in [
            PartyStatusChoices.RESERVED_CZ,
            PartyStatusChoices.RESERVED_LOCAL
        ]


class ProductionParty(UUIDModel):
    """Производственная партия — задание на линии."""

    # Связь с УИП.
    uip = models.ForeignKey(
        to=UIP,
        on_delete=models.CASCADE,
        verbose_name='УИП',
        related_name='production_parties'
    )

    # Место производства.
    factory = models.ForeignKey(
        to=Factory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Завод',
        related_name='production_parties'
    )
    workshop = models.ForeignKey(
        to=Workshop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Цех',
        related_name='production_parties'
    )
    line = models.ForeignKey(
        to=Line,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Линия',
        related_name='production_parties'
    )

    # Номер задания во внешней системе.
    external_number_task = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        verbose_name='Номер задания во внешней системе',
        help_text='Например, номер задания в "Молвест.Маркировка"'
    )

    # Внутренний номер производственной партии.
    production_party = models.CharField(
        max_length=32,
        verbose_name='Производственная партия',
        help_text='Внутренний номер партии (обычно 0-999)'
    )

    # Даты.
    production_datetime_start = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата и время начала выпуска'
    )
    production_datetime_end = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата и время окончания выпуска'
    )
    marking_datetime = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата и время маркировки'
    )
    expiration_datetime = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Срок годности'
    )

    # Количества.
    planned_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Плановое количество, шт.'
    )
    produced_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name='Фактическое количество, шт.'
    )

    # Audit.
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        verbose_name = 'Производственная партия'
        verbose_name_plural = 'Производственные партии'
        ordering = ('-created_at', )
        indexes = [
            models.Index(fields=['uip', '-created_at']),
            models.Index(fields=['line', '-production_datetime_start'])
        ]

    def __str__(self) -> str:
        return f'{self.uip.number} | Партия {self.production_party}'
