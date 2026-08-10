# app_factory/views.py

from drf_spectacular.utils import extend_schema
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from app_factory.models import Workshop, Line, Product
from app_factory.serializers import (
    WorkshopSerializer, LineSerializer, ProductSerializer
)


@extend_schema(tags=['Производство'])
class WorkshopViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API для получения списка цехов.
    GET /api/v1/dictionaries/workshops/
    """
    serializer_class = WorkshopSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Workshop.objects.select_related('factory').all()

        # Фильтр по заводу.
        factory_id = self.request.query_params.get('factory_id')
        if factory_id:
            qs = qs.filter(factory_id=factory_id)

        # Фильтр по активности.
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')

        return qs.order_by('name')

    def list(self, request, *args, **kwargs):
        """Оборачиваем результат в ключ 'result' согласно ТЗ."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'result': serializer.data})


@extend_schema(tags=['Производство'])
class LineViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API для получения списка линий.
    GET /api/v1/dictionaries/lines/
    """
    serializer_class = LineSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Line.objects.select_related(
            'workshop__factory'
        ).all()

        # Фильтр по цеху.
        workshop_id = self.request.query_params.get('workshop_id')
        if workshop_id:
            qs = qs.filter(workshop_id=workshop_id)

        # Фильтр по заводу.
        factory_id = self.request.query_params.get('factory_id')
        if factory_id:
            qs = qs.filter(workshop__factory_id=factory_id)

        # Фильтр по активности.
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')

        return qs.order_by('name')

    def list(self, request, *args, **kwargs):
        """Оборачиваем результат в ключ 'result' согласно ТЗ."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'result': serializer.data})


@extend_schema(tags=['Производство'])
class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API для получения списка продуктов.
    GET /api/v1/dictionaries/products/
    """
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Product.objects.prefetch_related(
            'skus', 'packagings'
        ).all()

        # Фильтр по товарной группе.
        group = self.request.query_params.get('group')
        if group:
            qs = qs.filter(group=group)

        # Фильтр по активности.
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')

        # Поиск по названию.
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(name__icontains=search)

        # Поиск по артикулу (SKU)
        article = self.request.query_params.get('article')
        if article:
            qs = qs.filter(skus__article__icontains=article).distinct()

        # Поиск по GTIN.
        gtin = self.request.query_params.get('gtin')
        if gtin:
            qs = qs.filter(packagings__gtin=gtin).distinct()

        return qs.order_by('group', 'name')

    def list(self, request, *args, **kwargs):
        """Оборачиваем результат в ключ 'result' согласно ТЗ."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'result': serializer.data})
