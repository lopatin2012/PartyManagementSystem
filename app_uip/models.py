# app_uip\models.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.validators import RegexValidator

from app_helper.models import UUIDModel

from app_factory.models import Line, ProductSKU

party_number_validator = RegexValidator(
    regex=r'^[0-9]{14}[0-9]{6}[A-Za-z0-9/.,\-]{1,12}$',
    message='Номер партии должен состоять из 14 цифр GTIN, 6 цифр даты и 1-12 символов серийного номера'
)


class PartyStatusChoices(models.TextChoices):
    """Статусы УИП (согласно ТЗ)."""
    DRAFT = 'draft', 'Черновик'  # Не возвращать в 1С. # NOT TRUE.
    RESERVED_CZ = 'reserved_cz', 'Сгенерирован'  # TRUE
    RESERVED_LOCAL = 'reserved_local', 'Зарезервирован'  # TRUE
    REGISTERED = 'registered', 'Зарегистрирован'  # TRUE
    CLOSED = 'closed', 'Партия закрыта'  # Нет новых заданий по УИП в течении 3-х дней после даты производства. # TRUE
    DELETED = 'deleted', 'Удалён'  # УИП сгорел по истечению 30 дней. # NOT TRUE.
    ARCHIVED = 'archived', 'В архиве'  # Если не было активных заданий в течение 30 дней. # TRUE


class ProductionPartyStatusChoices(models.TextChoices):
    """Статусы производственной партии (согласно ТЗ)."""
    CHECK = 'check', 'Проверка'  # Задание создано в предварительном статусе. #.
    CREATED = 'created', 'Создан'  # Первичный активный статус.
    WORK = 'work', 'В работе'  # Задание партии открыто на линии.
    CLOSED = 'closed', 'Закрыто'  # Задание партии было закрыто на линии.
    COMPLETED = 'completed', 'Завершено'  # Мастер производства подтвердил что задание партии завершено.
    DELETED = 'deleted', 'Удалено'  # Задание партии удалено.
    ARCHIVED = 'archived', 'Архив'  # Задание партии в архиве.
    ERROR = 'error', 'Ошибка'  # Задание в ошибке.


# Уникальный Идентификатор Партии.
class UIP(UUIDModel):
    """УИП — резервирование в Честном Знаке."""

    # Активные статусы.
    ACTIVE_STATUSES = [
        PartyStatusChoices.RESERVED_CZ,
        PartyStatusChoices.RESERVED_LOCAL,
        PartyStatusChoices.REGISTERED,
        PartyStatusChoices.CLOSED,
        PartyStatusChoices.ARCHIVED,
    ]

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

    # Статус.
    status = models.CharField(
        max_length=20,
        choices=PartyStatusChoices.choices,
        default=PartyStatusChoices.DRAFT,
        verbose_name='Статус УИП',
        db_index=True
    )
    is_desync = models.BooleanField(
        default=False,
        verbose_name='Рассинхрон данных',
        help_text='УИП зарегистрирован/закрыт/в архиве, но всё ещё числится зарезервированным в ЧЗ',
        db_index=True,
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

    # Информация по УИП для дальнейших вычислений.
    production_date = models.DateField(null=True, blank=True, verbose_name='Дата производства')
    reservation_date = models.DateField(null=True, blank=True, verbose_name='Дата резервирования')

    # Audit.
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата закрытия')
    archived_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата архивации')

    class Meta:
        verbose_name = 'УИП (уникальный идентификатор партии)'
        verbose_name_plural = '1. УИП'
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

    @property
    def is_registered(self) -> bool:
        """УИП был зарегистрирован в ЧЗ."""
        return self.status in [
            PartyStatusChoices.REGISTERED,
            PartyStatusChoices.CLOSED,
            PartyStatusChoices.ARCHIVED
        ]

    @property
    def gtin(self) -> str:
        """GTIN — первые 14 символов номера УИП."""
        return self.number[:14] if self.number else ''

    def change_status(self, new_status: str, source: str, note: str = '', changed_by=None) -> bool:
        """
        Централизованная смена статуса УИП с записью в историю.
        Все изменения статуса в коде делать через этот метод.

        :param new_status: Новый статус (из PartyStatusChoices).
        :param source: Источник изменения ('admin', 'sync', 'api', 'service'...).
        :param note: Читаемый комментарий.
        :param changed_by: Пользователь-инициатор при наличии.
        :return: True, если статус изменился, иначе False.
        """
        if self.status == new_status:
            return False

        from_status = self.status

        # Автоматические даты для служебных переходов.
        if new_status == PartyStatusChoices.CLOSED and not self.closed_at:
            self.closed_at = timezone.now()
        elif new_status == PartyStatusChoices.ARCHIVED and not self.archived_at:
            self.archived_at = timezone.now()

        self.status = new_status
        self.save()

        UIPStatusLog.objects.create(
            uip=self,
            from_status=from_status,
            to_status=new_status,
            source=source,
            note=note,
            changed_by=changed_by,
        )
        return True


class UIPStatusLog(UUIDModel):
    """История переходов статуса УИП (аудит)."""
    uip = models.ForeignKey(
        to=UIP,
        on_delete=models.CASCADE,
        verbose_name='УИП',
        related_name='status_logs'
    )
    from_status = models.CharField(
        max_length=20,
        choices=PartyStatusChoices.choices,
        null=True,
        blank=True,
        verbose_name='Предыдущий статус'
    )
    to_status = models.CharField(
        max_length=20,
        choices=PartyStatusChoices.choices,
        verbose_name='Новый статус'
    )
    source = models.CharField(
        max_length=50,
        verbose_name='Источник',
        help_text='Откуда инициировано изменение: admin, sync, api, service и т.д.'
    )
    note = models.TextField(
        null=True,
        blank=True,
        verbose_name='Комментарий'
    )
    changed_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Изменил',
        related_name='uip_status_changes'
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата перехода')

    class Meta:
        verbose_name = 'Лог статуса УИП'
        verbose_name_plural = '2. Логи статусов УИП'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['uip', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.uip.number}: {self.from_status} → {self.to_status} ({self.source})'


class ProductionParty(UUIDModel):
    """Производственная партия — задание на линии."""

    # Связь с УИП.
    uip = models.ForeignKey(
        to=UIP,
        on_delete=models.CASCADE,
        verbose_name='УИП',
        related_name='production_parties'
    )

    # Связь с производством.
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
        default='',
        verbose_name='Номер задания во внешней системе',
        help_text='Например, номер задания в "Молвест.Маркировка"'
    )

    # Внутренний номер производственной партии.
    production_party = models.CharField(
        max_length=32,
        verbose_name='Партия',
        help_text='Внутренний номер партии (обычно 0-999)'
    )

    # Статус.
    status = models.CharField(
        max_length=20,
        choices=ProductionPartyStatusChoices.choices,
        default=ProductionPartyStatusChoices.CREATED,
        verbose_name='Статус',
        help_text='Задание производственной партии',
        db_index=True
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
        verbose_name_plural = '2. Производственные партии'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['uip', '-created_at']),
            models.Index(fields=['line', '-production_datetime_start'])
        ]

    @property
    def product_sku(self):
        """Номенклатура (SKU) — через УИП."""
        return self.uip.product_sku

    @property
    def product(self):
        """Продукт — через УИП."""
        return self.uip.product_sku.product

    @property
    def gtin(self) -> str:
        """GTIN потребительской упаковки — через УИП."""
        return self.uip.gtin

    def __str__(self) -> str:
        return f'{self.uip.number} | Партия {self.production_party}'
