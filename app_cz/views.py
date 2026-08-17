# app_cz/views.py

import uuid
from datetime import datetime
import logging

from django.utils import timezone

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
)
from app_cz.services.code_sync import (
    sync_codes_task,
    receive_external_task,
    sync_codes_for_party,
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
from app_factory.models import ProductSKU

from app_uip.models import UIP, ProductionParty, PartyStatusChoices

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
