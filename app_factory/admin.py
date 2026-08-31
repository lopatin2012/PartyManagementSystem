# app_factory/admin.py

from django.contrib import admin

from app_factory.models import (
    Factory,
    Workshop,
    Line,
    Product,
    ProductPackaging,
    ProductSKU,
    ProductProductionLocation,
    NationalCatalogProduct
)


# ==========================================
# Вспомогательные функции для отображения.
# ==========================================
@admin.display(boolean=True, description='Активен')
def is_active_display(obj):
    """Возвращает красивую иконку галочки для поля is_active."""
    return obj.is_active


# ==========================================
# Админки для справочников производства.
# ==========================================
@admin.register(Factory)
class FactoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', is_active_display)
    list_filter = ('is_active',)
    search_fields = ('name',)
    ordering = ('name',)
    list_per_page = 25


@admin.register(Workshop)
class WorkshopAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'factory', is_active_display)
    list_filter = ('factory', 'is_active')
    autocomplete_fields = ('factory',)
    search_fields = ('name', 'factory__name')
    ordering = ('factory', 'name')
    list_per_page = 25


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'workshop', 'factory_name', is_active_display)
    list_filter = ('workshop__factory', 'workshop', 'is_active')
    autocomplete_fields = ('workshop',)
    search_fields = ('name', 'workshop__name', 'workshop__factory__name')
    ordering = ('workshop', 'name')
    list_per_page = 25

    @admin.display(description='Завод')
    def factory_name(self, obj):
        return obj.workshop.factory.name


# ==========================================
# Inline для упаковок продукта.
# ==========================================
class ProductPackagingInline(admin.TabularInline):
    model = ProductPackaging
    extra = 3
    fields = ('level', 'gtin', 'quantity_inside', 'code_storage_period_in_days', 'code_tnved', 'is_active')
    ordering = ('level',)
    verbose_name = 'Упаковка'
    verbose_name_plural = 'Упаковки продукта (до 3 уровней)'

    def get_queryset(self, request):
        return super().get_queryset(request).filter(is_active=True)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == 'level':
            kwargs['empty_label'] = None
            # Добавляем подсказку в help_text
            kwargs['help_text'] = 'Выберите уровень: 1=Штука, 2=Коробка, 3=Паллет'
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'gtin':
            kwargs['help_text'] = 'Ровно 14 цифр'
        if db_field.name == 'quantity_inside':
            kwargs['help_text'] = 'Для штуки = 1'
        return super().formfield_for_dbfield(db_field, request, **kwargs)


# ==========================================
# Админки для продукции и упаковок.
# ==========================================
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'group', 'item_condition', 'card_status', is_active_display)
    list_filter = ('group', 'item_condition', 'card_status', 'is_active')
    search_fields = ('name',)
    ordering = ('-id',)
    list_per_page = 25
    inlines = [ProductPackagingInline]

    def save_formset(self, request, form, formset, change):
        """Сохраняет упаковки.

        Несколько упаковок одного уровня разрешены (разные GTIN),
        поэтому никакой проверки дубликатов уровня здесь нет.
        """
        formset.save()


@admin.register(ProductPackaging)
class ProductPackagingAdmin(admin.ModelAdmin):
    list_display = ('id', 'product', 'level', 'gtin', 'quantity_inside', is_active_display)
    list_filter = ('level', 'is_active', 'product__group')
    autocomplete_fields = ('product',)
    search_fields = ('gtin', 'product__name')
    ordering = ('-id', 'product', 'level')
    list_per_page = 25


@admin.register(ProductSKU)
class ProductSKUAdmin(admin.ModelAdmin):
    list_display = ('id', 'article', 'product', 'type_formation_uip', is_active_display)
    list_filter = ('is_active', 'product__group')
    autocomplete_fields = ('product',)
    search_fields = ('article', 'other_codes', 'product__name', 'product__packagings__gtin')
    ordering = ('-id', 'product', 'article')
    list_per_page = 50


# ==========================================
# Админки для привязок.
# ==========================================
@admin.register(ProductProductionLocation)
class ProductProductionLocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'product_sku', 'line', 'workshop_name', 'factory_name', is_active_display)
    list_filter = ('is_active', 'line__workshop__factory', 'line__workshop', 'line')
    autocomplete_fields = ('product_sku', 'line')
    search_fields = (
        'product_sku__article',
        'product_sku__product__name',
        'line__name'
    )
    ordering = ('-id',)
    list_per_page = 25

    @admin.display(description='Цех')
    def workshop_name(self, obj):
        return obj.line.workshop.name

    @admin.display(description='Завод')
    def factory_name(self, obj):
        return obj.line.workshop.factory.name


# ==========================================
# Админка Национального каталога.
# ==========================================
@admin.register(NationalCatalogProduct)
class NationalCatalogProductAdmin(admin.ModelAdmin):
    list_display = (
        'good_id', 'gtin', 'name', 'brand_name', 'product_group_name',
        'card_state', 'state_condition', 'create_date', 'ready_badge', 'synced_at',
    )
    list_filter = ('card_state', 'state_condition', 'product_group')
    search_fields = ('good_id', 'gtin', 'name', 'brand_name')
    readonly_fields = ('good_id', 'etag', 'create_date', 'raw_data', 'synced_at')
    date_hierarchy = 'create_date'
    list_per_page = 50
    ordering = ('-synced_at', 'good_id')

    @admin.display(boolean=True, description='Готов к производству')
    def ready_badge(self, obj):
        return obj.is_ready_for_production
