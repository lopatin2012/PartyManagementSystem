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


class TypeFormationUIP(models.IntegerChoices):
    """Тип формата УИПа"""
    general = 1, 'Обычный'
        # GTIN(14) + дата ГГММДД(6) + артикул(5) + добивка нулями до 32.'
        # Пример: 04601751029980260724143620000000'
    party_beginning = 2, 'Обычный + партия в начале'
        # Пример: 04601751029980260724143629990000'
    party_end = 3, 'Обычный + партия в конце' # До 3-х символов. 999
        # Пример: 04601751029980260724143620000999'
    natura = 4, 'НатураПРО'
        # Партия формируется: 26(год) + 19(неделя) + 5(день недели) + 20(партия за день)'
        # Пример: 046017510299802607240000-2619520'


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
    ip_address = models.GenericIPAddressField(
        blank=True, null=True,
        verbose_name='ip-адрес локального приложения',
        help_text='Локально приложение должно поддерживать необходимые методы'
    )
    port_address = models.IntegerField(
        blank=True, null=True,
        verbose_name='порт-адрес локального приложения',
        help_text='Локально приложение должно поддерживать необходимые методы'
    )
    is_active = models.BooleanField(default=True, verbose_name='Действующий')

    class Meta:
        verbose_name = "Завод"
        verbose_name_plural = "1. Заводы"
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
        verbose_name_plural = "2. Цеха"
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
        verbose_name_plural = "3. Линии"
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
    shelf_life_in_minutes = models.IntegerField(default=0, verbose_name='Срок годности в минутах')
    item_condition = models.CharField(
        max_length=50, choices=StateConditionChoices.choices, verbose_name='Состояние товара'
    )
    card_status = models.CharField(
        max_length=50, choices=CardStateChoices.choices, verbose_name='Состояние карточки'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')

    class Meta:
        verbose_name = 'Продукт'
        verbose_name_plural = '4. Продукты'
        ordering = ('group', 'name')

    def __str__(self):
        product_name = (
            f"{self.name[:20]}..."
            if len(self.name) > 20
            else self.name
        )
        return f'{product_name}'

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
    gtin = models.CharField(
        max_length=14,
        blank=False, null=False,
        unique=True,
        validators=[RegexValidator(regex=r'^\d{14}$', message='GTIN должен состоять ровно из 14 цифр')],
        verbose_name='GTIN',
        help_text='14-значный номер GTIN для данной упаковки'
    )
    quantity_inside = models.PositiveIntegerField(
        verbose_name='Количество в упаковке',
        help_text='Сколько штук (или коробок) помещается в эту упаковку. Для штуки = 1.'
    )
    code_storage_period_in_days = models.IntegerField(
        default=60, verbose_name='Срок хранения кодов в днях'
    )
    code_tnved = models.CharField(
        blank=True, null=True,
        max_length=50,
        verbose_name='Код ТНВЭД', help_text='Ветеринарный номер'
    )
    is_active = models.BooleanField(default=True, verbose_name='Активна')

    class Meta:
        verbose_name = 'Упаковка продукта (GTIN)'
        verbose_name_plural = '5. Упаковки продукта (GTIN)'
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
    article = models.CharField(
        max_length=100,
        unique=True,
        help_text='Уникальный код внутри организации (например, из 1С)',
        verbose_name='Код внутри организации'
    )
    type_formation_uip = models.PositiveSmallIntegerField(
        default=TypeFormationUIP.general,
        choices=TypeFormationUIP.choices,
        verbose_name='Тип формирования УИП'
    )
    is_active = models.BooleanField(default=True, verbose_name='Используется')

    class Meta:
        verbose_name = 'Номенклатура (SKU)'
        verbose_name_plural = '6. Номенклатуры (SKUs)'
        ordering = ('product', 'article')

    def __str__(self):
        status = 'Исп.' if self.is_active else 'Выведен'
        product_name = (
            f"{self.product.name[:20]}..."
            if len(self.product.name) > 20
            else self.product.name
        )
        return f'{self.article} | {product_name} [{status}]'


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
        verbose_name_plural = '7. Места производства продукции'
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(
                fields=['product_sku', 'line'],
                name='unique_sku_on_line'
            )
        ]

    def __str__(self):
        return f'{self.product_sku.product.name} | {self.product_sku.article}'

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
