from django.contrib import admin
from django.utils.html import format_html

from .models import UIP, ProductionParty, PartyStatusChoices


# ==========================================
# Вспомогательные функции для отображения.
# ==========================================
@admin.display(description='Статус УИП')
def status_colored(obj):
    """Отображает статус УИП с цветовой индикацией."""
    colors = {
        PartyStatusChoices.DRAFT: '#6c757d',  # Серый
        PartyStatusChoices.RESERVED_CZ: '#FABB1C',  # Желтый
        PartyStatusChoices.RESERVED_LOCAL: '#FABB1C',  # Желтый
        PartyStatusChoices.ACTIVE: '#BBCF32',  # Зеленый
        PartyStatusChoices.CLOSED: '#3D52A1',  # Синий
        PartyStatusChoices.ARCHIVE: '#302F80',  # Темно-синий
    }
    color = colors.get(obj.status, '#FFFFFF')
    return format_html(
        '<span style="background-color: {}; color: white; padding: 3px 8px; '
        'border-radius: 4px; font-size: 0.85rem; font-weight: 600;">{}</span>',
        color,
        obj.get_status_display()
    )


@admin.display(description='Прогресс производства')
def production_progress(obj):
    """Показывает прогресс производства в виде текста."""
    if obj.planned_quantity == 0:
        return '—'
    percentage = (obj.produced_quantity / obj.planned_quantity) * 100
    return f'{obj.produced_quantity} / {obj.planned_quantity} ({percentage:.1f}%)'


# ==========================================
# Inline для производственных партий внутри УИП.
# ==========================================
class ProductionPartyInline(admin.TabularInline):
    """Позволяет создавать и редактировать производственные партии прямо в карточке УИП."""
    model = ProductionParty
    extra = 0  # Не показывать пустые строки по умолчанию.
    fields = (
        'production_party', 'line', 'workshop', 'factory',
        'planned_quantity', 'produced_quantity',
        'production_datetime_start', 'production_datetime_end'
    )
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('line', 'workshop', 'factory')
    show_change_link = True  # Позволяет перейти на страницу редактирования партии.


# ==========================================
# Админка для УИП.
# ==========================================
@admin.register(UIP)
class UIPAdmin(admin.ModelAdmin):
    list_display = (
        'number', 'product_sku', status_colored, 'number_type',
        'planned_quantity', 'produced_quantity', 'created_at'
    )
    list_filter = ('status', 'number_type', 'product_sku__product__group', 'product_sku')
    search_fields = ('number', 'product_sku__sku_code', 'product_sku__name')
    autocomplete_fields = ('product_sku',)
    ordering = ('-created_at',)
    list_per_page = 25

    # Поля, доступные для редактирования.
    readonly_fields = ('created_at', 'updated_at', 'closed_at', 'archived_at')

    # Группировка полей на странице редактирования.
    fieldsets = (
        ('Основная информация', {
            'fields': ('product_sku', 'number', 'number_type', 'status')
        }),
        ('Количества', {
            'fields': ('planned_quantity', 'produced_quantity')
        }),
        ('Дополнительно', {
            'fields': ('description',),
            'classes': ('collapse',)  # Сворачиваемая секция.
        }),
        ('Аудит', {
            'fields': ('created_at', 'updated_at', 'closed_at', 'archived_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [ProductionPartyInline]


# ==========================================
# Админка для производственных партий.
# ==========================================
@admin.register(ProductionParty)
class ProductionPartyAdmin(admin.ModelAdmin):
    list_display = (
        'production_party', 'uip', 'line', 'factory_name',
        'planned_quantity', 'produced_quantity', production_progress,
        'production_datetime_start'
    )
    list_filter = (
        'factory', 'workshop', 'line',
        'uip__status', 'uip__product_sku__product__group'
    )
    search_fields = (
        'production_party', 'external_number_task',
        'uip__number', 'uip__product_sku__sku_code'
    )
    autocomplete_fields = ('uip', 'factory', 'workshop', 'line')
    ordering = ('-created_at',)
    list_per_page = 25

    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Основная информация', {
            'fields': ('uip', 'production_party', 'external_number_task')
        }),
        ('Место производства', {
            'fields': ('factory', 'workshop', 'line')
        }),
        ('Даты', {
            'fields': (
                'production_datetime_start', 'production_datetime_end',
                'marking_datetime', 'expiration_datetime'
            )
        }),
        ('Количества', {
            'fields': ('planned_quantity', 'produced_quantity')
        }),
        ('Аудит', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Завод')
    def factory_name(self, obj):
        return obj.factory.name if obj.factory else '—'
