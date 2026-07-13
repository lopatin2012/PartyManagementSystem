# app_factory\models.py

from django.db import models
from django.core.validators import RegexValidator

from app_helper.models import UUIDModel


# ==========================================
# Справочники и Константы
# ==========================================
PRODUCT_GROUP_CZ_IDS = {
    'null': 0,
    'milk': 8,
    'bio': 17,
}


class ProductGroupChoices(models.TextChoices):
    """Товарная группа"""
    NOT_SELECTED = 'null', 'Товарная группа не выбрана'
    MILK = 'milk', 'Молочная продукция'
    BIO = 'bio', 'Специализированная пищевая продукция и БАД к пище'

    @classmethod
    def ch_group_id(cls, group_value):
        return PRODUCT_GROUP_CZ_IDS.get(group_value, 0)


class PackagingLevelChoices(models.IntegerChoices):
    """Уровни упаковки для агрегации в Честном ЗНАКе"""
    UNIT = 1, 'Потребительская (Штука)'
    GROUP = 2, 'Групповая (Коробка/Блок)'
    TRANSPORT = 3, 'Транспортная (Паллет)'


class StateConditionChoices(models.TextChoices):
    """Состояние товара"""
    NOT_READY_ORDER_KM = 'not_ready_order_km', 'Не готов к заказу КМ'
    READY_ORDER_KM = 'ready_order_km', 'Готов к заказу КМ'
    READY_COMMERCIALIZATION = 'ready_commercialization', 'Готов к вводу в оборот'


class CardStateChoices(models.TextChoices):
    """Состояние карточки в ЧЗ"""
    DRAFT = 'draft', 'Черновик'
    ON_MODERATION = 'on_moderation', 'На модерации'
    REQUIRES_MODERATION = 'requires_moderation', 'Требует модерацию'
    AWAITING_SIGNATURE = 'awaiting_signature', 'Ожидает подписания'
    PUBLISHED = 'published', 'Опубликована'
    IN_ARCHIVE = 'in_archive', 'В архиве'
    REQUIRES_PROCESSING = 'requires_processing', 'Требует обработки'


class ProductionBatchChoices(models.TextChoices):
    """Статусы производственной партии"""
    DRAFT = 'draft', 'Черновик'
    IN_PROGRESS = 'in_progress', 'В производстве'
    COMPLETED = 'completed', 'Завершен'
    CANCELLED = 'cancelled', 'Отменен'


class Factory(UUIDModel):
    """Завод."""
    name = models.CharField(max_length=50, unique=True, verbose_name='Наименование')
    photo = models.ImageField(
        blank=True, null=True,
        upload_to='factory_image',
        verbose_name='Фотография'
    )
    is_active = models.BooleanField(default=True, verbose_name='Действующий')

    class Meta:
        verbose_name = "Завод"
        verbose_name_plural = "Заводы"
        ordering = ('-id',)

    def __str__(self) -> str:
        return self.name


class Workshop(UUIDModel):
    """Цеха"""
    factory = models.ForeignKey(to=Factory, on_delete=models.CASCADE, verbose_name='Завод')
    name = models.CharField(max_length=50, verbose_name='Наименование')
    photo = models.ImageField(
        blank=True, null=True,
        upload_to='workshop_image',
        verbose_name='Фотография'
    )
    is_active = models.BooleanField(default=True, verbose_name='Действующий')

    class Meta:
        verbose_name = "Цех"
        verbose_name_plural = "Цеха"
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(fields=['factory', 'name'], name='unique_workshop_per_factory')
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.factory.name})'


class Line(UUIDModel):
    """Линия"""
    workshop = models.ForeignKey(to=Workshop, on_delete=models.CASCADE, verbose_name='Цех')
    name = models.CharField(max_length=50, verbose_name='Наименование')
    photo = models.ImageField(
        blank=True, null=True,
        upload_to='line_image',
        verbose_name='Фотография'
    )
    is_active = models.BooleanField(default=True, verbose_name='Действующая')

    class Meta:
        verbose_name = "Линия"
        verbose_name_plural = "Линии"
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(fields=['workshop', 'name'], name='unique_line_per_workshop')
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.workshop.name})'

# ==========================================
# Продукция и Упаковки
# ==========================================
class Product(UUIDModel):
    """Логическая сущность товара (без привязки к конкретному GTIN)"""
    group = models.CharField(
        max_length=50, choices=ProductGroupChoices.choices, verbose_name='Товарная группа продукта'
    )
    name = models.CharField(max_length=255, verbose_name='Наименование')
    shelf_life_in_days = models.IntegerField(verbose_name='Срок годности в днях')
    item_condition = models.CharField(
        max_length=50, choices=StateConditionChoices.choices, verbose_name='Состояние товара'
    )
    card_status = models.CharField(
        max_length=50, choices=CardStateChoices.choices, verbose_name='Состояние карточки'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')

    class Meta:
        verbose_name = 'Продукт'
        verbose_name_plural = 'Продукты'
        ordering = ('group', 'name')

    def __str__(self):
        status = 'Активен' if self.is_active else 'Выведен'
        return f'{self.get_group_display()} - {self.name} ({status})'

    @property
    def consumer_gtin(self):
        """Быстрый доступ к GTIN потребительской упаковки (уровень 1)"""
        packaging = self.packagings.filter(level=PackagingLevelChoices.UNIT).first()
        return packaging.gtin if packaging else None


class ProductPackaging(UUIDModel):
    """Физическая упаковка продукта с собственным GTIN (для агрегации)"""
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='packagings',
        verbose_name='Продукт'
    )
    level = models.PositiveSmallIntegerField(
        choices=PackagingLevelChoices.choices,
        verbose_name='Уровень упаковки'
    )
    name = models.CharField(
        max_length=150,
        verbose_name='Наименование упаковки',
        help_text='Например: "Бутылка 1л", "Коробка 12 шт.", "Паллет 1200 шт."'
    )
    gtin = models.CharField(
        max_length=14,
        unique=True,
        validators=[RegexValidator(regex=r'^\d{14}$', message='GTIN должен состоять ровно из 14 цифр')],
        verbose_name='GTIN упаковки',
    )
    quantity_inside = models.PositiveIntegerField(
        verbose_name='Количество в упаковке',
        help_text='Сколько штук (или коробок) помещается в эту упаковку. Для штуки = 1.'
    )
    code_storage_period_in_days = models.IntegerField(verbose_name='Срок хранения кодов в днях')
    is_active = models.BooleanField(default=True, verbose_name='Активна')

    class Meta:
        verbose_name = 'Упаковка продукта (GTIN)'
        verbose_name_plural = 'Упаковки продукта (GTIN)'
        ordering = ('product', 'level')
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'level'],
                name='unique_packaging_level_per_product'
            )
        ]

    def __str__(self):
        level_name = self.get_level_display()
        return f'{self.product.name} | {level_name} | GTIN: {self.gtin}'


class ProductSKU(UUIDModel):
    """Конкретная номенклатура в учётной системе производства (1С)"""
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        verbose_name='Продукт',
        related_name='skus',
    )
    sku_code = models.CharField(
        max_length=100,
        unique=True,
        help_text='Уникальный код внутри организации (например, из 1С)',
        verbose_name='Код внутри организации'
    )
    name = models.CharField(max_length=150, verbose_name='Наименование в 1С')
    is_active = models.BooleanField(default=True, verbose_name='Используется')

    class Meta:
        verbose_name = 'Номенклатура (SKU)'
        verbose_name_plural = 'Номенклатуры (SKUs)'
        ordering = ('product', 'sku_code')

    def __str__(self):
        status = 'Исп.' if self.is_active else 'Выведен'
        return f'{self.sku_code} | {self.name} [{status}]'


# ==========================================
# Привязка продукта к линии.
# ==========================================
class ProductProductionLocation(UUIDModel):
    product_sku = models.ForeignKey(
        ProductSKU,
        on_delete=models.PROTECT,
        verbose_name='Номенклатура (SKU)',
        related_name='product_production_locations'
    )
    line = models.ForeignKey(
        Line,
        on_delete=models.PROTECT,
        verbose_name='Производственная линия',
        related_name='product_production_locations'
    )
    is_active = models.BooleanField(default=True, verbose_name='Используется')

    class Meta:
        verbose_name = 'Место производства продукции'
        verbose_name_plural = 'Места производства продукции'
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(
                fields=['product_sku', 'line'],
                name='unique_sku_on_line'
            )
        ]

    def __str__(self):
        return f'{self.product_sku.product.name} | {self.product_sku.sku_code}'

    @property
    def product(self):
        return self.product_sku.product

    @property
    def gtin(self):
        """GTIN потребительской упаковки для заказа кодов маркировки"""
        return self.product.consumer_gtin

    @property
    def cz_group_code(self):
        return self.product.group

    @property
    def cz_group_id(self):
        return ProductGroupChoices.ch_group_id(self.product.group)
