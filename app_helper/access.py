# app_helper/access.py

"""
Ролевая модель доступа на основе встроенных групп Django.

Роли:
- «Админ» — полный доступ (как раньше у суперпользователя): все страницы,
  синхронизация, Национальный каталог, фоновые задачи, резервирование.
- «Просмотр» — доступ только к странице УИП. Генерация УИП разрешена, если
  пользователю выдано право на запись в модель uip (app_uip.add_uip /
  app_uip.change_uip).

Суперпользователь и staff всегда считаются администраторами — чтобы не
сломать существующие учётные записи и доступ в /admin/.
"""

from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.urls import reverse

from rest_framework.permissions import BasePermission

ROLE_ADMIN = 'Админ'
ROLE_VIEW = 'Просмотр'
ROLE_MONITORING = 'Мониторинг'

# Права на модель УИП.
UIP_VIEW_PERM = 'app_uip.view_uip'
UIP_WRITE_PERMS = ('app_uip.add_uip', 'app_uip.change_uip')


def is_admin(user) -> bool:
    """Администратор: суперпользователь, staff или член группы «Админ»."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name=ROLE_ADMIN).exists()


def is_viewer(user) -> bool:
    """Член группы «Просмотр» (или администратор)."""
    if not user or not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    return user.groups.filter(name=ROLE_VIEW).exists()


def can_view_uip(user) -> bool:
    """Доступ к странице УИП: админ, группа «Просмотр» или право view_uip."""
    if is_admin(user):
        return True
    if not user or not user.is_authenticated:
        return False
    return user.groups.filter(name=ROLE_VIEW).exists() or user.has_perm(UIP_VIEW_PERM)


def can_generate_uip(user) -> bool:
    """
    Генерация УИП: админ или наличие права на запись в модель uip
    (add_uip / change_uip).
    """
    if is_admin(user):
        return True
    if not user or not user.is_authenticated:
        return False
    return any(user.has_perm(perm) for perm in UIP_WRITE_PERMS)


# ==========================================
# Декораторы и миксины для представлений.
# ==========================================

def _login_url() -> str:
    try:
        return reverse('login')
    except Exception:
        return '/auth/login/'


class AdminRequiredMixin(LoginRequiredMixin):
    """CBV: только администратор; анонимного — на страницу входа."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_admin(request.user):
            raise PermissionDenied('Доступ только для администраторов.')
        return super().dispatch(request, *args, **kwargs)


class UipPageAccessMixin(LoginRequiredMixin):
    """CBV: страница УИП — администратор или роль «Просмотр» (право view_uip)."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_view_uip(request.user):
            raise PermissionDenied('Нет доступа к странице УИП.')
        return super().dispatch(request, *args, **kwargs)


def admin_required(view_func):
    """Только администратор. Анонимного — на страницу входа."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), _login_url())
        if not is_admin(request.user):
            raise PermissionDenied('Доступ только для администраторов.')
        return view_func(request, *args, **kwargs)
    return _wrapped


def admin_required_json(view_func):
    """Только администратор, ответ — JSON 403 (для fetch/JS-эндпоинтов)."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403,
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def generate_uip_required_json(view_func):
    """Генерация УИП: администратор или право на запись в uip. JSON 403 иначе."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_generate_uip(request.user):
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Нет права на генерацию УИП '
                               '(требуется право на запись в модель uip).',
                },
                status=403,
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


# ==========================================
# DRF-права.
# ==========================================

class IsAppAdmin(BasePermission):
    """Администратор приложения (суперпользователь / staff / группа «Админ»)."""
    message = 'Доступ только для администраторов.'

    def has_permission(self, request, view) -> bool:
        return is_admin(request.user)


class CanGenerateUip(BasePermission):
    """Право на генерацию/резервирование УИП."""
    message = 'Нет права на генерацию УИП.'

    def has_permission(self, request, view) -> bool:
        return can_generate_uip(request.user)
