# app_uip/serializers.py

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

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
    product_article = serializers.CharField(source='product_sku.sku_code', read_only=True)

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
