# app_cz/services/suz_client.py

import json
import logging
from datetime import timedelta
import uuid

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from app_helper import external_http
from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ

from app_helper.sign_helper import attached_signed_data, unpinned_signed_data

logger = logging.getLogger(__name__)

# Единый UUID-токен True API действует 10 часов. Храним его в SUZAccount и
# переиспользуем: ЧЗ разрешает не более 1000 действующих токенов на
# пользователя, поэтому запрашивать новый на каждую операцию нельзя.
TRUE_API_TOKEN_TTL = timedelta(hours=10)
TRUE_API_TOKEN_TTL_MARGIN = timedelta(minutes=30)
TRUE_API_TOKEN_REFRESH_THRESHOLD = timedelta(minutes=30)


def true_api_token_is_valid(account) -> bool:
    """Токен из БД ещё можно использовать (не истекает в ближайшие 30 мин)."""
    if (
        not account
        or not account.true_api_token
        or not account.true_api_token_expires_at
    ):
        return False
    return (
        account.true_api_token_expires_at
        > timezone.now() + TRUE_API_TOKEN_REFRESH_THRESHOLD
    )


def true_api_token_expires_at(payload: dict, now=None):
    """Срок хранения токена: expireDate из ответа, иначе 10 часов с запасом."""
    now = now or timezone.now()
    parsed = None
    raw = (payload or {}).get('expireDate')
    if raw:
        parsed = parse_datetime(str(raw))
        if parsed is not None:
            if settings.USE_TZ and timezone.is_naive(parsed):
                parsed = timezone.make_aware(
                    parsed, timezone.get_current_timezone()
                )
            elif not settings.USE_TZ and timezone.is_aware(parsed):
                parsed = timezone.make_naive(
                    parsed, timezone.get_current_timezone()
                )

    ttl_end = parsed or (now + TRUE_API_TOKEN_TTL)
    ttl_end = min(ttl_end, now + TRUE_API_TOKEN_TTL)
    expires_at = ttl_end - TRUE_API_TOKEN_TTL_MARGIN
    if expires_at <= now:
        expires_at = now + timedelta(minutes=5)
    return expires_at


def get_true_api_auth_key() -> dict:
    """
    Получает uuid и data для последующей подписи и получения токена.
    :return: Словарь {'uuid': '...', 'data': '...'}
    :raises Exception: Если запрос к TrueAPI не удался
    """
    try:
        response = external_http.get('suz', SUZ.auth_key, timeout=10)

        response.raise_for_status()

        return response.json()

    except requests.exceptions.Timeout:
        logger.error("Превышено время ожидания ответа от TrueAPI (auth_key)")
        raise Exception("Сервис Честного Знака не отвечает. Попробуйте позже.")

    except requests.exceptions.HTTPError as e:
        detail = (
            _extract_true_api_error(e.response)
            if e.response is not None else str(e)
        )
        logger.error(f"HTTP ошибка при запросе auth_key: {detail}")
        raise Exception(f"Не удалось получить ключ авторизации ЧЗ: {detail}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при запросе auth_key: {e}")
        raise Exception("Не удалось соединиться с сервисом Честного Знака.")

    except ValueError:
        logger.error("Некорректный JSON в ответе от TrueAPI")
        raise Exception("Сервер Честного Знака вернул некорректные данные.")


def get_true_api_session_token(force_refresh: bool = False) -> dict:
    """
    Возвращает действующий единый токен True API (формат UUID).

    Токен кэшируется в активном SUZAccount и переиспользуется до истечения:
    ЧЗ считает каждый выданный токен действующим 10 часов и разрешает не
    более 1000 действующих токенов на пользователя.

    Контракт прежний: функция НИКОГДА не бросает исключение — при любой
    ошибке возвращает ``{'token': None, 'message': <причина>}``.

    :return: {'uuid': str|None, 'token': str|None, 'message': str}
    """
    account = SUZAccount.objects.filter(is_active=True).first()
    if account is None:
        return {
            'uuid': None,
            'token': None,
            'message': 'Активная учётная запись СУЗ не найдена.',
        }

    if not force_refresh and true_api_token_is_valid(account):
        return {
            'uuid': None,
            'token': account.true_api_token,
            'message': 'Единый токен True API из кэша.',
        }

    try:
        result = _refresh_true_api_session_token(
            force_refresh=force_refresh
        )
    except Exception as e:
        logger.exception('Ошибка обновления единого токена True API')
        result = {'uuid': None, 'token': None, 'message': str(e)}

    # Обновление не удалось, но у текущей активной записи ещё есть
    # действующий токен — не роняем операцию из-за временного сбоя.
    if not result.get('token'):
        current = SUZAccount.objects.filter(is_active=True).first()
        if (
            current
            and current.true_api_token
            and current.true_api_token_expires_at
            and current.true_api_token_expires_at > timezone.now()
        ):
            return {
                'uuid': result.get('uuid'),
                'token': current.true_api_token,
                'message': (
                    'Не удалось обновить токен True API, используется '
                    'действующий токен из кэша.'
                ),
            }
    return result


def _refresh_true_api_session_token(force_refresh: bool = False) -> dict:
    """
    Получает новый единый токен и сохраняет его в SUZAccount.

    Строка активной учётной записи блокируется на время запроса
    (`SELECT FOR UPDATE`), чтобы веб, планировщик и воркер не запросили
    несколько токенов одновременно.
    """
    with transaction.atomic():
        account = (
            SUZAccount.objects.select_for_update()
            .filter(is_active=True)
            .first()
        )
        if account is None:
            return {
                'uuid': None,
                'token': None,
                'message': 'Активная учётная запись СУЗ не найдена.',
            }

        # Пока ждали блокировку, токен мог обновить другой процесс.
        if not force_refresh and true_api_token_is_valid(account):
            return {
                'uuid': None,
                'token': account.true_api_token,
                'message': 'Единый токен True API из кэша.',
            }

        # 1. Ключ для подписи (GET /auth/key).
        try:
            auth_data = get_true_api_auth_key()
            row_uuid = auth_data['uuid']
            row_data = auth_data['data']
        except Exception as e:
            logger.error(f"Не удалось получить auth_key TrueAPI: {e}")
            return {'uuid': None, 'token': None, 'message': str(e)}

        # 2. Подпись данных (прикреплённая).
        try:
            _, signed_data = attached_signed_data(row_data)
        except Exception as e:
            logger.error(f"Не удалось подписать данные для TrueAPI: {e}")
            return {'uuid': row_uuid, 'token': None, 'message': str(e)}

        if not signed_data:
            return {
                'uuid': row_uuid,
                'token': None,
                'message': 'Сервис подписей не вернул подпись. Проверьте сертификат.',
            }

        # 3. simpleSignIn → токен. При unitedToken ЧЗ возвращает `uuidToken`
        #    (как в «Молвест.Маркировка»), поэтому читаем его ПЕРВЫМ.
        url = SUZ.simple_sign_in
        payload = {
            'uuid': row_uuid,
            'data': signed_data,
            'inn': account.inn,
            'unitedToken': True,
        }
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        try:
            response = external_http.post(
                'suz', url, json=payload, headers=headers, timeout=15
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Сетевая ошибка при получении токена TrueAPI: {e}")
            return {
                'uuid': row_uuid,
                'token': None,
                'message': f'Ошибка соединения с TrueAPI: {e}',
            }

        if response.status_code != 200:
            detail = _extract_true_api_error(response)
            logger.error(
                f"TrueAPI simpleSignIn вернул {response.status_code}: {detail}"
            )
            return {'uuid': row_uuid, 'token': None, 'message': detail}

        try:
            result = response.json()
        except ValueError:
            return {
                'uuid': row_uuid,
                'token': None,
                'message': (
                    'TrueAPI вернул некорректный ответ: '
                    f'{response.text[:200]}'
                ),
            }

        # При unitedToken=True ЧЗ отдаёт uuidToken.
        token = result.get('uuidToken') or result.get('token')
        if not token:
            return {
                'uuid': row_uuid,
                'token': None,
                'message': (
                    'TrueAPI не вернул токен (uuidToken/token). '
                    f'Ответ: {str(result)[:200]}'
                ),
            }

        # 4. Кэшируем до expireDate, чтобы не запрашивать новый токен на
        #    каждую операцию (лимит ЧЗ — 1000 действующих токенов).
        expires_at = true_api_token_expires_at(result)
        SUZAccount.objects.filter(pk=account.pk).update(
            true_api_token=token,
            true_api_token_expires_at=expires_at,
            updated_at=timezone.now(),
        )
        account.true_api_token = token
        account.true_api_token_expires_at = expires_at

        logger.info(
            'Единый токен True API получен и сохранён до '
            f'{expires_at:%Y-%m-%d %H:%M}'
        )
        return {
            'uuid': row_uuid,
            'token': token,
            'message': 'Токен успешно получен.',
        }


def _stringify_error_value(value) -> str:
    """Строка из поля ошибки TrueAPI (строка/список/словарь)."""
    if isinstance(value, (list, tuple)):
        parts = [_stringify_error_value(item) for item in value if item]
        return '; '.join(part for part in parts if part)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _extract_true_api_error(response) -> str:
    """Человекочитаемое сообщение об ошибке из ответа TrueAPI."""
    text = (response.text or '').strip()
    data = None
    if text:
        try:
            data = response.json()
        except ValueError:
            data = None

    detail = ''
    if isinstance(data, dict):
        for key in (
            'error_message', 'errorMessage', 'message',
            'error_description', 'description', 'error', 'errors',
            'errorResult',
        ):
            value = data.get(key)
            if value:
                detail = _stringify_error_value(value)
                break
        code = data.get('error_code') or data.get('code')
        if code:
            detail = (
                f'{detail} (код {code})' if detail else f'код {code}'
            )

    if not detail:
        detail = text[:300]
    if len(detail) > 300:
        detail = detail[:300]

    return f'HTTP {response.status_code}: {detail}' if detail else f'HTTP {response.status_code}'


def get_true_api_dynamic_token(row_uuid: str, signed_data: str) -> str:
    """
    Получает динамический токен для работы с СУЗ (unitedToken=True).
    """
    try:
        account = SUZAccount.objects.filter(is_active=True).first()
        if not account:
            raise ValueError("Активная учётная запись СУЗ не найдена")

        # Формируем URL с идентификатором соединения
        url = f"{SUZ.simple_sign_in}/{account.connection_identifier}"

        payload = {
            'uuid': row_uuid,
            'data': signed_data,
            'inn': account.inn,
            'unitedToken': True
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "omsConnection": account.connection_identifier
        }

        response = external_http.post(
            'suz',
            url,
            json=payload,
            headers=headers,
            timeout=15
        )

        response.raise_for_status()

        result = response.json()
        token = result.get('token')

        if not token:
            raise ValueError("Динамический токен отсутствует в ответе сервера")

        logger.info("Динамический токен TrueAPI успешно получен")
        return token

    except Exception as e:
        logger.error(f"Ошибка получения динамического токена TrueAPI: {e}")
        raise


# Сколько попыток обновления динамического токена делаем за один вызов.
SUZ_TOKEN_REFRESH_ATTEMPTS = 3

# Порог «пора обновить»: если до истечения токена осталось меньше этого
# времени — обновляем заранее.
SUZ_TOKEN_REFRESH_THRESHOLD = timedelta(hours=1)


def _suz_dynamic_token_valid(
        account, min_remaining: timedelta = None,
) -> bool:
    """Динамический токен СУЗ ещё можно использовать."""
    if not account or not account.dynamic_token or not account.token_expires_at:
        return False
    min_remaining = min_remaining or SUZ_TOKEN_REFRESH_THRESHOLD
    return account.token_expires_at > timezone.now() + min_remaining


def _refresh_suz_dynamic_token_once(force: bool = False) -> bool:
    """
    Одна попытка полного цикла обновления динамического токена СУЗ.

    Строка учётной записи блокируется на время запроса, чтобы веб, планировщик
    и воркер не выдали по токену одновременно.

    :return: True при успехе, False при неудаче (исключения не пробрасывает).
    """
    try:
        with transaction.atomic():
            account = (
                SUZAccount.objects.select_for_update()
                .filter(is_active=True)
                .first()
            )
            if not account:
                logger.error(
                    "Активная учётная запись СУЗ не найдена для обновления токена"
                )
                return False

            # Пока ждали блокировку, токен мог обновить другой процесс.
            if not force and _suz_dynamic_token_valid(account):
                return True

            # 1. Получаем ключи для подписи.
            auth_data = get_true_api_auth_key()
            row_uuid = auth_data['uuid']
            row_data = auth_data['data']

            # 2. Подписываем данные (прикреплённая подпись).
            _, signed_data = attached_signed_data(row_data)

            if not signed_data:
                logger.error("Сервис подписей не вернул подпись для токена СУЗ")
                return False

            # 3. Получаем динамический токен.
            dynamic_token = get_true_api_dynamic_token(row_uuid, signed_data)

            # Проверка корректности токена (само значение в лог не пишем).
            try:
                uuid.UUID(dynamic_token)
            except (ValueError, TypeError, AttributeError):
                logger.error(
                    "Получен токен СУЗ не в формате UUID — не сохраняем"
                )
                return False

            # 4. Сохраняем в БД (queryset.update — без побочного save()).
            expires_at = timezone.now() + timedelta(hours=8)
            SUZAccount.objects.filter(pk=account.pk).update(
                dynamic_token=dynamic_token,
                token_expires_at=expires_at,
                updated_at=timezone.now(),
            )

            logger.info(
                f"Динамический токен успешно обновлён и сохранён "
                f"для {account.certificate_name}"
            )
            return True

    except Exception as e:
        logger.error(f"Ошибка попытки обновления токена СУЗ: {e}")
        return False


def refresh_suz_dynamic_token(
        max_attempts: int = None, force: bool = False,
) -> bool:
    """
    Полный цикл обновления динамического токена СУЗ с повторами.

    При неудаче повторяем до `max_attempts` раз (по умолчанию
    SUZ_TOKEN_REFRESH_ATTEMPTS = 3). После исчерпания попыток больше не
    пробуем и возвращаем False. При `force=True` обновляем токен, даже если
    он ещё действует (кнопка «Обновить»).

    :return: True при успехе, False при неудаче.
    """
    if max_attempts is None:
        max_attempts = SUZ_TOKEN_REFRESH_ATTEMPTS
    max_attempts = max(1, int(max_attempts))

    for attempt in range(1, max_attempts + 1):
        if _refresh_suz_dynamic_token_once(force=force):
            return True
        logger.warning(
            f"Обновление токена СУЗ: попытка {attempt}/{max_attempts} "
            f"не удалась"
        )

    logger.error(
        f"Не удалось обновить динамический токен СУЗ за {max_attempts} попыток"
    )
    return False


def ensure_suz_token_valid(
        min_remaining: timedelta = None,
        max_attempts: int = None,
) -> dict:
    """
    Гарантирует, что динамический токен СУЗ не истечёт в ближайшее время.

    Если до истечения осталось меньше `min_remaining` (по умолчанию
    SUZ_TOKEN_REFRESH_THRESHOLD = 1 час) — обновляем заранее, с повторами.

    :return: {'refreshed': bool, 'skipped': bool, 'message': str}
    """
    if min_remaining is None:
        min_remaining = SUZ_TOKEN_REFRESH_THRESHOLD

    account = SUZAccount.objects.filter(is_active=True).first()
    if not account:
        return {
            'refreshed': False,
            'skipped': True,
            'message': 'Активная учётная запись СУЗ не найдена.',
        }

    now = timezone.now()
    if account.dynamic_token and account.token_expires_at:
        if account.token_expires_at > now + min_remaining:
            return {
                'refreshed': False,
                'skipped': True,
                'message': (
                    f'Токен действителен до '
                    f'{account.token_expires_at:%Y-%m-%d %H:%M}. '
                    f'Обновление не требуется.'
                ),
            }

    ok = refresh_suz_dynamic_token(max_attempts=max_attempts)
    return {
        'refreshed': ok,
        'skipped': False,
        'message': (
            'Динамический токен СУЗ обновлён заранее.'
            if ok
            else 'Не удалось обновить динамический токен СУЗ.'
        ),
    }
