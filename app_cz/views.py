# app_cz/views.py

from django.shortcuts import render

from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated, AllowAny

from app_cz.serializers import CISCodeSerializer, UIPSerializer, ProductionPartySerializer
from app_cz.models import CISCode

from app_uip.models import UIP, ProductionParty


class CISCodeViewSet(viewsets.ReadOnlyModelViewSet):
    """API для просмотра кодов маркировки (только чтение)."""
    queryset = CISCode.objects.select_related(
        'production_party__uip',
        'product_packaging__product'
    ).order_by('-created_at')
    serializer_class = CISCodeSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['=code', 'code__istartswith']
    ordering_fields = ['created_at', 'cz_status', 'production_status']
    ordering = ['-created_at']

    # permission_classes = [AllowAny]


class UIViewSet(viewsets.ReadOnlyModelViewSet):
    """API для просмотра УИП (только чтение)."""
    queryset = UIP.objects.select_related('product_sku__product').order_by('-created_at')
    serializer_class = UIPSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['number', 'number__icontains']
    ordering_fields = ['created_at', 'status']
    ordering = ['-created_at']


class ProductionPartyViewSet(viewsets.ReadOnlyModelViewSet):
    """API для просмотра производственных партий (только чтение)."""
    queryset = ProductionParty.objects.select_related(
        'uip', 'factory', 'line'
    ).order_by('-created_at')
    serializer_class = ProductionPartySerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['production_party', 'external_number_task', 'uip__number']
    ordering_fields = ['created_at', 'production_datetime_start']
    ordering = ['-created_at']
