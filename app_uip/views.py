# app_uip/views.py

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from app_uip.models import UIP
from app_uip.serializers import (
    UIPStatusSerializer,
    UIPBatchStatusSerializer,
    UIPActiveListSerializer
)


class UIPStatusViewSet(viewsets.ViewSet):
    """
    API для проверки статусов УИП.

    GET /api/v1/status_parties/{number}/ — проверка одного УИП
    POST /api/v1/status_parties/batch/ — проверка списка УИП
    GET /api/v1/status_parties/active/ — список действующих УИП
    """
    # permission_classes = [IsAuthenticated]

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

    @action(detail=False, methods=['get'], url_path='active')
    def active_list(self, request):
        """
        GET /api/v1/status_parties/active/
        Список всех зарезервированных и зарегистрированных УИП.
        """
        queryset = UIP.objects.filter(
            status__in=UIP.ACTIVE_STATUSES
        ).select_related('product_sku').order_by('-created_at')

        # Фильтры.
        product_id = request.query_params.get('product_id')
        if product_id:
            queryset = queryset.filter(product_sku__product_id=product_id)

        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(number__icontains=search)

        serializer = UIPActiveListSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'result': serializer.data
        })
