# app_factory/admin.py

from django.contrib import admin
from django.core.exceptions import ValidationError

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
    autocomplete_fields = ('factory',)
    search_fields = ('name', 'factory__name')
    ordering = ('factory', 'name')
    list_per_page = 25


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = ('name', 'workshop', 'factory_name', is_active_display)
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
    list_display = ('name', 'group', 'item_condition', 'card_status', is_active_display)
    list_filter = ('group', 'item_condition', 'card_status', 'is_active')
    search_fields = ('name',)
    ordering = ('group', 'name')
    list_per_page = 25
    inlines = [ProductPackagingInline]

    def save_formset(self, request, form, formset, change):
        """Валидация: проверяем, что нет дубликатов уровней упаковки."""
        instances = formset.save(commit=False)

        # Проверяем дубликаты уровней
        levels_seen = {}
        for idx, instance in enumerate(instances):
            if instance.level in levels_seen:
                # Добавляем ошибку к конкретной форме
                form_idx = levels_seen[instance.level]
                formset.forms[form_idx].add_error(
                    'level',
                    f'Уровень "{instance.get_level_display()}" уже используется в другой упаковке'
                )
                # Отменяем сохранение
                raise ValidationError(
                    f'Нельзя создать две упаковки с уровнем "{instance.get_level_display()}" '
                    f'для одного продукта.'
                )
            levels_seen[instance.level] = idx

        # Сохраняем все экземпляры
        for instance in instances:
            instance.save()

        # Удаляем помеченные на удаление
        for obj in formset.deleted_objects:
            obj.delete()


@admin.register(ProductPackaging)
class ProductPackagingAdmin(admin.ModelAdmin):
    list_display = ('product__name', 'product', 'level', 'gtin', 'quantity_inside', is_active_display)
    list_filter = ('level', 'is_active', 'product__group')
    autocomplete_fields = ('product',)
    search_fields = ('gtin', 'product__name')
    ordering = ('product', 'level')
    list_per_page = 25


@admin.register(ProductSKU)
class ProductSKUAdmin(admin.ModelAdmin):
    list_display = ('sku_code', 'product__name', 'product', is_active_display)
    list_filter = ('is_active', 'product__group')
    autocomplete_fields = ('product',)
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
