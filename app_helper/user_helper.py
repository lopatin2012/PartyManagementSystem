# app_helper/user_helper.py

from django.http import HttpRequest


def get_user_name(request: HttpRequest, default: str = 'Неизвестный') -> str:
    """
    Возвращает отображаемое имя пользователя.

    Приоритет:
    1. Полное имя (first_name + last_name)
    2. Username
    3. Значение по умолчанию (для неавторизованных)
    """
    if not request.user.is_authenticated:
        return default

    full_name = request.user.get_full_name()
    if full_name and full_name.strip():
        return full_name.strip()

    if request.user.username and request.user.username.strip():
        return request.user.username.strip()

    return default
