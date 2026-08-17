from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import UIP, UIPStatusLog, ProductionParty, PartyStatusChoices


# ==========================================
# Вспомогательные функции для отображения.
# ==========================================
@admin.display(description='Статус УИП')
def status_colored(obj):
    """Отображает статус УИП с цветовой индикацией."""
    colors = {
        PartyStatusChoices.DRAFT: '#6c757d',
        PartyStatusChoices.RESERVED_CZ: '#FABB1C',
        PartyStatusChoices.RESERVED_LOCAL: '#FABB1C',
        PartyStatusChoices.REGISTERED: '#BBCF32',
        PartyStatusChoices.CLOSED: '#3D52A1',
        PartyStatusChoices.DELETED: '#58135E',
        PartyStatusChoices.ARCHIVED: '#302F80',
    }
    color = colors.get(obj.status, '#FFFFFF')
    return format_html(
        '<span style="background-color: {}; color: white; padding: 3px 8px; '
        'border-radius: 4px; font-size: 0.85rem; font-weight: 600;">{}</span>',
        color,
        obj.get_status_display()
    )


def render_plan_fact(planned: int, produced: int):
    """
    Универсальный рендер 'План/Факт' с мини-прогрессбаром.
    """
    # Если плана нет — возвращаем статичный HTML через mark_safe.
    if planned == 0:
        return mark_safe('<span style="color: #999;">—</span>')

    percentage = min((produced / planned) * 100, 100)

    # Цвет в зависимости от процента выполнения.
    if percentage >= 100:
        color = '#BBCF32'  # Зелёный — план выполнен.
    elif percentage >= 50:
        color = '#FABB1C'  # Жёлтый — в процессе.
    else:
        color = '#ff6b7a'  # Красный — низкий процент.

    return format_html(
        '<div style="min-width: 90px;">'
        '<div style="font-weight: 600;">{} <span style="color: #999; font-weight: 400;">/ {}</span></div>'
        '<div style="background: rgba(255,255,255,0.15); border-radius: 3px; height: 4px; margin-top: 3px;">'
        '<div style="background: {}; width: {}%; height: 100%; border-radius: 3px;"></div>'
        '</div></div>',
        produced, planned, color, percentage
    )


# ==========================================
# Inline для производственных партий внутри УИП.
# ==========================================
class ProductionPartyInline(admin.TabularInline):
    """Позволяет создавать и редактировать производственные партии прямо в карточке УИП."""
    model = ProductionParty
    extra = 0
    fields = (
        'production_party', 'line', 'workshop_name', 'factory_name',
        'planned_quantity', 'produced_quantity',
        'production_datetime_start', 'production_datetime_end'
    )
    readonly_fields = ('workshop_name', 'factory_name', 'created_at', 'updated_at')
    autocomplete_fields = ('line',)
    show_change_link = True

    @admin.display(description='Цех')
    def workshop_name(self, obj):
        """Получает название цеха через линию."""
        return obj.line.workshop.name if obj.line and obj.line.workshop else '—'

    @admin.display(description='Завод')
    def factory_name(self, obj):
        """Получает название завода через линию и цех."""
        if obj.line and obj.line.workshop and obj.line.workshop.factory:
            return obj.line.workshop.factory.name
        return '—'


# ==========================================
# Inline истории статусов (только чтение).
# ==========================================
class UIPStatusLogInline(admin.TabularInline):
    model = UIPStatusLog
    extra = 0
    fields = ('from_status', 'to_status', 'source', 'note', 'changed_by', 'created_at')
    readonly_fields = ('from_status', 'to_status', 'source', 'note', 'changed_by', 'created_at')
    ordering = ('-created_at',)
    verbose_name = 'История статуса'
    verbose_name_plural = 'История изменений статуса'

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ==========================================
# Админка для УИП.
# ==========================================
@admin.register(UIP)
class UIPAdmin(admin.ModelAdmin):
    list_display = (
        'number', 'product_sku', status_colored, 'desync_indicator',
        'planned_and_produced',
        'created_at'
    )
    list_filter = ('status', 'product_sku__product__group', 'product_sku')
    search_fields = ('number', 'product_sku__article', 'product_sku__product__name')
    autocomplete_fields = ('product_sku',)
    ordering = ('-created_at',)
    list_per_page = 25

    readonly_fields = ('created_at', 'updated_at', 'closed_at', 'archived_at')

    fieldsets = (
        ('Основная информация', {
            'fields': ('product_sku', 'number', 'status')
        }),
        ('Количества', {
            'fields': ('planned_quantity', 'produced_quantity')
        }),
        ('Дополнительно', {
            'fields': ('description', 'is_desync'),
            'classes': ('collapse',)
        }),
        ('Аудит', {
            'fields': ('created_at', 'updated_at', 'closed_at', 'archived_at'),
            'classes': ('collapse',)
        }),
    )

    inlines = [ProductionPartyInline, UIPStatusLogInline]

    @admin.display(description='План/Факт')
    def planned_and_produced(self, obj):
        """План/Факт с мини-прогрессбаром."""
        return render_plan_fact(obj.planned_quantity, obj.produced_quantity)

    @admin.display(description='Рассинхрон')
    def desync_indicator(self, obj):
        """Индикатор рассинхрона данных с ЧЗ."""
        if obj.is_desync:
            return mark_safe(
                '<span style="color: #870000; font-weight: 600;" '
                'title="УИП зарегистрирован/закрыт/в архиве, но числится в резерве ЧЗ">'
                '+</span>'
            )
        return mark_safe('<span style="color: #999;">—</span>')

    def save_model(self, request, obj, form, change):
        """Логируем создание и ручную смену статуса через админку."""
        user = request.user if request.user.is_authenticated else None

        if change:
            old_status = UIP.objects.filter(pk=obj.pk).values_list('status', flat=True).first()
            super().save_model(request, obj, form, change)
            if old_status and old_status != obj.status:
                UIPStatusLog.objects.create(
                    uip=obj,
                    from_status=old_status,
                    to_status=obj.status,
                    source='admin',
                    note='Статус изменён через админку',
                    changed_by=user,
                )
        else:
            super().save_model(request, obj, form, change)
            UIPStatusLog.objects.create(
                uip=obj,
                from_status=None,
                to_status=obj.status,
                source='admin',
                note='УИП создан через админку',
                changed_by=user,
            )


# ==========================================
# Админка для производственных партий.
# ==========================================
@admin.register(ProductionParty)
class ProductionPartyAdmin(admin.ModelAdmin):
    list_display = (
        'production_party',
        'line', 'workshop_name', 'factory_name',
        'planned_and_produced',
        'status',
        'sync_status',
        'last_sync_at',
        'production_datetime_start'
    )
    list_filter = (
        'line__workshop__factory',
        'line__workshop',
        'line',
        'uip__status',
        'uip__product_sku__product__group',
        'status',
        'sync_status',
        'is_external',
        'production_datetime_start'
    )
    search_fields = (
        'production_party', 'external_number_task',
        'uip__number', 'uip__product_sku__article'
    )
    autocomplete_fields = ('uip', 'line')
    ordering = ('-created_at',)
    list_per_page = 25

    readonly_fields = (
        'created_at', 'updated_at', 'workshop_name', 'factory_name',
        'last_sync_at', 'last_sync_message'
    )

    fieldsets = (
        ('Основная информация', {
            'fields': ('uip', 'production_party', 'external_number_task', 'status')
        }),
        ('Место производства', {
            'fields': ('line', 'workshop_name', 'factory_name')
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
        ('Синхронизация', {
            'fields': ('is_external', 'sync_status', 'last_sync_at', 'last_sync_message'),
            'classes': ('collapse',)
        }),
        ('Аудит', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Номер УИП')
    def uip_number(self, obj):
        """Показывает номер УИП с ссылкой."""
        from django.utils.html import format_html
        if not obj.uip:
            return '—'
        return format_html(
            '<a href="/admin/app_uip/uip/{}/change/">{}</a>',
            obj.uip.id,
            obj.uip.number
        )

    @admin.display(description='Цех')
    def workshop_name(self, obj):
        """Получает название цеха через линию."""
        return obj.line.workshop.name if obj.line and obj.line.workshop else '—'

    @admin.display(description='План/Факт')
    def planned_and_produced(self, obj):
        """План/Факт с мини-прогрессбаром."""
        return render_plan_fact(obj.planned_quantity, obj.produced_quantity)

    @admin.display(description='Завод')
    def factory_name(self, obj):
        """Получает название завода через линию и цех."""
        if obj.line and obj.line.workshop and obj.line.workshop.factory:
            return obj.line.workshop.factory.name
        return '—'

    @admin.display(description='Прогресс производства')
    def production_progress(self, obj):
        """Показывает прогресс производства в виде текста."""
        if obj.planned_quantity == 0:
            return '—'
        percentage = (obj.produced_quantity / obj.planned_quantity) * 100
        return f'{obj.produced_quantity} / {obj.planned_quantity} ({percentage:.1f}%)'
