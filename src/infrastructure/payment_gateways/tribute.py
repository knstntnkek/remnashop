import hashlib
import hmac
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import orjson
from aiogram import Bot
from fastapi import Request
from httpx import AsyncClient, HTTPStatusError
from loguru import logger

from src.application.dto import PaymentGatewayDto, PaymentResultDto
from src.application.dto.payment_gateway import TributeGatewaySettingsDto
from src.core.config import AppConfig
from src.core.enums import TransactionStatus

from .base import BasePaymentGateway


# Tribute Shop API:
#   docs:           https://wiki.tribute.tg/for-shops/api
#   methods:        https://wiki.tribute.tg/for-shops/api/methods
#   webhooks:       https://wiki.tribute.tg/for-shops/api/webhooks
#   webhook auth:   trbt-signature = HMAC-SHA256(raw_body, key=api_key)
class TributeGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE: Final[str] = "https://tribute.tg/api/v1"
    SIGNATURE_HEADER: Final[str] = "trbt-signature"

    # Tribute amounts are expressed in the smallest currency unit
    # (kopecks for RUB, cents for EUR/USD).
    _MINOR_UNITS_PER_MAJOR: Final[int] = 100

    # Tribute string field limits (UTF-16 code units, see Methods docs).
    _NAME_MAX_LEN: Final[int] = 100
    _DESCRIPTION_MAX_LEN: Final[int] = 300

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(self.data.settings, TributeGatewaySettingsDto):
            raise TypeError(
                f"Invalid settings type: expected {TributeGatewaySettingsDto.__name__}, "
                f"got {type(self.data.settings).__name__}"
            )

        self._client = self._make_client(
            base_url=self.API_BASE,
            headers={
                "Api-Key": self.data.settings.api_key.get_secret_value(),  # type: ignore[union-attr]
                "Content-Type": "application/json",
            },
        )

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        payload = await self._create_payment_payload(amount, details)
        logger.debug(f"Creating payment payload: {payload}")

        try:
            response = await self._client.post("/shop/orders", json=payload)
            response.raise_for_status()
            data = orjson.loads(response.content)
            return self._get_payment_data(data)

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error creating payment. "
                f"Status: '{e.response.status_code}', Body: {e.response.text}"
            )
            raise
        except (KeyError, orjson.JSONDecodeError) as e:
            logger.error(f"Failed to parse response. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"An unexpected error occurred while creating payment: {e}")
            raise

    async def handle_webhook(self, request: Request) -> tuple[UUID, TransactionStatus]:
        logger.debug(f"Received {self.__class__.__name__} webhook request")

        raw_body = await request.body()

        if not self._verify_webhook(request, raw_body):
            raise PermissionError("Webhook verification failed")

        webhook_data = orjson.loads(raw_body)

        event_name = webhook_data.get("name", "")
        payload: dict = webhook_data.get("payload") or {}

        # 'shop_order_payment_received' is an intermediate signal — funds have
        # not been credited yet. The terminal event for one-time orders is
        # 'shop_order' (with status=paid|failed).
        if event_name == "shop_order_payment_received":
            raise ValueError("Intermediate event 'shop_order_payment_received' ignored")

        order_uuid_str = payload.get("uuid")
        if not order_uuid_str:
            raise ValueError("Required field 'payload.uuid' is missing in Tribute webhook")

        try:
            payment_id = UUID(order_uuid_str)
        except ValueError as e:
            raise ValueError(f"Invalid order uuid: '{order_uuid_str}'") from e

        order_status = (payload.get("status") or "").lower()

        if event_name == "shop_order" and order_status == "paid":
            transaction_status = TransactionStatus.COMPLETED
        elif event_name in {
            "shop_order_payment_failed",
            "shop_order_cancelled",
        } or order_status in {"failed", "cancelled", "canceled"}:
            transaction_status = TransactionStatus.CANCELED
        else:
            raise ValueError(
                f"Unsupported Tribute event '{event_name}' with status '{order_status}'"
            )

        return payment_id, transaction_status

    async def _create_payment_payload(
        self,
        amount: Decimal,
        details: str,
    ) -> dict[str, Any]:
        redirect_url = await self._get_bot_redirect_url()
        minor_amount = int((amount * self._MINOR_UNITS_PER_MAJOR).to_integral_value())
        return {
            "amount": minor_amount,
            "currency": self.data.currency.value.lower(),
            "name": details[: self._NAME_MAX_LEN],
            "description": details[: self._DESCRIPTION_MAX_LEN],
            "successUrl": redirect_url,
            "failUrl": redirect_url,
            "period": "onetime",
        }

    def _get_payment_data(self, data: dict[str, Any]) -> PaymentResultDto:
        order_uuid = data.get("id")
        if not order_uuid:
            raise KeyError("Invalid response from Tribute API: missing 'id'")

        payment_url = data.get("webPaymentUrl") or data.get("webappPaymentUrl")
        if not payment_url:
            raise KeyError(
                "Invalid response from Tribute API: missing 'webPaymentUrl'/'webappPaymentUrl'"
            )

        return PaymentResultDto(id=UUID(str(order_uuid)), url=str(payment_url))

    def _verify_webhook(self, request: Request, raw_body: bytes) -> bool:
        signature = request.headers.get(self.SIGNATURE_HEADER)
        if not signature:
            logger.warning(f"Webhook is missing '{self.SIGNATURE_HEADER}' header")
            return False

        api_key = self.data.settings.api_key.get_secret_value()  # type: ignore[union-attr]
        expected = hmac.new(api_key.encode(), raw_body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Tribute webhook signature")
            return False

        return True
