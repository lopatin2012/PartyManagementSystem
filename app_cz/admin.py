from django.contrib import admin
from django.utils.html import format_html
from django.core.paginator import Paginator
from django.db import connection

from .models import CISCode, CISCodesStatusChoices, ProductionCodeStatusChoices


# ==========================================
# Кастомный пагинатор для больших таблиц.
# ==========================================
class LargeTablePaginator(Paginator):
    """
    Пагинатор, который не делает медленный COUNT(*) на больших таблицах.
    Вместо этого использует приближённое количество строк из статистики PostgreSQL (pg_class).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._count = None

    @property
    def count(self):
        if self._count is None:
            try:
                with connection.cursor() as cursor:
                    # Получаем примерное количество строк из системного каталога PostgreSQL
                    # Это работает в сотни раз быстрее, чем SELECT COUNT(*)
                    cursor.execute("""
                        SELECT reltuples::bigint 
                        FROM pg_class 
                        WHERE relname = 'app_cz_ciscode'
                    """)
                    row = cursor.fetchone()
                    # Если таблица пуста или статистика ещё не собрана, reltuples может быть None.
                    self._count = row[0] if row and row[0] is not None else 0
            except Exception:
                # В случае ошибки (например, таблица ещё не создана, или это не PostgreSQL)
                # безопасно откатываемся к стандартному поведению Django (хоть оно и медленнее).
                self._count = super().count

        return self._count


# ==========================================
# Вспомогательные функции для цветных бейджей.
# ==========================================
@admin.display(description='Статус ЧЗ')
def cz_status_badge(obj):
    colors = {
        CISCodesStatusChoices.EMITTED: '#6c757d',
        CISCodesStatusChoices.APPLIED: '#FABB1C',
        CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION: '#BBCF32',
        CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION: '#dc3545',
    }
    color = colors.get(obj.cz_status, '#6c757d')
    return format_html(
        '<span style="background-color: {}; color: white; padding: 3px 8px; '
        'border-radius: 4px; font-size: 0.8rem; font-weight: 600;">{}</span>',
        color,
        obj.get_cz_status_display()
    )


@admin.display(description='Статус производства')
def production_status_badge(obj):
    colors = {
        ProductionCodeStatusChoices.FREE: '#6c757d',
        ProductionCodeStatusChoices.PENDING: '#FABB1C',
        ProductionCodeStatusChoices.APPLIED: '#3D52A1',
        ProductionCodeStatusChoices.REJECTED: '#dc3545',
        ProductionCodeStatusChoices.SHIPPED: '#BBCF32',
    }
    color = colors.get(obj.production_status, '#6c757d')
    return format_html(
        '<span style="background-color: {}; color: white; padding: 3px 8px; '
        'border-radius: 4px; font-size: 0.8rem; font-weight: 600;">{}</span>',
        color,
        obj.get_production_status_display()
    )


@admin.display(description='УИП')
def uip_number(obj):
    return obj.production_party.uip.number if obj.production_party and obj.production_party.uip else '—'


@admin.display(description='GTIN')
def packaging_gtin(obj):
    return obj.product_packaging.gtin if obj.product_packaging else '—'


@admin.register(CISCode)
class CISCodeAdmin(admin.ModelAdmin):
    # Загружаем связанные объекты одним запросом
    list_select_related = (
        'production_party__uip',
        'product_packaging__product',
        'parent'
    )

    list_display = (
        'code_truncated',
        uip_number,
        packaging_gtin,
        'level',
        cz_status_badge,
        production_status_badge,
        'created_at'
    )

    # Используем кастомный пагинатор.
    paginator = LargeTablePaginator
    show_full_result_count = False  # Отключаем точный подсчёт.

    # Ограничиваем фильтры, чтобы не грузить миллионы записей.
    list_filter = (
        'cz_status',
        'production_status',
        'level',
    )

    # Поиск только по точному совпадению кода (использует индекс).
    search_fields = ('=code',)  # Знак '=' означает точное совпадение.

    autocomplete_fields = ('production_party', 'product_packaging', 'parent')
    ordering = ('-created_at',)
    list_per_page = 100  # 100 для удобства.

    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Основная информация', {
            'fields': ('code', 'level')
        }),
        ('Привязка к производству', {
            'fields': ('production_party', 'product_packaging', 'parent')
        }),
        ('Статусы', {
            'fields': ('cz_status', 'production_status')
        }),
        ('Аудит', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Код маркировки')
    def code_truncated(self, obj):
        code = obj.code
        if len(code) > 30:
            return format_html('<span title="{}">{}</span>', code, f'{code[:27]}...')
        return code

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if obj:
            readonly.append('code')
            readonly.append('production_party')
            readonly.append('product_packaging')
        return readonly

    def get_queryset(self, request):
        """Оптимизируем запрос, добавляя ограничения по дате по умолчанию."""
        qs = super().get_queryset(request)
        # По умолчанию показываем только последние 30 дней
        # Это ускоряет загрузку страницы в разы
        if not request.GET.get('created_at__gte'):
            from django.utils import timezone
            from datetime import timedelta
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=30))
        return qs


# ==========================================
# Массовые действия.
# ==========================================
@admin.action(description='Отметить выбранные коды как "Отбракован" (REJECTED)')
def mark_as_rejected(modeladmin, request, queryset):
    updated = queryset.update(production_status=ProductionCodeStatusChoices.REJECTED)
    modeladmin.message_user(request, f'Успешно изменено статусов: {updated}')


@admin.action(description='Отметить выбранные коды как "Нанесён" (APPLIED)')
def mark_as_applied(modeladmin, request, queryset):
    updated = queryset.update(
        production_status=ProductionCodeStatusChoices.APPLIED,
        cz_status=CISCodesStatusChoices.APPLIED
    )
    modeladmin.message_user(request, f'Успешно изменено статусов: {updated}')


CISCodeAdmin.actions = [mark_as_rejected, mark_as_applied]
