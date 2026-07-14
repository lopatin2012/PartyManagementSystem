from django.db import models
from django.core.exceptions import ValidationError

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
    PENDING = 2, 'Ожидает нанесения'
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
        verbose_name_plural = 'Коды маркировки'
        ordering = ('-created_at',)
        indexes = [
            # Оставляем только критичные индексы. 
            # Убрали uip и product_sku, так как их больше нет в модели.
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
