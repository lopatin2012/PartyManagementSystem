# app_uip/serializers.py

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from app_cz.enums import TypeProduct

from app_uip.models import UIP, PartyStatusChoices


class UIPStatusSerializer(serializers.ModelSerializer):
    """Сериализатор для статуса одного УИП."""
    is_active = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    detail = serializers.SerializerMethodField()

    class Meta:
        model = UIP
        fields = [
            'number',
            'status',
            'status_display',
            'is_active',
            'detail'
        ]

    @extend_schema_field(serializers.BooleanField)
    def get_is_active(self, obj):
        """
        Определяет, можно ли использовать УИП.
        Действующие: reserved_cz, reserved_local, registered, closed, archived
        Недействующие: draft, deleted
        """
        return obj.status in UIP.ACTIVE_STATUSES

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_detail(self, obj):
        """Возвращает причину, почему УИП не найден/недействующий."""
        if obj.status == PartyStatusChoices.DRAFT:
            return "УИП находится в статусе черновика и не зарезервирован в ЧЗ"
        elif obj.status == PartyStatusChoices.DELETED:
            return "УИП удалён по истечению 30 дней"

        return None


class UIPBatchStatusSerializer(serializers.Serializer):
    """Сериализатор для проверки списка УИП."""
    numbers = serializers.ListField(
        child=serializers.CharField(max_length=32),
        min_length=1,
        max_length=100,
        help_text="Список номеров УИП для проверки"
    )


class UIPBatchResultSerializer(serializers.Serializer):
    """Результат проверки списка УИП."""
    total = serializers.IntegerField()
    found = serializers.IntegerField()
    not_found = serializers.ListField(child=serializers.CharField())
    details = UIPStatusSerializer(many=True)


class UIPActiveListSerializer(serializers.ModelSerializer):
    """Сериализатор для списка действующих УИП."""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    product_name = serializers.CharField(source='product_sku.name', read_only=True)
    product_article = serializers.CharField(source='product_sku.article', read_only=True)

    class Meta:
        model = UIP
        fields = [
            'id',
            'number',
            'gtin',
            'status',
            'status_display',
            'product_name',
            'product_article',
            'planned_quantity',
            'produced_quantity',
            'production_date',
            'reservation_date'
        ]


class UIPReserveItemSerializer(serializers.Serializer):
    """Один запрос резервирования УИП (генерация или своих номеров)."""
    # === Путь генерации нового УИП (через generate_uip). ===
    article = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
        help_text='Артикул продукта (для генерации)'
    )
    gtin = serializers.CharField(
        max_length=14, required=False, allow_blank=True,
        help_text='GTIN потребительской упаковки (для генерации)'
    )
    production_date = serializers.DateField(
        required=False,
        help_text='Дата производства (ГГГГ-ММ-ДД)'
    )
    mode = serializers.ChoiceField(
        choices=['local', 'cz'], default='local', required=False,
        help_text='local — согласованный формат, cz — формирует Честный Знак'
    )
    count = serializers.IntegerField(
        min_value=1, max_value=50, required=False, default=1,
        help_text='Сколько УИП сгенерировать (только для пути генерации)'
    )
    party = serializers.CharField(
        required=False, allow_blank=True,
        help_text='Номер партии (для local)'
    )
    target_status = serializers.CharField(
        required=False, allow_blank=True,
        help_text='Переопределить статус создаваемого УИП'
    )
    skip_cz = serializers.BooleanField(
        required=False, default=False,
        help_text='Создать черновик (только для local)'
    )

    # === Путь резервирования своих номеров. ===
    product_group = serializers.ChoiceField(
        choices=[choice.value for choice in TypeProduct],
        required=False,
        help_text='Товарная группа (для резервирования своих номеров)'
    )
    party_numbers = serializers.ListField(
        child=serializers.CharField(min_length=21, max_length=32),
        min_length=1, max_length=50, required=False,
        help_text='Список номеров УИП для резервирования'
    )

    def validate(self, attrs):
        has_generate = bool(attrs.get('article') or attrs.get('gtin'))
        has_reserve_own = bool(attrs.get('party_numbers'))

        if has_generate and has_reserve_own:
            raise serializers.ValidationError(
                'Нельзя одновременно указать параметры генерации и party_numbers.'
            )
        if not has_generate and not has_reserve_own:
            raise serializers.ValidationError(
                'Укажите article/gtin (генерация) или party_numbers (резервирование своих номеров).'
            )
        if has_generate and not attrs.get('production_date'):
            raise serializers.ValidationError(
                'Для генерации укажите production_date.'
            )
        if has_reserve_own and not attrs.get('product_group'):
            raise serializers.ValidationError(
                'Для резервирования своих номеров укажите product_group.'
            )
        return attrs


class UIPReserveRequestSerializer(UIPReserveItemSerializer):
    """
    Тело запроса: один объект или список объектов.

    Принимает либо один объект (поля см. выше), либо массив таких объектов.
    Каждый объект — либо генерация нового УИП (article/gtin + production_date [+ count]),
    либо резервирование своих номеров (product_group + party_numbers).
    """
    def to_internal_value(self, data):
        if isinstance(data, dict):
            item = UIPReserveItemSerializer(data=data)
            item.is_valid(raise_exception=True)
            return item.validated_data
        if isinstance(data, list):
            if not data:
                raise serializers.ValidationError(
                    'Список запросов не может быть пустым.'
                )
            results = []
            for d in data:
                item = UIPReserveItemSerializer(data=d)
                item.is_valid(raise_exception=True)
                results.append(item.validated_data)
            return results
        raise serializers.ValidationError(
            'Тело запроса должно быть объектом или списком объектов.'
        )

    def validate(self, attrs):
        # Валидация каждого элемента уже выполнена в to_internal_value.
        if isinstance(attrs, list):
            return attrs
        return super().validate(attrs)

    def to_representation(self, instance):
        return instance
