from django.contrib import admin
from django.utils.html import format_html, mark_safe
from django.core.paginator import Paginator
from django.db import connections

from app_cz.models import (
    CISCode, CISCodeArchive, CISCodesStatusChoices, ProductionCodeStatusChoices, SUZAccount
)


# ==========================================
# Кастомный пагинатор для больших таблиц.
# ==========================================
class LargeTablePaginator(Paginator):
    """
    Пагинатор без медленного COUNT(*) на больших таблицах.

    Django-админка ВСЕГДА запрашивает `paginator.count` (для числа страниц), а
    на таблицах с миллионами строк `SELECT COUNT(*)` очень дорог. Здесь число
    строк берётся приблизительно из статистики PostgreSQL (`pg_class.reltuples`)
    для таблицы и БД переданного queryset (важно для архива в отдельной БД).
    При неудаче — откат к обычному COUNT.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._count = None

    @property
    def count(self):
        if self._count is None:
            self._count = self._estimate_count()
        return self._count

    def _estimate_count(self):
        queryset = self.object_list
        model = getattr(queryset, 'model', None)
        if model is None:
            return super().count
        table = model._meta.db_table
        db = getattr(queryset, 'db', None) or 'default'
        try:
            with connections[db].cursor() as cursor:
                cursor.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                    [table],
                )
                row = cursor.fetchone()
            if row and row[0] is not None and row[0] >= 0:
                return row[0]
        except Exception:
            # Таблица ещё не создана / не PostgreSQL — откат к обычному COUNT.
            pass
        return super().count


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
    # Сортировка по PK (-id): у created_at в рабочей таблице НЕТ индекса, и
    # ORDER BY -created_at заставляет PostgreSQL сортировать миллионы строк.
    # -id использует индекс первичного ключа и совпадает с порядком создания.
    ordering = ('-id',)
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


@admin.register(CISCodeArchive)
class CISCodeArchiveAdmin(admin.ModelAdmin):
    """Архив кодов маркировки — только просмотр (историчность данных)."""

    # Архив живёт в отдельной базе.
    def get_queryset(self, request):
        return CISCodeArchive.objects.using('archive').all()

    list_display = (
        'code_truncated',
        'uip_number',
        'gtin',
        'level',
        cz_status_badge,
        production_status_badge,
        'created_at',
        'archived_at',
    )
    list_filter = (
        'cz_status',
        'production_status',
        'level',
    )
    search_fields = ('=code',)
    ordering = ('-created_at',)
    list_per_page = 100

    # Архив в разы больше рабочей таблицы (не чистится). Без этого админка
    # делала бы COUNT(*) по всей таблице и медленно открывалась. Пагинатор
    # берёт приблизительное число строк из pg_class для БД 'archive'.
    paginator = LargeTablePaginator
    show_full_result_count = False

    readonly_fields = tuple(
        f.name for f in CISCodeArchive._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Код маркировки')
    def code_truncated(self, obj):
        code = obj.code
        if len(code) > 30:
            return format_html('<span title="{}">{}</span>', code, f'{code[:27]}...')
        return code

    @admin.display(description='УИП')
    def uip_number(self, obj):
        return obj.uip_number or '—'

    @admin.display(description='GTIN')
    def gtin(self, obj):
        return obj.gtin or '—'


@admin.register(SUZAccount)
class SUZAccountAdmin(admin.ModelAdmin):
    list_display = (
        'certificate_name',
        'inn',
        'status_badge',
        'oms_id',
        'token_status',
        'updated_at'
    )

    list_filter = ('is_active',)
    search_fields = ('certificate_name', 'serial_number', 'inn', 'oms_id')
    ordering = ('-is_active', '-updated_at')

    fieldsets = (
        ('Статус', {
            'fields': ('is_active',)
        }),
        ('Данные сертификата', {
            'fields': ('certificate_name', 'serial_number', 'inn', 'valid_from', 'valid_to')
        }),
        ('Параметры СУЗ', {
            'fields': ('oms_id', 'device_name', 'connection_identifier')
        }),
        ('Авторизация (обновляется автоматически)', {
            'fields': ('dynamic_token', 'token_expires_at'),
            'classes': ('collapse',),
            'description': 'Эти поля заполняются автоматически при успешном запросе токена из СУЗ.'
        }),
        ('Системная информация', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    readonly_fields = ('dynamic_token', 'token_expires_at', 'created_at', 'updated_at')

    @admin.display(description='Статус')
    def status_badge(self, obj):
        if obj.is_active:
            return mark_safe(
                '<span style="background-color: #BBCF32; color: #302F80; padding: 3px 8px; '
                'border-radius: 4px; font-weight: 600;">АКТИВНА</span>'
            )
        return mark_safe(
            '<span style="background-color: #6c757d; color: white; padding: 3px 8px; '
            'border-radius: 4px; font-weight: 600;">Неактивна</span>'
        )

    @admin.display(description='Токен')
    def token_status(self, obj):
        if obj.is_token_valid:
            return mark_safe(
                '<span style="color: #BBCF32; font-weight: 600;">Действует</span>'
            )
        return mark_safe(
            '<span style="color: #dc3545; font-weight: 600;">Отсутствует или истёк</span>'
        )
