from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils import timezone

from app_factory.models import PackagingLevelChoices


class CISCodesStatusChoices(models.IntegerChoices):
    """Статус кода со стороны Честного Знака."""
    EMITTED = 1, 'Эмитирован'
    APPLIED = 2, 'Нанесён (оплачен)'
    INTRODUCED_INTO_CIRCULATION = 3, 'Введён в оборот'
    WITHDRAWN_FROM_CIRCULATION = 4, 'Выведен из оборота'


class ProductionCodeStatusChoices(models.IntegerChoices):
    """Статус кода со стороны производства."""
    FREE = 1, 'Свободный'
    PENDING = 2, 'Напечатан'
    APPLIED = 3, 'Нанесён'
    REJECTED = 4, 'Отбракован'
    SHIPPED = 5, 'Отгружен'


class CISCode(models.Model):
    """Код маркировки DM (DataMatrix)"""

    production_party = models.ForeignKey(
        to='app_uip.ProductionParty',
        on_delete=models.PROTECT,
        verbose_name='Производственная партия',
        related_name='cis_codes'
    )

    product_packaging = models.ForeignKey(
        to='app_factory.ProductPackaging',
        on_delete=models.PROTECT,
        verbose_name='Упаковка продукта (GTIN)',
        related_name='cis_codes'
    )

    code = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Код маркировки'
    )

    # Дублируем уровень упаковки для быстрого доступа и фильтрации без JOIN.
    level = models.PositiveSmallIntegerField(
        choices=PackagingLevelChoices.choices,
        verbose_name='Уровень упаковки',
        db_index=True
    )

    cz_status = models.PositiveSmallIntegerField(
        choices=CISCodesStatusChoices.choices,
        default=CISCodesStatusChoices.EMITTED,
        verbose_name='Статус со стороны ЧЗ',
        db_index=True
    )

    production_status = models.PositiveSmallIntegerField(
        choices=ProductionCodeStatusChoices.choices,
        default=ProductionCodeStatusChoices.FREE,
        verbose_name='Статус со стороны производства',
        db_index=True
    )

    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Родительский код (агрегация)',
        related_name='children'
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        verbose_name = 'Код маркировки'
        verbose_name_plural = '1. Коды маркировки'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['production_party', '-created_at']),
            models.Index(fields=['product_packaging', '-created_at']),
            models.Index(fields=['cz_status', '-created_at']),
            models.Index(fields=['production_status', '-created_at']),
            models.Index(fields=['parent', 'level']),
        ]

    def __str__(self) -> str:
        return f'{self.code[:24]}... ({self.get_cz_status_display()})'

    # ==========================================
    # Свойства для быстрого доступа.
    # ==========================================
    @property
    def product_sku(self):
        """Получаем SKU через упаковку и продукт (без хранения лишнего ID в БД)."""
        return self.product_packaging.product.skus.first()

    @property
    def uip(self):
        """Получаем УИП через производственную партию."""
        return self.production_party.uip

    def clean(self):
        """Проверяем согласованность уровня упаковки."""
        if self.product_packaging_id and self.level != self.product_packaging.level:
            raise ValidationError({
                'level': 'Уровень упаковки кода должен соответствовать уровню упаковки продукта (GTIN)'
            })

    def save(self, *args, **kwargs):
        # Автоматически проставляем уровень упаковки из product_packaging при создании.
        if self.product_packaging_id and not self.level:
            self.level = self.product_packaging.level

        super().save(*args, **kwargs)


class CISCodeArchive(models.Model):
    """
    Архивная копия кода маркировки (денормализованный снимок).

    Хранится в отдельной архивной базе ('archive') через ArchiveRouter.
    Коды старше ARCHIVE_AFTER_DAYS дней переносятся сюда командой
    `archive_old_codes` и удаляются из рабочей таблицы — историчность
    данных сохраняется в архиве, чистка архива выполняется вручную.
    """

    # Исходный идентификатор кода (для трассировки).
    id = models.BigIntegerField(primary_key=True, verbose_name='Исходный ID кода')
    code = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Код маркировки'
    )
    level = models.PositiveSmallIntegerField(
        choices=PackagingLevelChoices.choices,
        verbose_name='Уровень упаковки',
        db_index=True
    )
    cz_status = models.PositiveSmallIntegerField(
        choices=CISCodesStatusChoices.choices,
        verbose_name='Статус со стороны ЧЗ',
        db_index=True
    )
    production_status = models.PositiveSmallIntegerField(
        choices=ProductionCodeStatusChoices.choices,
        verbose_name='Статус со стороны производства',
        db_index=True
    )
    parent_code = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name='Родительский код (агрегация)'
    )

    # Привязка к производству (денормализовано — без внешних ключей).
    production_party_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Производственная партия (ID)'
    )
    party_number = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        verbose_name='Номер партии'
    )
    party_status = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        verbose_name='Статус партии'
    )
    uip_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='УИП (ID)'
    )
    uip_number = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Номер УИП'
    )
    product_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Продукт (ID)'
    )
    product_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name='Наименование продукта'
    )
    product_sku_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Номенклатура (SKU, ID)'
    )
    sku_article = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name='Артикул (SKU)'
    )
    packaging_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Упаковка (ID)'
    )
    gtin = models.CharField(
        max_length=14,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='GTIN'
    )

    # Даты.
    created_at = models.DateTimeField(db_index=True, verbose_name='Создано')
    updated_at = models.DateTimeField(verbose_name='Обновлено')
    archived_at = models.DateTimeField(auto_now_add=True, verbose_name='Архивировано')

    class Meta:
        verbose_name = 'Код маркировки (архив)'
        verbose_name_plural = '1. Коды маркировки (архив)'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['production_party_id', '-created_at']),
            models.Index(fields=['cz_status', '-created_at']),
            models.Index(fields=['production_status', '-created_at']),
            models.Index(fields=['level', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.code[:24]}... (архив)'


class SUZAccount(models.Model):
    """
    Учётная запись для взаимодействия с СУЗ и TrueAPI.
    """

    # Основные данные.
    is_active = models.BooleanField(
        default=False,
        verbose_name="Активная запись",
        help_text="Только одна запись может быть активной одновременно."
    )
    certificate_name = models.CharField(
        max_length=150,
        verbose_name="Наименование сертификата"
    )
    serial_number = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="Серийный номер сертификата"
    )
    inn = models.CharField(
        max_length=12,
        validators=[
            RegexValidator(
                regex=r'^\d{10,12}$',
                message='ИНН должен содержать 10 или 12 цифр'
            )
        ],
        verbose_name="ИНН организации"
    )

    # Сроки действия.
    valid_from = models.DateTimeField(
        blank=True, null=True,
        verbose_name="Действителен с"
    )
    valid_to = models.DateTimeField(
        blank=True, null=True,
        verbose_name="Действителен по"
    )

    # Параметры подключения.
    oms_id = models.CharField(
        max_length=100,
        verbose_name="СУЗ ID (omsId)"
    )
    device_name = models.CharField(
        max_length=150,
        verbose_name="Название устройства (suz_device_name)"
    )
    connection_identifier = models.CharField(
        max_length=100,
        verbose_name="Идентификатор соединения (identifier_id)"
    )

    # Токены.
    dynamic_token = models.CharField(
        max_length=512,
        blank=True, null=True,
        verbose_name="Текущий динамический токен (client_token)"
    )
    token_expires_at = models.DateTimeField(
        blank=True, null=True,
        verbose_name="Токен действителен до"
    )

    # Аудит.
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата добавления"
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата и время изменения"
    )

    class Meta:
        verbose_name = "Учётная запись СУЗ"
        verbose_name_plural = "2. Учётные записи СУЗ"
        ordering = ["-is_active", "-updated_at"]

        constraints = [
            models.UniqueConstraint(
                fields=['is_active'],
                condition=models.Q(is_active=True),
                name='unique_active_suz_account'
            )
        ]

    def __str__(self):
        status = "Активна" if self.is_active else "Неактивна"
        return f'{self.certificate_name} ({self.inn}) | {status}'

    def save(self, *args, **kwargs):
        """
        Переопределяем save, чтобы при активации этой записи,
        все остальные записи автоматически деактивировались.
        """
        if self.is_active:
            # Снимаем флаг активности со всех остальных записей.
            SUZAccount.objects.exclude(pk=self.pk).update(is_active=False)

        super().save(*args, **kwargs)

    @property
    def is_token_valid(self) -> bool:
        """Проверяет, не истёк ли срок действия текущего динамического токена."""
        if not self.dynamic_token or not self.token_expires_at:
            return False

        # Добавляем небольшой буфер (например, 5 минут), чтобы не использовать токен на грани истечения.
        return timezone.now() < (self.token_expires_at - timezone.timedelta(minutes=5))

    def get_active_account(self):
        """Получение активной записи для работы с СУЗ/TrueAPI."""
        return SUZAccount.objects.filter(is_active=True).first()
