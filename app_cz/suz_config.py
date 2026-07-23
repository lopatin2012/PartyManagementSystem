# app/suz_config.py

from enum import Enum
from django.conf import settings


class SUZEnvironment(Enum):
    """Окружение СУЗ (Честный Знак)"""
    PROD = 'prod'
    SANDBOX = 'sandbox'

class SUZUrls:
    """Единый источник URL-адресов СУЗ."""
    def __init__(self, env: SUZEnvironment):
        self.env = env
        self._setup_base_domain()
        self._setup_endpoints()

    def _setup_base_domain(self):
        """Настройка базовых доменов в зависимости от окружения."""

        if self.env == SUZEnvironment.PROD:
            self.DOMAIN_SUZ = "https://suzgrid.crpt.ru"
            self.DOMAIN_TRUE_API = "https://markirovka.crpt.ru"
            self.DOMAIN_MILK = "https://milk.crpt.ru"
        else:
            self.DOMAIN_SUZ = "https://suz.sandbox.crptech.ru"
            self.DOMAIN_TRUE_API = "https://markirovka.sandbox.crptech.ru"
            self.DOMAIN_MILK = "https://milk.sandbox.crptech.ru"

    def _setup_endpoints(self):
        """Определение путей эндпоинтов."""

        # --- Эндпоинты СУЗ (suzgrid / suz.sandbox) ---
        self.order_milk = f"{self.DOMAIN_SUZ}/api/v3/order"
        self.check_work_suz = f"{self.DOMAIN_SUZ}/api/v3/ping"
        self.check_order = f"{self.DOMAIN_SUZ}/api/v3/order/status"
        self.download_codes_from_order = f"{self.DOMAIN_SUZ}/api/v3/codes"
        self.order_close = f"{self.DOMAIN_SUZ}/api/v3/order/close"
        self.utilisation = f"{self.DOMAIN_SUZ}/api/v3/utilisation"
        self.check_status_report = f"{self.DOMAIN_SUZ}/api/v3/report/info"
        self.get_information_about_receipt = f"{self.DOMAIN_SUZ}/api/v3/receipts/receipt"
        self.get_information_about_linked_document = f"{self.DOMAIN_SUZ}/api/v3/receipts/document"

        # --- Эндпоинты True API (markirovka / markirovka.sandbox) ---
        self.true_api = f"{self.DOMAIN_TRUE_API}/api/v3/true-api"
        self.cises_search = f"{self.DOMAIN_TRUE_API}/api/v4/true-api/cises/search"
        self.generation_party = f"{self.DOMAIN_TRUE_API}/api/v3/true-api/party-numbers/generate"
        self.reservation_party = f"{self.DOMAIN_TRUE_API}/api/v3/true-api/party-numbers/reserve"
        self.get_all_reserved_party = f"{self.DOMAIN_TRUE_API}/api/v3/true-api/party-numbers/list"

        # --- Эндпоинты для получения клиентского токена (markirovka / markirovka.sandbox) ---
        self.auth_key = f"{self.true_api}/auth/key"
        self.simple_sign_in = f"{self.true_api}/auth/simpleSignIn"

        # --- Эндпоинты Молочной продукции (milk / milk.sandbox) ---
        self.create_document_milk = f"{self.DOMAIN_MILK}/lk/documents/create"

CURRENT_ENV = (
        SUZEnvironment.SANDBOX
        if settings.DEBUG
        else SUZEnvironment.PROD
)
SUZ = SUZUrls(CURRENT_ENV)
