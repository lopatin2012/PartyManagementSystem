# app_uip/views.py

from datetime import timedelta

from django.utils import timezone
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from app_uip.models import UIP, PartyStatusChoices
from app_uip.serializers import (
    UIPStatusSerializer,
    UIPBatchStatusSerializer,
    UIPActiveListSerializer,
    UIPBatchResultSerializer
)


class UIPStatusViewSet(viewsets.ViewSet):
    """
    API для проверки статусов УИП.

    GET /api/v1/status_parties/{number}/ — проверка одного УИП
    POST /api/v1/status_parties/batch/ — проверка списка УИП
    GET /api/v1/status_parties/active/ — список действующих УИП
    """

    # permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['УИП'],
        operation_id='status_parties_retrieve',
        summary='Проверка статуса одного УИП',
        parameters=[
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description='Номер УИП (partyNumber)',
            )
        ],
        responses={
            200: UIPStatusSerializer,
            404: UIPStatusSerializer,
        },
    )
    def retrieve(self, request, pk=None):
        """
        GET /api/v1/status_parties/{number}/
        Проверка статуса одного УИП.
        """
        try:
            uip = UIP.objects.get(number=pk)
        except UIP.DoesNotExist:
            return Response({
                'number': pk,
                'is_active': False,
                'detail': 'УИП не найден в системе'
            }, status=status.HTTP_404_NOT_FOUND)

        serializer = UIPStatusSerializer(uip)
        data = serializer.data

        # Если УИП недействующий, возвращаем 404.
        if not data['is_active']:
            return Response(data, status=status.HTTP_404_NOT_FOUND)

        # Если действующий — 200.
        return Response(data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=['УИП'],
        operation_id='status_parties_batch',
        summary='Пакетная проверка статусов УИП',
        request=UIPBatchStatusSerializer,
        responses={
            200: UIPBatchResultSerializer,
            404: UIPBatchResultSerializer,
        },
    )
    @action(detail=False, methods=['post'], url_path='batch')
    def batch_check(self, request):
        """
        POST /api/v1/status_parties/batch/
        Проверка списка УИП.

        Тело запроса:
        {
            "numbers": ["04601751016843160622680816000000", "04601751016843160622680816000001"]
        }
        """
        serializer = UIPBatchStatusSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        numbers = serializer.validated_data['numbers']

        # Находим все существующие УИП.
        found_uips = UIP.objects.filter(number__in=numbers)
        found_numbers = set(found_uips.values_list('number', flat=True))
        not_found_numbers = [n for n in numbers if n not in found_numbers]

        # Сериализуем найденные УИП.
        details = UIPStatusSerializer(found_uips, many=True).data

        # Определяем, все ли действующие.
        all_active = all(uip.status in UIP.ACTIVE_STATUSES for uip in found_uips)

        response_data = {
            'total': len(numbers),
            'found': len(found_uips),
            'not_found': not_found_numbers,
            'details': details
        }

        # Если есть не найденные или не все действующие — 404.
        if not_found_numbers or not all_active:
            return Response(response_data, status=status.HTTP_404_NOT_FOUND)

        # Все найдены и действующие — 200.
        return Response(response_data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=['УИП'],
        operation_id='status_parties_active',
        summary='Список действующих УИП',
        parameters=[
            OpenApiParameter(name='product_id', type=OpenApiTypes.UUID, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='product_article', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='gtin', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='search', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='production_date_from', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='production_date_to', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='reservation_date_from', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
            OpenApiParameter(name='reservation_date_to', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
        ],
        responses={200: UIPActiveListSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], url_path='active')
    def active_list(self, request):
        """
        GET /api/v1/status_parties/active/
        Список всех действующих УИП.

        По умолчанию: зарезервированные УИП + зарегистрированные за последние 7 дней.
        """
        queryset = UIP.objects.filter(
            status__in=UIP.ACTIVE_STATUSES
        ).select_related('product_sku').order_by('-created_at')

        # Фильтры по продукту.
        product_id = request.query_params.get('product_id')
        if product_id:
            queryset = queryset.filter(product_sku__product_id=product_id)

        product_article = request.query_params.get('product_article')
        if product_article:
            queryset = queryset.filter(product_sku__sku_code__icontains=product_article)

        gtin = request.query_params.get('gtin')
        if gtin:
            queryset = queryset.filter(number__startswith=gtin)

        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(number__icontains=search)

        # Фильтры по датам
        production_date_from = request.query_params.get('production_date_from')
        production_date_to = request.query_params.get('production_date_to')
        reservation_date_from = request.query_params.get('reservation_date_from')
        reservation_date_to = request.query_params.get('reservation_date_to')

        # Применяем фильтры по датам, если указаны
        if production_date_from:
            queryset = queryset.filter(production_date__gte=production_date_from)
        if production_date_to:
            queryset = queryset.filter(production_date__lte=production_date_to)
        if reservation_date_from:
            queryset = queryset.filter(reservation_date__gte=reservation_date_from)
        if reservation_date_to:
            queryset = queryset.filter(reservation_date__lte=reservation_date_to)

        # Если НЕ указаны фильтры по датам, применяем дефолтную логику:
        # показать все зарезервированные + зарегистрированные за последние 7 дней.
        if not (production_date_from or production_date_to or
                reservation_date_from or reservation_date_to):
            seven_days_ago = timezone.now().date() - timedelta(days=7)
            queryset = queryset.filter(
                Q(status__in=[
                    PartyStatusChoices.RESERVED_CZ,
                    PartyStatusChoices.RESERVED_LOCAL
                ]) |
                Q(
                    status=PartyStatusChoices.REGISTERED,
                    production_date__gte=seven_days_ago
                )
            )

        serializer = UIPActiveListSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'result': serializer.data
        })
