# config/context_processors.py

from django.conf import settings

from config.settings import SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION

def global_footer_info(request):
    """
    Добавляем информацию в контекст в шаблоны.
    :param request:
    :return:
    """
    requests_count = 1000

    return {
        'service_version': getattr(settings, 'SERVICE_VERSION', SERVICE_VERSION),
        'requests_count': requests_count,
        'service_mode_name': getattr(settings, 'SERVICE_MODE_TEXT', SERVICE_MODE_TEXT),
        'service_mode_color': getattr(settings, 'SERVICE_MODE_COLOR', SERVICE_MODE_COLOR),
    }