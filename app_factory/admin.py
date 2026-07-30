from django.contrib import admin
from app_factory.models import (
    Factory,
    Workshop,
    Line,
    Product,
    ProductPackaging,
    ProductSKU,
    ProductProductionLocation
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
    list_display = ('name', is_active_display)
    list_filter = ('is_active',)
    search_fields = ('name',)
    ordering = ('name',)
    list_per_page = 25


@admin.register(Workshop)
class WorkshopAdmin(admin.ModelAdmin):
    list_display = ('name', 'factory', is_active_display)
    list_filter = ('factory', 'is_active')
    autocomplete_fields = ('factory', )
    search_fields = ('name', 'factory__name')
    ordering = ('factory', 'name')
    list_per_page = 25


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = ('name', 'workshop', 'factory_name', is_active_display)
    list_filter = ('workshop__factory', 'workshop', 'is_active')
    autocomplete_fields = ('workshop', )
    search_fields = ('name', 'workshop__name', 'workshop__factory__name')
    ordering = ('workshop', 'name')
    list_per_page = 25

    @admin.display(description='Завод')
    def factory_name(self, obj):
        return obj.workshop.factory.name


# ==========================================
# Админки для продукции и упаковок.
# ==========================================
@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'group', 'item_condition', 'card_status', is_active_display)
    list_filter = ('group', 'item_condition', 'card_status', 'is_active')
    search_fields = ('name',)
    ordering = ('group', 'name')
    list_per_page = 25


@admin.register(ProductPackaging)
class ProductPackagingAdmin(admin.ModelAdmin):
    list_display = ('product__name', 'product', 'level', 'gtin', 'quantity_inside', is_active_display)
    list_filter = ('level', 'is_active', 'product__group')
    autocomplete_fields = ('product', )
    search_fields = ('gtin', 'product__name')
    ordering = ('product', 'level')
    list_per_page = 25

    # Делаем GTIN читаемым, но защищаем от случайного изменения в списке.
    readonly_fields = ('gtin',)


@admin.register(ProductSKU)
class ProductSKUAdmin(admin.ModelAdmin):
    list_display = ('sku_code', 'product__name', 'product', is_active_display)
    list_filter = ('is_active', 'product__group')
    autocomplete_fields = ('product', )
    search_fields = ('sku_code', 'product__name')
    ordering = ('product', 'sku_code')
    list_per_page = 50


# ==========================================
# Админки для привязок.
# ==========================================
@admin.register(ProductProductionLocation)
class ProductProductionLocationAdmin(admin.ModelAdmin):
    list_display = ('product_sku', 'line', 'workshop_name', 'factory_name', is_active_display)
    list_filter = ('is_active', 'line__workshop__factory', 'line__workshop', 'line')
    autocomplete_fields = ('product_sku', 'line')
    search_fields = (
        'product_sku__sku_code',
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
