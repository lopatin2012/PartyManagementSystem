# app_cz/serializers.py

from rest_framework import serializers
from app_cz.models import CISCode
from app_cz.enums import TypeProduct
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
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = UIP
        fields = [
            'id', 'number', 'number_type', 'status', 'status_display',
            'product_name', 'planned_quantity', 'produced_quantity',
            'created_at', 'updated_at'
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
        max_length=100
    )


class ClosePartySerializer(serializers.Serializer):
    cis = serializers.CharField(min_length=31, max_length=50)
    batch_number = serializers.CharField(min_length=21, max_length=32)

