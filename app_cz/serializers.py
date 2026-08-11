# app_cz/serializers.py

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from app_cz.models import CISCode
from app_cz.enums import TypeProduct
from app_factory.models import PackagingLevelChoices
from app_uip.models import UIP, ProductionParty


class CISCodeSerializer(serializers.ModelSerializer):
    """Сериализатор для кодов маркировки."""
    uip_number = serializers.CharField(source='production_party.uip.number', read_only=True)
    gtin = serializers.CharField(source='product_packaging.gtin', read_only=True)
    cz_status_display = serializers.CharField(source='get_cz_status_display', read_only=True)
    production_status_display = serializers.CharField(source='get_production_status_display', read_only=True)

    class Meta:
        model = CISCode
        fields = [
            'id', 'code', 'uip_number', 'gtin', 'level',
            'cz_status', 'cz_status_display',
            'production_status', 'production_status_display',
            'created_at', 'updated_at'
        ]


class UIPSerializer(serializers.ModelSerializer):
    """Сериализатор для УИП."""
    product_name = serializers.CharField(source='product_sku.name', read_only=True)
    status = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = UIP
        fields = [
            'number',
            'status',
            'product_name',
            'planned_quantity',
            'produced_quantity',
            'created_at'
        ]


class ProductionPartySerializer(serializers.ModelSerializer):
    """Сериализатор для производственных партий."""
    uip_number = serializers.CharField(source='uip.number', read_only=True)
    factory_name = serializers.CharField(source='factory.name', read_only=True, allow_null=True)
    line_name = serializers.CharField(source='line.name', read_only=True, allow_null=True)

    class Meta:
        model = ProductionParty
        fields = [
            'id', 'production_party', 'uip_number',
            'factory_name', 'line_name',
            'planned_quantity', 'produced_quantity',
            'production_datetime_start', 'production_datetime_end',
            'created_at', 'updated_at'
        ]


class PartyInfoItemSerializer(serializers.Serializer):
    gtin = serializers.CharField(max_length=14, min_length=14)
    productionDate = serializers.CharField()  # Формат ISO 8601, например "2026-07-21T00:00:00.000Z"
    count = serializers.IntegerField(min_value=1, max_value=50)


class GeneratePartySerializer(serializers.Serializer):
    product_group = serializers.ChoiceField(choices=[choice.value for choice in TypeProduct])
    party_info_list = PartyInfoItemSerializer(many=True, min_length=1, max_length=50)


class ReservePartySerializer(serializers.Serializer):
    product_group = serializers.ChoiceField(choices=[
        choice.value
        for choice in TypeProduct
    ])
    party_numbers = serializers.ListField(
        child=serializers.CharField(
            min_length=21, max_length=32
        ),
        min_length=1,
        max_length=50
    )


class ClosePartySerializer(serializers.Serializer):
    cis = serializers.CharField(min_length=31, max_length=50)
    batch_number = serializers.CharField(min_length=21, max_length=32)


class SyncCodesTaskSerializer(serializers.Serializer):
    """Для синхронизации кодов cis."""

    url = serializers.URLField(
        help_text="Базовый URL API рабочего проекта (например, http://192.168.1.100:8000)"
    )
    task_id = serializers.UUIDField(
        help_text="UUID задания (Task)"
    )
    production_party_id = serializers.UUIDField(
        help_text="UUID производственной партии"
    )
    packaging_id = serializers.UUIDField(
        help_text="UUID упаковки продукта (GTIN)"
    )
    token = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Токен авторизации (опционально)"
    )
    level = serializers.IntegerField(
        required=False,
        default=PackagingLevelChoices.UNIT,
        help_text="Уровень упаковки (по умолчанию UNIT)"
    )


class CISCodeDetailSerializer(serializers.ModelSerializer):
    """Детальный сериализатор для просмотра полной информации о коде (только строки)."""

    # Переопределяем поля для возврата текстового представления.
    level = serializers.CharField(source='get_level_display', read_only=True)
    cz_status = serializers.CharField(source='get_cz_status_display', read_only=True)
    production_status = serializers.CharField(source='get_production_status_display', read_only=True)

    # Данные УИП и Партии.
    uip_number = serializers.CharField(source='production_party.uip.number', read_only=True)
    external_task_number = serializers.CharField(source='production_party.external_number_task', read_only=True,
                                                 allow_null=True)
    internal_party_number = serializers.CharField(source='production_party.production_party', read_only=True)
    marking_datetime = serializers.DateTimeField(source='production_party.marking_datetime', read_only=True,
                                                 allow_null=True)

    # Данные о месте производства.
    factory_name = serializers.CharField(source='production_party.factory.name', read_only=True, allow_null=True)
    workshop_name = serializers.CharField(source='production_party.workshop.name', read_only=True, allow_null=True)
    line_name = serializers.CharField(source='production_party.line.name', read_only=True, allow_null=True)

    # Данные об упаковке и продукте.
    gtin = serializers.CharField(source='product_packaging.gtin', read_only=True)
    product_name = serializers.CharField(source='product_packaging.product.name', read_only=True)

    class Meta:
        model = CISCode
        fields = [
            'code',  # Сам код.
            'level',  # Уровень вложенности.
            'cz_status',  # Статус в ЧЗ.
            'production_status',  # Статус на производстве.
            'created_at',  # Дата и время добавления кода в локальную базу данных.
            'marking_datetime',  # Дата маркировки.
            'uip_number',  # Номер УИП.
            'external_task_number',  # Номер задания во внешней системе.
            'internal_party_number',  # Внутренний номер партии.
            'factory_name',  # Название завода.
            'workshop_name',  # Название цеха.
            'line_name',  # Название линии.
            'gtin',  # Gtin кода.
            'product_name'  # Наименование продукта.
        ]


class ReservedPartyProductSerializer(serializers.Serializer):
    """Вложенный сериализатор для продукта."""
    id = serializers.UUIDField()
    gtin = serializers.CharField()
    name = serializers.CharField()
    articles = serializers.ListField(child=serializers.CharField())


class ReservedPartyWorkshopSerializer(serializers.Serializer):
    """Вложенный сериализатор для цеха."""
    id = serializers.UUIDField()
    name = serializers.CharField()


class ReservedPartyLineSerializer(serializers.Serializer):
    """Вложенный сериализатор для линии."""
    id = serializers.UUIDField()
    name = serializers.CharField()


class ReservedPartyListSerializer(serializers.ModelSerializer):
    """Сериализатор для списка зарезервированных партий."""

    party_number = serializers.CharField(source='number')
    party_number_type = serializers.CharField(source='get_number_type_display')
    status = serializers.CharField(source='get_status_display')

    product = serializers.SerializerMethodField()
    workshop = serializers.SerializerMethodField()
    lines = serializers.SerializerMethodField()
    marking_date = serializers.SerializerMethodField()

    class Meta:
        model = UIP
        fields = [
            'id',
            'party_number',
            'party_number_type',
            'status',
            'product',
            'workshop',
            'lines',
            'marking_date',
            'created_at',
            'updated_at'
        ]

    @extend_schema_field(serializers.CharField)
    def get_product(self, obj):
        """Продукт с GTIN и списком артикулов."""
        sku = obj.product_sku
        if not sku:
            return None

        product = sku.product
        articles = list(
            product.skus.filter(is_active=True).values_list('article', flat=True)
        )

        # GTIN берём из первой активной упаковки.
        gtin = product.packagings.filter(is_active=True).values_list('gtin', flat=True).first()

        return {
            'id': str(product.id),
            'gtin': gtin or '',
            'name': product.name,
            'articles': articles
        }

    @extend_schema_field(serializers.CharField)
    def get_workshop(self, obj):
        """Цех из первой производственной партии."""
        party = obj.production_parties.select_related('workshop').first()
        if party and party.workshop:
            return {
                'id': str(party.workshop.id),
                'name': party.workshop.name
            }
        return None

    @extend_schema_field(serializers.ListField)
    def get_lines(self, obj):
        """Все уникальные линии из производственных партий."""
        parties = obj.production_parties.select_related('line').all()
        lines_map = {}
        for party in parties:
            if party.line and party.line.id not in lines_map:
                lines_map[party.line.id] = {
                    'id': str(party.line.id),
                    'name': party.line.name
                }
        return list(lines_map.values())

    @extend_schema_field(serializers.DateField)
    def get_marking_date(self, obj):
        """Дата маркировки из первой партии (формат dd.mm.yyyy)."""
        party = obj.production_parties.first()
        if party and party.marking_datetime:
            return party.marking_datetime.strftime('%d.%m.%Y')
        return None


class ReservedPartyDetailSerializer(serializers.ModelSerializer):
    """
    Детальный сериализатор УИП с полной информацией:
    - продукт, цех, линии
    - производственные партии
    - статистика кодов и уровень агрегации
    """

    # === Основная информация ===
    party_number = serializers.CharField(source='number')
    status = serializers.CharField(source='get_status_display')
    status_code = serializers.CharField(source='status')
    number_type_display = serializers.CharField(source='get_number_type_display')
    description = serializers.CharField(read_only=True)

    # === Продукт ===
    product = serializers.SerializerMethodField()

    # === Место производства ===
    workshop = serializers.SerializerMethodField()
    lines = serializers.SerializerMethodField()
    marking_date = serializers.SerializerMethodField()

    # === Суммарные данные ===
    total_planned = serializers.IntegerField(source='planned_quantity', read_only=True)
    total_produced = serializers.IntegerField(source='produced_quantity', read_only=True)

    # === Статистика кодов ===
    level_aggregation = serializers.SerializerMethodField()
    codes_total = serializers.SerializerMethodField()

    # === Производственные партии ===
    production_parties = serializers.SerializerMethodField()

    class Meta:
        model = UIP
        fields = [
            'id',
            'party_number',
            'status',
            'status_code',
            'number_type_display',
            'description',
            'product',
            'workshop',
            'lines',
            'marking_date',
            'total_planned',
            'total_produced',
            'level_aggregation',
            'codes_total',
            'production_parties',
            'created_at',
            'updated_at',
            'closed_at',
            'archived_at'
        ]

    # === Методы для получения данных ===

    @extend_schema_field(serializers.CharField())
    def get_product(self, obj):
        """Продукт с GTIN и списком артикулов."""
        sku = obj.product_sku
        if not sku:
            return None

        product = sku.product
        articles = list(
            product.skus.filter(is_active=True).values_list('article', flat=True)
        )
        gtin = product.packagings.filter(is_active=True).values_list('gtin', flat=True).first()

        return {
            'id': str(product.id),
            'gtin': gtin or '',
            'name': product.name,
            'articles': articles
        }

    @extend_schema_field(serializers.CharField())
    def get_workshop(self, obj):
        """Цех из первой производственной партии."""
        party = obj.production_parties.select_related('workshop').first()
        if party and party.workshop:
            return {
                'id': str(party.workshop.id),
                'name': party.workshop.name
            }
        return None

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_lines(self, obj):
        """Все уникальные линии из производственных партий."""
        parties = obj.production_parties.select_related('line').all()
        lines_map = {}
        for party in parties:
            if party.line and party.line.id not in lines_map:
                lines_map[party.line.id] = {
                    'id': str(party.line.id),
                    'name': party.line.name
                }
        return list(lines_map.values())

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_marking_date(self, obj):
        """Дата маркировки из первой партии (формат dd.mm.yyyy)."""
        party = obj.production_parties.first()
        if party and party.marking_datetime:
            return party.marking_datetime.strftime('%d.%m.%Y')
        return None

    def _get_all_codes(self, obj):
        """Кэшированный запрос всех кодов для УИП."""
        if not hasattr(obj, '_cached_codes'):
            obj._cached_codes = list(
                CISCode.objects.filter(
                    production_party__uip=obj
                ).values('code', 'level')
            )
        return obj._cached_codes

    @extend_schema_field(serializers.CharField())
    def get_level_aggregation(self, obj):
        """Максимальный уровень агрегации среди всех кодов."""
        codes = self._get_all_codes(obj)
        if not codes:
            return 0
        return max(c['level'] for c in codes)

    @extend_schema_field(serializers.IntegerField())
    def get_codes_total(self, obj):
        """Количество кодов по уровням."""
        codes = self._get_all_codes(obj)
        totals = {}
        for c in codes:
            key = f"level{c['level']}_total"
            totals[key] = totals.get(key, 0) + 1
        return totals

    @extend_schema_field(serializers.ListField())
    def get_production_parties(self, obj):
        """Детальная информация о всех производственных партиях."""
        parties = obj.production_parties.select_related(
            'factory', 'workshop', 'line'
        ).order_by('-created_at')

        result = []
        for party in parties:
            result.append({
                'internal_number': party.production_party,  # Внутренний производственный номер.
                'external_task_number': party.external_number_task,  # Номер внешнего задания.
                'factory_name': party.factory.name if party.factory else None,  # Наименование завода.
                'workshop_name': party.workshop.name if party.workshop else None,  # Наименование цеха.
                'line_name': party.line.name if party.line else None,  # Наименование линии.
                'planned_quantity': party.planned_quantity,  # План в штуках.
                'produced_quantity': party.produced_quantity,  # План валидированных в штуках.
                'production_start': party.production_datetime_start,  # Дата запуска.
                'production_end': party.production_datetime_end,  # Дата окончания.
                'marking_datetime': party.marking_datetime,  # Дата маркировки.
                'expiration_datetime': party.expiration_datetime,  # Срок годности.
                'created_at': party.created_at  # Дата создания производственной партии.
            })
        return result


class CodeTreeNodeSerializer(serializers.Serializer):
    """Рекурсивный сериализатор для дерева кодов."""
    code = serializers.CharField()
    level = serializers.CharField(source='get_level_display')
    children = serializers.SerializerMethodField()

    def get_children(self, obj):
        """Рекурсивно строит дерево вложенных кодов."""
        children = obj.children.all().select_related('product_packaging')
        if not children:
            return []
        return CodeTreeNodeSerializer(children, many=True).data


class ReservedPartyCodesSerializer(serializers.ModelSerializer):
    """Сериализатор для дерева кодов УИП."""

    party_number = serializers.CharField(source='number')
    status = serializers.CharField(source='get_status_display')
    level_aggregation = serializers.SerializerMethodField()
    codes = serializers.SerializerMethodField()

    class Meta:
        model = UIP
        fields = [
            'id',
            'party_number',
            'status',
            'level_aggregation',
            'codes'
        ]

    @extend_schema_field(serializers.CharField())
    def get_level_aggregation(self, obj):
        from django.db import models

        codes = CISCode.objects.filter(production_party__uip=obj)
        max_level = codes.aggregate(max_level=models.Max('level'))['max_level']
        return max_level or 0

    @extend_schema_field(serializers.CharField())
    def get_codes(self, obj):
        """
        Строит дерево кодов, сгруппированное по уровням.
        Возвращает структуру: { "level3": [...], "level2": [...], "level1": [...] }
        """
        from django.db import models as db_models

        all_codes = CISCode.objects.filter(
            production_party__uip=obj
        ).select_related('product_packaging').prefetch_related('children')

        # Находим максимальный уровень
        max_level = all_codes.aggregate(m=db_models.Max('level'))['m'] or 0

        if max_level == 0:
            return {}

        # Группируем коды по уровням (от высшего к низшему)
        result = {}
        for lvl in range(max_level, 0, -1):
            level_codes = all_codes.filter(level=lvl)
            if level_codes.exists():
                level_name = f"level{lvl}"
                result[level_name] = CodeTreeNodeSerializer(
                    level_codes, many=True
                ).data

        return result

class GenerateUIPSerializer(serializers.Serializer):
    """Генерация одного УИП по запросу извне."""
    article = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
        help_text='Артикул продукта'
    )
    gtin = serializers.CharField(
        max_length=14, required=False, allow_blank=True,
        help_text='GTIN потребительской упаковки'
    )
    production_date = serializers.DateField(
        help_text='Дата производства (ГГГГ-ММ-ДД)'
    )
    mode = serializers.ChoiceField(
        choices=['local', 'cz'], default='local',
        help_text='local — согласованный формат, cz — формирует Честный Знак'
    )
    skip_cz = serializers.BooleanField(
        default=False,
        help_text='Создать черновик'
    )

    def validate(self, attrs):
        if not attrs.get('article') and not attrs.get('gtin'):
            raise serializers.ValidationError('Укажите article или gtin.')
        return attrs

class ReserveDraftUIPSerializer(serializers.Serializer):
    """Запрос резервирования черновой УИП в Честном Знаке."""
    uip_id = serializers.UUIDField(
        help_text='UUID черновой УИП, который нужно зарезервировать'
    )
