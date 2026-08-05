# app_helper/middleware.py
from app_helper.load_tracker import record_request

EXCLUDED_PREFIXES = (
    '/static/',
    '/media/',
    '/templates/',
    '/favicon.ico',
    '/cz/api/status/',
)

class LoadTrackingMiddleware:
    """Считает входящие запросы для расчёта нагрузки."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(EXCLUDED_PREFIXES):
            record_request()
        return self.get_response(request)
