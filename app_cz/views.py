# app_cz/views.py

import json
import logging
import re
import threading
import uuid
from datetime import datetime

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from rest_framework import viewsets, filters, status
from rest_framework.decorators import api_view, action, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny

from app_cz.serializers import (
    CISCodeSerializer, UIPSerializer, ProductionPartySerializer, GenerateUIPSerializer
)
from app_cz.models import CISCode, SUZAccount
from app_cz.services.suz_client import get_true_api_auth_key, refresh_suz_dynamic_token
from app_cz.services.party_service import (
    generate_party_numbers,
    reserve_parties_honest_sign,
    get_all_reserved_parties,
    close_party_reservation, generate_uip, find_sku_by_gtin,
    sync_parties_from_cz,
    get_available_products,
)
from app_cz.services.code_sync import (
    sync_codes_task,
    receive_external_task,
    sync_codes_for_party,
    sync_all_external_tasks,
)
from app_cz.serializers import (
    # Взаимодействие УИП через ЧЗ.
    GeneratePartySerializer, ReservePartySerializer, ClosePartySerializer,

    # Синхронизация кодов из задания.
    SyncCodesTaskSerializer,
    # Приёмник задания из внешнего сервиса.
    ReceiveExternalTaskSerializer,
    # Ручная синхронизация кодов партии.
    SyncTaskCodesRequestSerializer,
    # Подробная информация о коде.
    CISCodeDetailSerializer,

    # Endpoint для внешних информационных систем по зарезервированным партиям (УИП)
    # с большей информацией и возможностями.
    ReservedPartyListSerializer,
    ReservedPartyDetailSerializer,
    ReservedPartyCodesSerializer,
    # Резервирование чернового УИП.
    ReserveDraftUIPSerializer
)
from app_factory.models import Product, ProductSKU, NationalCatalogProduct, CardStateChoices, StateConditionChoices

from app_uip.models import UIP, ProductionParty, PartyStatusChoices, ProductionPartyStatusChoices, ProductionPartySyncStatusChoices

from app_helper.sign_helper import get_list_certificates

logger = logging.getLogger(__name__)


@extend_schema(tags=["Честный Знак"])
class CISCodeViewSet(viewsets.ReadOnlyModelViewSet):
    """API для просмотра кодов маркировки (только чтение)."""

    lookup_field = 'code'

    serializer_class = CISCodeSerializer

    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['=code', 'code__istartswith']
    ordering_fields = ['created_at', 'cz_status', 'production_status']
    ordering = ['-created_at']

    def get_serializer_class(self):
        """Используем детальный сериализатор только при просмотре одного кода."""
        if self.action == 'retrieve':
            return CISCodeDetailSerializer
        return self.serializer_class

    def get_queryset(self):
        """
        Загружаем все связанные данные ОДНИМ SQL-запросом (JOIN).
        """
        return CISCode.objects.select_related(
            'production_party__uip',
            'production_party__factory',
            'production_party__workshop',
            'production_party__line',
            'product_packaging__product'
        )

    # permission_classes = [AllowAny]


@extend_schema(tags=["Честный Знак"])
class ReservedPartyViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API для работы с зарезервированными партиями (УИП).

    GET /api/v1/reserved_parties/ — список с фильтрацией
    GET /api/v1/reserved_parties/{id_or_number}/ — детальная информация (с кодами и партиями)
    GET /api/v1/reserved_parties/{id_or_number}/codes/ — дерево кодов
    """

    queryset = UIP.objects.all()

    # permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ReservedPartyDetailSerializer
        if self.action == 'codes':
            return ReservedPartyCodesSerializer
        return ReservedPartyListSerializer

    def get_object(self):
        """Умный lookup: UUID или номер УИП."""
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs.get('pk')

        if not lookup_value:
            raise NotFound("Не указан идентификатор")

        # Проверяем, является ли строка валидным UUID
        try:
            uuid.UUID(lookup_value)
            obj = queryset.filter(id=lookup_value).first()
        except (ValueError, AttributeError):
            obj = queryset.filter(number=lookup_value).first()

        if not obj:
            raise NotFound(f"УИП не найден: {lookup_value}")

        self.check_object_permissions(self.request, obj)
        return obj

    def get_queryset(self):
        """Базовый queryset с фильтрацией."""
        qs = UIP.objects.select_related(
            'product_sku__product'
        ).prefetch_related(
            'production_parties__line__workshop__factory',
            'product_sku__product__skus',
            'product_sku__product__packagings'
        )

        params = self.request.query_params

        # === ФИЛЬТР ПО СТАТУСУ ===
        status_param = params.get('status')
        if status_param:
            if status_param != 'all':
                statuses = [s.strip() for s in status_param.split(',')]
                qs = qs.filter(status__in=statuses)
        else:
            qs = qs.filter(
                status__in=[
                    PartyStatusChoices.RESERVED_CZ,
                    PartyStatusChoices.RESERVED_LOCAL,
                    PartyStatusChoices.REGISTERED,
                ]
            )

        # === ФИЛЬТР ПО ДАТЕ МАРКИРОВКИ ===
        marking_date = params.get('marking_date')
        if marking_date:
            try:
                parsed = datetime.strptime(marking_date, '%d.%m.%Y')
                qs = qs.filter(
                    production_parties__marking_datetime__date=parsed.date()
                )
            except ValueError:
                pass

        marking_date_from = params.get('marking_date_from')
        if marking_date_from:
            try:
                parsed = datetime.strptime(marking_date_from, '%d.%m.%Y')
                qs = qs.filter(
                    production_parties__marking_datetime__date__gte=parsed.date()
                )
            except ValueError:
                pass

        # === ФИЛЬТР ПО ВРЕМЕНИ МАРКИРОВКИ ===
        marking_time = params.get('marking_time')
        if marking_time:
            try:
                parsed_time = datetime.strptime(marking_time, '%H:%M').time()
                qs = qs.filter(
                    production_parties__marking_datetime__time=parsed_time
                )
            except ValueError:
                pass

        # === ФИЛЬТРЫ ПО СВЯЗЯМ ===
        workshop_id = params.get('workshop_id')
        if workshop_id:
            qs = qs.filter(production_parties__workshop_id=workshop_id)

        line_id = params.get('line_id')
        if line_id:
            qs = qs.filter(production_parties__line_id=line_id)

        product_id = params.get('product_id')
        if product_id:
            qs = qs.filter(product_sku__product_id=product_id)

        article = params.get('article')
        if article:
            qs = qs.filter(product_sku__article__icontains=article)

        # === ПОИСК ===
        search = params.get('search')
        if search:
            qs = qs.filter(number__icontains=search)

        # === СОРТИРОВКА ===
        ordering = params.get('ordering')
        if ordering:
            ordering_map = {
                'marking_date': 'production_parties__marking_datetime',
                '-marking_date': '-production_parties__marking_datetime',
                'party_number': 'number',
                '-party_number': '-number',
                'status': 'status',
                '-status': '-status',
                'created_at': 'created_at',
                '-created_at': '-created_at',
            }
            real_ordering = ordering_map.get(ordering, ordering)
            qs = qs.order_by(real_ordering)
        else:
            qs = qs.order_by('-created_at')

        return qs.distinct()

    @action(detail=True, methods=['get'], url_path='codes')
    def codes(self, request, pk=None):
        """GET /api/v1/reserved_parties/{id_or_number}/codes/"""
        uip = self.get_object()
        serializer = ReservedPartyCodesSerializer(uip)
        return Response(serializer.data)


@extend_schema(tags=["Честный Знак"])
class ProductionPartyViewSet(viewsets.ReadOnlyModelViewSet):
    """API для просмотра производственных партий (только чтение)."""
    queryset = ProductionParty.objects.select_related(
        'uip', 'line__workshop__factory'
    ).order_by('-created_at')
    serializer_class = ProductionPartySerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['production_party', 'external_number_task', 'uip__number']
    ordering_fields = ['created_at', 'production_datetime_start']
    ordering = ['-created_at']


# ==========================================
# Функции для взаимодействия с СУЗ.
# ==========================================

@extend_schema(
    tags=['Честный Знак'],
    summary="Возвращает список доступных валидных сертификатов ЭЦП",
    responses={
        200: OpenApiTypes.OBJECT,
        500: OpenApiTypes.OBJECT
    }
)
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Создает или обновляет активную запись СУЗ",
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT}
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Деактивирует текущую активную запись СУЗ",
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiTypes.OBJECT}
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Получение ключа аутентификации TrueAPI",
    responses={200: OpenApiTypes.OBJECT, 502: OpenApiTypes.OBJECT}
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Принудительное обновление динамического токена СУЗ",
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT}
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

@extend_schema(
    tags=['Честный Знак'],
    summary="Генерация номеров партий в Честном Знаке",
    request=GeneratePartySerializer,
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
)
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Резервирование своих номеров партий в Честном Знаке",
    request=ReservePartySerializer,
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
)
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Получение списка всех зарезервированных партий из ЧЗ",
    responses={200: OpenApiTypes.OBJECT, 502: OpenApiTypes.OBJECT}
)
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Снятие партии с резерва (через отчет о нанесении)",
    request=ClosePartySerializer,
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
)
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


@extend_schema(
    tags=['Честный Знак'],
    summary="Синхронизация кодов из рабочего проекта",
    request=SyncCodesTaskSerializer,
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}
)
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

# Добавить токен для предотвращения случайной генерации.
@extend_schema(
    tags=['Честный Знак'],
    summary="Генерация УИП по запросу извне",
    request=GenerateUIPSerializer,
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        404: OpenApiTypes.OBJECT
    }
)
@api_view(['POST'])
def api_generate_uip(request):
    """Внешний API для генерации одного УИП с резервированием в Честном Знаке."""
    serializer = GenerateUIPSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'is_error': True,
                'message_error': serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    # Поиск продукта по артикулу или GTIN.
    product_sku = None
    if data.get('article'):
        product_sku = ProductSKU.objects.filter(
            article=data['article'], is_active=True
        ).select_related('product').first()

    elif data.get('gtin'):
        product_sku = find_sku_by_gtin(data['gtin'])

    if not product_sku:
        logger.error(
            f"Критическая ошибка. Отсутствует запрошенный продукт: {data.get('article')}",
            exc_info=True
        )
        return Response(
            {
                'is_error': True,
                'message': 'Продукт не найден или неактивен.'
            },
            status=status.HTTP_404_NOT_FOUND
        )

    # Используем единый генератор УИП. Единичное количество, без множества.
    result = generate_uip(
        product_sku=product_sku,
        production_date=data['production_date'],
        mode=data['mode'],
        is_external_service=True,
        skip_cz=data.get('skip_cz', True) # Черновик на время ввода разработки.
    )

    if result.get('is_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


@extend_schema(
    tags=['Честный Знак'],
    summary="Резервирование черновой УИП в Честном Знаке",
    request=ReserveDraftUIPSerializer,
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        403: OpenApiTypes.OBJECT,
        404: OpenApiTypes.OBJECT,
    }
)
@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_reserve_draft_uip(request):
    """
    Резервирует черновую УИП в Честном Знаке и переводит её в статус RESERVED_LOCAL.
    Доступно только администраторам.
    """
    serializer = ReserveDraftUIPSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'is_error': True,
                'message': 'Некорректные данные запроса',
                'errors': serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    uip_id = serializer.validated_data['uip_id']

    # 1. Ищем УИП.
    try:
        uip = UIP.objects.select_related('product_sku__product').get(id=uip_id)
    except UIP.DoesNotExist:
        return Response(
            {'is_error': True, 'message': 'УИП не найден.'},
            status=status.HTTP_404_NOT_FOUND
        )

    # 2. Проверяем, что он в черновике.
    if uip.status != PartyStatusChoices.DRAFT:
        return Response(
            {
                'is_error': True,
                'message': (
                    f'УИП уже в статусе "{uip.get_status_display()}". '
                    f'Резервировать можно только черновики.'
                )
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    # 3. Базовые проверки данных УИП.
    if not uip.number:
        return Response(
            {'is_error': True, 'message': 'У УИП отсутствует номер.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    product_group = uip.product_sku.product.group
    if not product_group:
        return Response(
            {'is_error': True, 'message': 'У продукта не указана товарная группа.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 4. Резервируем в ЧЗ.
    result = reserve_parties_honest_sign(
        product_group=product_group,
        party_numbers=[uip.number],
    )

    if result.get('is_error'):
        return Response(
            {
                'is_error': True,
                'message': (
                    f'ЧЗ отклонил резервирование: '
                    f'{result.get("message_error", "неизвестная ошибка")}'
                )
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    # 5. Успех: меняем статус и ставим reservation_date.
    uip.reservation_date = timezone.now().date()
    uip.description = 'Зарезервирован вручную из черновика через API'
    uip.save(update_fields=['reservation_date', 'description', 'updated_at'])

    uip.change_status(
        PartyStatusChoices.RESERVED_LOCAL,
        source='api',
        note='Зарезервирован вручную из черновика через API',
        changed_by=(
            request.user
            if request.user.is_authenticated
            else None
        ),
    )

    return Response(
        {
            'is_error': False,
            'message': f'УИП {uip.number} успешно зарезервирован в ЧЗ.',
            'number': uip.number,
            'new_status': PartyStatusChoices.RESERVED_LOCAL,
        },
        status=status.HTTP_200_OK
    )


# ==========================================
# Синхронизация с внешним сервисом (Молвест.Маркировка).
# ==========================================

@extend_schema(
    tags=['Честный Знак'],
    summary="Приёмник задания из внешнего сервиса",
    request=ReceiveExternalTaskSerializer,
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
    }
)
@api_view(['POST'])
def api_receive_external_task(request):
    """
    Приёмник производственной партии (задания) из внешнего сервиса.

    Внешний сервис периодически «пушит» сюда данные своего задания
    (поля модели Task). Задание создаётся/обновляется в локальной БД,
    после чего по нему можно синхронизировать коды маркировки.
    """
    serializer = ReceiveExternalTaskSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'has_error': True,
                'message': 'Некорректные данные задания',
                'errors': serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    result = receive_external_task(serializer.validated_data)

    if result.get('has_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


@extend_schema(
    tags=['Честный Знак'],
    summary="Ручная синхронизация кодов задания из внешнего сервиса",
    request=SyncTaskCodesRequestSerializer,
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
    }
)
@api_view(['POST'])
@permission_classes([IsAdminUser])
def api_sync_task_codes(request):
    """
    Синхронизация кодов маркировки для производственной партии
    по её заданию во внешнем сервисе.
    """
    serializer = SyncTaskCodesRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                'has_error': True,
                'message': serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        party = ProductionParty.objects.get(
            id=serializer.validated_data['production_party_id']
        )
    except ProductionParty.DoesNotExist:
        return Response(
            {
                'has_error': True,
                'message': 'Производственная партия не найдена.'
            },
            status=status.HTTP_400_BAD_REQUEST
        )

    result = sync_codes_for_party(
        party,
        url=serializer.validated_data.get('url'),
        token=serializer.validated_data.get('token'),
    )

    if result.get('has_error'):
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    return Response(result, status=status.HTTP_200_OK)


# ==========================================
# API-методы
# ==========================================

@method_decorator(staff_member_required, name='dispatch')
class SyncPartiesView(View):
    """Синхронизация УИП из Честного Знака (только для админов)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse({
                'is_error': True,
                'message': 'Доступ только для администраторов'
            }, status=403)

        result = sync_parties_from_cz()

        status_code = (
            502
            if result.get('is_error')
            else 200
        )
        return JsonResponse(result, status=status_code)


@method_decorator(staff_member_required, name='dispatch')
class GenerateUIPView(View):
    """Генерация УИП вручную (только для администраторов)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Доступ только для администраторов.'
                },
                status=403
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Некорректный формат данных.'
                },
                status=400
            )

        product_sku_id = data.get('product_sku_id')
        production_date_str = data.get('production_date')
        mode = data.get('mode', 'local')
        party = data.get('party') or '000'

        if not product_sku_id or not production_date_str:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Не указаны обязательные параметры.'
                },
                status=400
            )

        try:
            production_date = datetime.strptime(
                production_date_str, '%Y-%m-%d'
            ).date()
        except ValueError:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Некорректный формат даты (ожидается ГГГГ-ММ-ДД).'
                },
                status=400
            )

        # Получаем объект продукта из БД.
        try:
            product_sku = ProductSKU.objects.get(id=product_sku_id)

        except ProductSKU.DoesNotExist:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Не найден указанный продукт по id'
                },
                status=400
            )

        result = generate_uip(
            product_sku, production_date, mode,
            party=party
        )

        status_code = (
            200
            if not result.get('is_error')
            else 400
        )
        return JsonResponse(result, status=status_code)


# ==========================================
# Страница синхронизации с внешним сервисом (Молвест.Маркировка).
# ==========================================

@method_decorator(staff_member_required, name='dispatch')
class SyncTasksView(TemplateView):
    """
    Страница отслеживания синхронизации заданий с внешним сервисом.

    Показывает производственные партии, полученные из внешнего сервиса:
    статус задания, статус синхронизации, последнюю синхронизацию и т.д.
    """
    template_name = 'sync/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        get = self.request.GET

        status_filter = get.get('status', '')
        sync_filter = get.get('sync', '')
        search = get.get('search', '').strip()

        queryset = ProductionParty.objects.filter(
            is_external=True
        ).select_related(
            'uip__product_sku__product',
            'line__workshop__factory',
        ).order_by('-created_at')

        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        if sync_filter and sync_filter != 'all':
            queryset = queryset.filter(sync_status=sync_filter)
        if search:
            queryset = queryset.filter(
                Q(external_number_task__icontains=search)
                | Q(production_party__icontains=search)
                | Q(uip__number__icontains=search)
                | Q(uip__product_sku__article__icontains=search)
            )

        paginator = Paginator(queryset, 50)
        page_number = get.get('page', 1)
        page_obj = paginator.get_page(page_number)

        # Диапазон страниц.
        current_page = page_obj.number
        total_pages = paginator.num_pages
        page_range = [1]
        start = max(2, current_page - 2)
        end = min(total_pages - 1, current_page + 2)
        if start > 2:
            page_range.append('...')
        page_range.extend(range(start, end + 1))
        if end < total_pages - 1:
            page_range.append('...')
        if total_pages > 1:
            page_range.append(total_pages)

        params = get.copy()
        params.pop('page', None)
        query_string = params.urlencode()

        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        # Статистика по статусам синхронизации.
        base_stats = ProductionParty.objects.filter(is_external=True)
        stats = {
            'total': base_stats.count(),
            'pending': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.PENDING
            ).count(),
            'synced': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.SYNCED
            ).count(),
            'error': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.ERROR
            ).count(),
            'active': base_stats.filter(
                status__in=[
                    ProductionPartyStatusChoices.CREATED,
                    ProductionPartyStatusChoices.WORK,
                    ProductionPartyStatusChoices.CLOSED,
                ]
            ).count(),
        }

        has_active_filters = bool(
            (status_filter and status_filter != 'all')
            or (sync_filter and sync_filter != 'all')
            or search
        )

        context.update({
            'title_name': 'Синхронизация заданий',
            'page_name': 'Синхронизация заданий',
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'query_string': query_string,
            'start_item': start_item,
            'end_item': end_item,
            'stats': stats,
            'status_choices': ProductionPartyStatusChoices.choices,
            'sync_status_choices': ProductionPartySyncStatusChoices.choices,
            'current_status': status_filter,
            'current_sync': sync_filter,
            'current_search': search,
            'has_active_filters': has_active_filters,
        })

        return context


@method_decorator(staff_member_required, name='dispatch')
class SyncTaskCodesView(View):
    """
    Ручная синхронизация кодов для одной производственной партии.
    POST /sync/task-codes/  {production_party_id: uuid}
    """

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {'is_error': True, 'message': 'Некорректный формат данных.'},
                status=400
            )

        party_id = data.get('production_party_id')
        if not party_id:
            return JsonResponse(
                {'is_error': True, 'message': 'Не указан production_party_id.'},
                status=400
            )

        try:
            party = ProductionParty.objects.get(id=party_id)
        except (ProductionParty.DoesNotExist, ValueError, TypeError):
            return JsonResponse(
                {'is_error': True, 'message': 'Производственная партия не найдена.'},
                status=400
            )

        result = sync_codes_for_party(party)

        status_code = 400 if result.get('has_error') else 200
        return JsonResponse(result, status=status_code)


@method_decorator(staff_member_required, name='dispatch')
class SyncAllTasksView(View):
    """
    Запуск полной синхронизации с внешним сервисом вручную.
    POST /sync/all/
    """

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403
            )

        result = sync_all_external_tasks()

        status_code = 502 if result.get('is_error') else 200
        return JsonResponse(result, status=status_code)


# ==========================================
# Национальный каталог (ГИС МТ).
# ==========================================

# Хранилище хода синхронизации НК: {user_id: progress}.
_NK_SYNC_PROGRESS = {}
_NK_SYNC_LOCK = threading.Lock()


def _nk_progress(user_id: int) -> dict:
    """Возвращает словарь прогресса для пользователя."""
    with _NK_SYNC_LOCK:
        return _NK_SYNC_PROGRESS.get(user_id, {
            'running': False, 'total': 0, 'done': 0,
            'created': 0, 'updated': 0, 'current_name': '', 'error': None,
        })


@method_decorator(staff_member_required, name='dispatch')
class NationalCatalogView(TemplateView):
    """Страница Национального каталога (только для администраторов)."""
    template_name = 'nk/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        get = self.request.GET
        q = get.get('q', '').strip()
        gtin_filter = get.get('gtin', '').strip()
        product_group = get.get('product_group', '').strip()
        readiness = get.get('readiness', '').strip()
        card_state = get.get('card_state', '').strip()
        state_condition = get.get('state_condition', '').strip()
        created_from = get.get('created_from', '').strip()
        created_to = get.get('created_to', '').strip()

        products = NationalCatalogProduct.objects.order_by('-synced_at')
        if q:
            products = products.filter(name__iregex=re.escape(q))
        if gtin_filter:
            products = products.filter(gtin__icontains=gtin_filter)
        if product_group:
            products = products.filter(product_group=product_group)
        if card_state:
            products = products.filter(card_state=card_state)
        if state_condition:
            products = products.filter(state_condition=state_condition)
        if created_from:
            products = products.filter(create_date__date__gte=created_from)
        if created_to:
            products = products.filter(create_date__date__lte=created_to)
        if readiness == 'ready':
            products = products.filter(
                card_state='published',
                state_condition__in=('ready_order_km', 'ready_commercialization'),
            )
        elif readiness == 'not_ready':
            products = products.exclude(
                card_state='published',
                state_condition__in=('ready_order_km', 'ready_commercialization'),
            )

        product_groups = (
            NationalCatalogProduct.objects
            .exclude(product_group='')
            .order_by('product_group_name')
            .values_list('product_group', 'product_group_name')
            .distinct()
        )

        paginator = Paginator(products, 25)
        page_number = get.get('page', 1)
        try:
            page_obj = paginator.get_page(page_number)
        except Exception:
            page_obj = paginator.get_page(1)

        # Диапазон страниц.
        current_page = page_obj.number
        total_pages = paginator.num_pages
        page_range = [1]
        start = max(2, current_page - 2)
        end = min(total_pages - 1, current_page + 2)
        if start > 2:
            page_range.append('...')
        page_range.extend(range(start, end + 1))
        if end < total_pages - 1:
            page_range.append('...')
        if total_pages > 1:
            page_range.append(total_pages)

        params = get.copy()
        params.pop('page', None)
        query_string = params.urlencode()

        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        has_active_filters = bool(
            q or gtin_filter or product_group or card_state
            or state_condition or created_from or created_to or readiness
        )

        context.update({
            'title_name': 'Национальный каталог',
            'page_name': 'Национальный каталог',
            'products': page_obj.object_list,
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'query_string': query_string,
            'start_item': start_item,
            'end_item': end_item,
            'product_groups': product_groups,
            'card_state_choices': CardStateChoices.choices,
            'state_condition_choices': StateConditionChoices.choices,
            'default_inn': SUZAccount.objects.filter(is_active=True).values_list('inn', flat=True).first() or '',
            'q': q,
            'current_gtin': gtin_filter,
            'current_product_group': product_group,
            'current_readiness': readiness,
            'current_card_state': card_state,
            'current_state_condition': state_condition,
            'current_created_from': created_from,
            'current_created_to': created_to,
            'has_active_filters': has_active_filters,
        })

        return context


@method_decorator(staff_member_required, name='dispatch')
class NKSyncProductsView(View):
    """API: Запуск синхронизации товаров Национального каталога (POST)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403,
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = {}

        owner_inn = data.get('owner_inn') or None
        brand_id = data.get('brand_id') or None
        cat_id = data.get('cat_id') or None

        with _NK_SYNC_LOCK:
            existing = _NK_SYNC_PROGRESS.get(request.user.id)
            if existing and existing.get('running'):
                return JsonResponse(
                    {'is_error': True, 'message': 'Синхронизация уже выполняется'},
                    status=409,
                )

        progress = {
            'running': True, 'total': 0, 'done': 0,
            'created': 0, 'updated': 0, 'current_name': '', 'error': None,
            'phase': 'list',
        }
        with _NK_SYNC_LOCK:
            _NK_SYNC_PROGRESS[request.user.id] = progress

        def _run_sync():
            from app_cz.services.national_catalog_client import sync_products
            from app_factory.services.nk_sync_service import sync_nk_to_products
            try:
                sync_products(
                    request.user,
                    owner_inn=owner_inn,
                    brand_id=brand_id,
                    cat_id=cat_id,
                    progress=progress,
                )
                # После выгрузки данных из НК — синхронизируем Product/ProductPackaging.
                product_progress = {
                    'total': 0, 'done': 0,
                    'products_created': 0, 'products_updated': 0,
                    'packagings_created': 0, 'skus_created': 0,
                }
                sync_nk_to_products(user=request.user, progress=product_progress)
                progress['product_sync'] = product_progress
            except Exception as e:
                logger.error(f'Ошибка синхронизации НК: {e}', exc_info=True)
                progress['error'] = str(e)
            finally:
                progress['running'] = False

        threading.Thread(target=_run_sync, daemon=True).start()

        return JsonResponse({'is_error': False, 'message': 'Синхронизация запущена'})


@method_decorator(staff_member_required, name='dispatch')
class NKSyncProgressView(View):
    """API: Ход выполнения синхронизации НК (GET)."""

    def get(self, request):
        return JsonResponse({
            'is_error': False,
            'progress': _nk_progress(request.user.id),
        })


@method_decorator(staff_member_required, name='dispatch')
class NKProductCreateView(View):
    """POST: ручное создание Product + ProductPackaging + ProductSKU
    из товара Национального каталога."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse({'is_error': True, 'message': 'Доступ только для администраторов'}, status=403)

        data = json.loads(request.body)
        nk_product_id = data.get('nk_product_id')
        if not nk_product_id:
            return JsonResponse({'is_error': True, 'message': 'Не указан ID товара НК'}, status=400)

        try:
            nk = NationalCatalogProduct.objects.get(id=nk_product_id)
        except NationalCatalogProduct.DoesNotExist:
            return JsonResponse({'is_error': True, 'message': 'Товар НК не найден'}, status=404)

        # Если уже создан — возвращаем существующий.
        existing = Product.objects.filter(national_product=nk, is_active=True).first()
        if existing:
            return JsonResponse({
                'is_error': False,
                'message': 'Продукт уже создан',
                'product_id': str(existing.id),
            })

        from app_factory.services.nk_sync_service import sync_nk_to_products
        result = sync_nk_to_products(user=request.user, progress=None)

        product = Product.objects.filter(national_product=nk, is_active=True).first()
        if product:
            return JsonResponse({
                'is_error': False,
                'message': 'Продукт создан из Национального каталога',
                'product_id': str(product.id),
            })

        return JsonResponse({
            'is_error': True,
            'message': 'Не удалось создать продукт (товар не готов к производству)',
        }, status=400)


class NKProductDetailView(View):
    """API: Получение товара по good_id или gtin (POST)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403,
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {'is_error': True, 'message': 'Некорректный формат данных'},
                status=400,
            )

        good_id = data.get('good_id') or None
        gtin = data.get('gtin') or None

        if not good_id and not gtin:
            return JsonResponse(
                {'is_error': True, 'message': 'Укажите good_id или gtin'},
                status=400,
            )

        if good_id:
            product = NationalCatalogProduct.objects.filter(good_id=good_id).first()
        else:
            product = NationalCatalogProduct.objects.filter(gtin=gtin).first()

        if product:
            return JsonResponse({
                'is_error': False,
                'source': 'db',
                'product': {
                    'good_id': product.good_id,
                    'gtin': product.gtin,
                    'name': product.name,
                    'brand_name': product.brand_name,
                    'product_group': product.product_group,
                    'product_group_name': product.product_group_name,
                    'card_state': product.card_state,
                    'card_state_name': product.get_card_state_display(),
                    'state_condition': product.state_condition,
                    'state_condition_name': product.get_state_condition_display(),
                    'is_ready_for_production': product.is_ready_for_production,
                    'image_url': product.image_url,
                    'synced_at': product.synced_at.isoformat(),
                },
            })

        # Если нет в локальном кэше — запрашиваем напрямую из НК.
        try:
            from app_cz.services.national_catalog_client import NationalCatalogClient
            client = NationalCatalogClient(request.user)
            items = client.get_product(
                good_id=int(good_id) if good_id else None,
                gtin=gtin,
            )
            return JsonResponse({
                'is_error': False,
                'source': 'nk',
                'product': items[0] if items else None,
            })
        except Exception as e:
            logger.error(f'Ошибка получения товара НК: {e}', exc_info=True)
            return JsonResponse(
                {'is_error': True, 'message': f'Ошибка запроса к НК: {e}'},
                status=500,
            )
