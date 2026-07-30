# app_factory/serializers.py

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from app_factory.models import (
    Factory, Workshop, Line, Product, ProductPackaging, ProductSKU,
    PackagingLevelChoices
)


class WorkshopSerializer(serializers.ModelSerializer):
    """Сериализатор для цехов."""
    factory_id = serializers.UUIDField(source='factory.id', read_only=True)
    factory_name = serializers.CharField(source='factory.name', read_only=True)
    factory_ip_address = serializers.IPAddressField(source='factory.ip_address', read_only=True, allow_null=True)
    factory_port_address = serializers.IntegerField(source='factory.port_address', read_only=True, allow_null=True)

    class Meta:
        model = Workshop
        fields = [
            'id', 'name',
            'factory_id', 'factory_name',
            'factory_ip_address',
            'factory_port_address',
            'is_active'
        ]


class LineSerializer(serializers.ModelSerializer):
    """Сериализатор для линий."""
    workshop_id = serializers.UUIDField(source='workshop.id', read_only=True)
    workshop_name = serializers.CharField(source='workshop.name', read_only=True)
    factory_id = serializers.UUIDField(source='workshop.factory.id', read_only=True)
    factory_name = serializers.CharField(source='workshop.factory.name', read_only=True)
    factory_ip_address = serializers.IPAddressField(source='workshop.factory.ip_address', read_only=True,
                                                    allow_null=True)

    class Meta:
        model = Line
        fields = [
            'id', 'name',
            'workshop_id', 'workshop_name',
            'factory_id', 'factory_name',
            'factory_ip_address',
            'is_active'
        ]


class ProductSerializer(serializers.ModelSerializer):
    """
    Сериализатор для продуктов.
    Собирает данные из Product, ProductSKU, ProductPackaging.
    """
    articles = serializers.SerializerMethodField()
    gtin = serializers.SerializerMethodField()
    gtin_packaging = serializers.SerializerMethodField()
    gtin_pallets = serializers.SerializerMethodField()
    expiration_date = serializers.IntegerField(source='shelf_life_in_days', read_only=True)
    expiration_time = serializers.IntegerField(source='shelf_life_in_minutes', read_only=True)
    box_nesting = serializers.SerializerMethodField()
    pallet_nesting = serializers.SerializerMethodField()
    code_storage_period_in_days = serializers.SerializerMethodField()
    code_tnved = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'name',
            'articles',
            'gtin',
            'gtin_packaging',
            'gtin_pallets',
            'expiration_date',
            'expiration_time',
            'box_nesting',
            'pallet_nesting',
            'is_active',
            'code_storage_period_in_days',
            'code_tnved'
        ]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_articles(self, obj):
        """Список артикулов (SKU из 1С)."""
        return list(
            obj.skus.filter(is_active=True).values_list('sku_code', flat=True)
        )

    def _get_packaging(self, obj, level):
        """Вспомогательный метод для получения упаковки по уровню."""
        return obj.packagings.filter(level=level, is_active=True).first()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_gtin(self, obj):
        """GTIN потребительской упаковки (уровень 1)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.UNIT)
        return packaging.gtin if packaging else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_gtin_packaging(self, obj):
        """GTIN групповой упаковки (уровень 2)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.GROUP)
        return packaging.gtin if packaging else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_gtin_pallets(self, obj):
        """GTIN транспортной упаковки (уровень 3)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.TRANSPORT)
        return packaging.gtin if packaging else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_box_nesting(self, obj):
        """Количество единиц в коробке (из групповой упаковки)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.GROUP)
        return packaging.quantity_inside if packaging else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_pallet_nesting(self, obj):
        """Количество коробок в паллете (из транспортной упаковки)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.TRANSPORT)
        return packaging.quantity_inside if packaging else None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_code_storage_period_in_days(self, obj):
        """Срок хранения кодов в архиве (из потребительской упаковки)."""
        packaging = self._get_packaging(obj, PackagingLevelChoices.UNIT)
        return packaging.code_storage_period_in_days if packaging else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_code_tnved(self, obj):
        """
        Код ТН ВЭД из потребительской упаковки.
        Может быть null, если не заполнен.
        """
        packaging = self._get_packaging(obj, PackagingLevelChoices.UNIT)
        return packaging.code_tnved if packaging else None
