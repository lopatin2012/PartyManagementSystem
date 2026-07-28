# app_cz/serializers.py

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


class UIPDetailSerializer(serializers.ModelSerializer):
    """Детальный сериализатор с полной информацией о производстве."""

    # Статусы только словами.
    status = serializers.CharField(source='get_status_display', read_only=True)
    number_type_display = serializers.CharField(source='get_number_type_display', read_only=True)

    # Продукт.
    product_name = serializers.CharField(source='product_sku.name', read_only=True)
    gtin = serializers.CharField(source='product_sku.product.packagings.first.gtin', read_only=True, allow_null=True)

    # Производственные партии (может быть несколько).
    production_parties = serializers.SerializerMethodField()

    # Суммарные данные.
    total_planned = serializers.IntegerField(source='planned_quantity', read_only=True)
    total_produced = serializers.IntegerField(source='produced_quantity', read_only=True)

    class Meta:
        model = UIP
        fields = [
            'number', # Номер.
            'status', # Текущий статус.
            'number_type_display', #
            'product_name', # Название продукта.
            'gtin', # Gtin продукта.
            'description', # Описание.
            'total_planned', # Всего по плану нужно сделать на производстве в штуках.
            'total_produced', # Всего было валидировано на производстве в штуках.
            'created_at', # Дата создания.
            'updated_at', # Дата обновления.
            'closed_at', # Дата закрытия.
            'archived_at', # Дата архивирования.
            'production_parties' # Производственные партии. Детальная информация.
        ]

    def get_production_parties(self, obj):
        """Возвращает список производственных партий с детальной информацией."""
        parties = obj.production_parties.select_related(
            'factory', 'workshop', 'line'
        ).order_by('-created_at')

        result = []
        for party in parties:
            result.append({
                'internal_number': party.production_party, # Внутренний производственный номер.
                'external_task_number': party.external_number_task, # Номер внешнего задания.
                'factory_name': party.factory.name if party.factory else None, # Наименование завода.
                'workshop_name': party.workshop.name if party.workshop else None, # Наименование цеха.
                'line_name': party.line.name if party.line else None, # Наименование линии.
                'planned_quantity': party.planned_quantity, # План в штуках.
                'produced_quantity': party.produced_quantity, # План валидированных в штуках.
                'production_start': party.production_datetime_start, # Дата запуска.
                'production_end': party.production_datetime_end, # Дата окончания.
                'marking_datetime': party.marking_datetime, # Дата маркировки.
                'expiration_datetime': party.expiration_datetime, # Срок годности.
                'created_at': party.created_at # Дата создания производственной партии.
            })
        return result


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
        max_length=100
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
