# app_helper/views.py

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from rest_framework.decorators import api_view
from rest_framework.response import Response

from config.settings import SERVICE_VERSION

from app_helper.service_helper import diagnose_service


@extend_schema(
    tags=["Помощник"],
    summary="Возвращает текущее состояние сервиса",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(['GET'])
def api_status_service(request):
    """
    Статус работы сервиса для внешних систем: доступность, версия, нагрузка.
    """
    diagnosis = diagnose_service()

    if diagnosis['is_available']:
        message = 'Сервис работает'
    else:
        failed = [name for name, c in diagnosis['checks'].items() if not c['ok']]
        message = f'Сервис частично недоступен: {", ".join(failed)}'

    return Response({
        'is_error': False,
        'is_available': diagnosis['is_available'],
        'message': message,
        'version': SERVICE_VERSION,
        'load': diagnosis['checks']['load'],
        'checks': diagnosis['checks'],
    })
