# app_cz/views.py

from datetime import datetime
import logging

from django.shortcuts import render
from django.utils import timezone

from rest_framework import viewsets, filters
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny

from app_cz.serializers import CISCodeSerializer, UIPSerializer, ProductionPartySerializer
from app_cz.models import CISCode, SUZAccount
from app_cz.services.suz_client import get_true_api_auth_key, refresh_suz_dynamic_token
from app_cz.services.party_service import (
    generate_party_numbers,
    reserve_parties_honest_sign,
    get_all_reserved_parties,
    close_party_reservation,
)
from app_cz.services.code_sync import sync_codes_task
from app_cz.serializers import (
    GeneratePartySerializer, ReservePartySerializer, ClosePartySerializer, SyncCodesTaskSerializer
)

from app_factory.models import PackagingLevelChoices

from app_uip.models import UIP, ProductionParty

from app_helper.sign_helper import get_list_certificates

logger = logging.getLogger(__name__)


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


# ==========================================
# Функции для взаимодействия с СУЗ.
# ==========================================

@api_view(['GET'])
def api_get_suz_certificates(request):
    """Возвращает список доступных валидных сертификатов ЭЦП."""
    try:
        certs = get_list_certificates()
        logger.info(f"API вернул {len(certs)} сертификатов.")

        return Response(
            {'certificates': certs},
            status=status.HTTP_200_OK
        )
    except Exception as e:
        logger.error(f"Критическая ошибка в api_get_suz_certificates: {e}", exc_info=True)
        return Response(
            {'error': f'Ошибка на сервере: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
# @permission_classes([IsAdminUser])
def api_setup_suz_account(request):
    """Создает или обновляет активную запись СУЗ на основе выбранных данных."""
    try:
        data = request.data
        serial_number = data.get('serial_number')
        oms_id = data.get('oms_id')
        device_name = data.get('device_name')
        connection_identifier = data.get('connection_identifier')

        inn = data.get('inn', '000000000000')  # Пустой ИНН при отсутствии его на фронте.

        certs = get_list_certificates()
        cert_info = next((c for c in certs if c['serial_number'] == serial_number), None)

        if not cert_info:
            return Response(
                {
                    'error': 'Сертификат не найден или недействителен'
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Парсинг дат из строки "ДД-ММ-ГГГГ"
        valid_from = datetime.strptime(cert_info['valid_from'], "%d-%m-%Y")
        valid_to = datetime.strptime(cert_info['valid_for'], "%d-%m-%Y")

        account, created = SUZAccount.objects.update_or_create(
            serial_number=serial_number,
            defaults={
                'is_active': True,
                'certificate_name': cert_info['fio'],
                'inn': inn,
                'valid_from': valid_from,
                'valid_to': valid_to,
                'oms_id': oms_id,
                'device_name': device_name,
                'connection_identifier': connection_identifier,
            }
        )

        return Response(
            {
                'success': True,
                'message': 'СУЗ успешно настроен'
            },
            status=status.HTTP_200_OK
        )

    except Exception as e:
        return Response(
            {'error': str(e)
             },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
# @permission_classes([IsAdminUser])
def api_reset_suz_account(request):
    """Деактивирует текущую активную запись СУЗ."""
    SUZAccount.objects.filter(is_active=True).update(is_active=False)
    return Response(
        {
            'success': True,
            'message': 'Настройки СУЗ сброшены'
        },
        status=status.HTTP_200_OK
    )


@api_view(['GET'])
# @permission_classes([IsAdminUser])
def api_get_auth_key(request):
    """
    API-эндпоинт для получения ключа аутентификации TrueAPI.
    Может использоваться фронтендом или другими внутренними сервисами.
    """
    try:
        auth_data = get_true_api_auth_key()

        return Response(
            {
                'uuid': auth_data.get('uuid'),
                'data': auth_data.get('data')
            },
            status=status.HTTP_200_OK
        )

    except Exception as e:
        return Response(
            {
                'error': str(e)
            },
            status=status.HTTP_502_BAD_GATEWAY
        )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_refresh_suz_token(request):
    """
    Принудительное обновления динамического токена СУЗ.
    """
    try:
        success = refresh_suz_dynamic_token()

        if success:
            return Response(
                {
                    'success': True,
                    'message': 'Динамический токен успешно обновлён'
                },
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {
                    'success': False,
                    'message': 'Не удалось получить токен. Проверьте логи сервера и настройки СУЗ.'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    except Exception as e:
        return Response(
            {
                'success': False,
                'message': f'Критическая ошибка: {str(e)}'
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ==========================================
# Функции для взаимодействия с УИП. Только админы или авторизованные системы могут вызывать эти методы ЧЗ.
# ==========================================

@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_generate_parties(request):
    """Внешний API для генерации номеров партий в Честном Знаке."""
    serializer = GeneratePartySerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'is_error': True,
                'message_error': serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    result = generate_party_numbers(
        party_info_list=serializer.validated_data['party_info_list'],
        product_group=serializer.validated_data['product_group']
    )

    if result.get('is_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_reserve_parties(request):
    """Внешний API для резервирования своих номеров партий в Честном Знаке."""
    serializer = ReservePartySerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'is_error': True,
                'message_error': serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    result = reserve_parties_honest_sign(
        product_group=serializer.validated_data['product_group'],
        party_numbers=serializer.validated_data['party_numbers']
    )

    if result.get('is_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def api_get_all_reserved_parties(request):
    """Внешний API для получения списка всех зарезервированных партий из ЧЗ."""
    result = get_all_reserved_parties()

    if result.get('is_error'):
        return Response(
            result,
            status=status.HTTP_502_BAD_GATEWAY
        )

    return Response(result, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_close_party_reservation(request):
    """Внешний API для снятия партии с резерва (через отчет о нанесении)."""
    serializer = ClosePartySerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'has_error': True,
                'message': serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    # Отправляем отчёт о нанесении
    result = close_party_reservation(
        cis=serializer.validated_data['cis'],
        batch_number=serializer.validated_data['party_number'],
    )

    if result.get('has_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_sync_codes_task(request):
    """
    API для синхронизации кодов из рабочего проекта.
    """
    # Валидация входных данных.
    serializer = SyncCodesTaskSerializer(data=request.data)

    if not serializer.is_valid():
        return Response({
            'has_error': True,
            'message': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    # 2. Вызов сервиса с распаковкой проверенных данных.
    result = sync_codes_task(**serializer.validated_data)

    # 3. Возврат результата.
    if result.get('has_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)
