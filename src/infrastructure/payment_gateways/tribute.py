import hashlib
import hmac
import uuid
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


# https://tribute.tg/  (Tribute Merchant API)
#
# NOTE: Tribute exposes a REST API for creating digital-product / donation orders
# and forwards payment notifications via signed webhooks. Endpoint paths and the
# exact signature header name should be verified against the merchant's Tribute
# dashboard / API documentation. The integration scaffolding (settings, DI,
# webhook routing) is fully wired and matches the bot's gateway contract.
class TributeGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE: Final[str] = "https://tribute.tg/api/v1"
    SIGNATURE_HEADER: Final[str] = "trbt-signature"

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
        order_id = str(uuid.uuid4())
        payload = await self._create_payment_payload(amount, details, order_id)
        logger.debug(f"Creating payment payload: {payload}")

        try:
            response = await self._client.post("/orders", json=payload)
            response.raise_for_status()
            data = orjson.loads(response.content)
            return self._get_payment_data(data, order_id)

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

        # Tribute wraps payload under "payload" with an event "name"; we accept
        # either flat or wrapped shape to be tolerant of API variations.
        event_name = webhook_data.get("name") or webhook_data.get("event")
        payload: dict = webhook_data.get("payload", webhook_data)

        order_id_str = (
            payload.get("order_id")
            or payload.get("external_id")
            or payload.get("orderId")
        )
        if not order_id_str:
            raise ValueError("Required field 'order_id' is missing in Tribute webhook")

        try:
            payment_id = UUID(order_id_str)
        except ValueError as e:
            raise ValueError(f"Invalid order_id UUID: '{order_id_str}'") from e

        status = (payload.get("status") or event_name or "").lower()

        match status:
            case "paid" | "succeeded" | "completed" | "new_payment":
                transaction_status = TransactionStatus.COMPLETED
            case "canceled" | "cancelled" | "expired" | "failed" | "declined":
                transaction_status = TransactionStatus.CANCELED
            case _:
                raise ValueError(f"Unsupported Tribute event/status: '{status}'")

        return payment_id, transaction_status

    async def _create_payment_payload(
        self,
        amount: Decimal,
        details: str,
        order_id: str,
    ) -> dict[str, Any]:
        redirect_url = await self._get_bot_redirect_url()
        return {
            "order_id": order_id,
            "amount": float(amount),
            "currency": self.data.currency.value,
            "description": details,
            "success_url": redirect_url,
            "fail_url": redirect_url,
        }

    def _get_payment_data(self, data: dict[str, Any], order_id: str) -> PaymentResultDto:
        # Tribute responses commonly nest result under "data"; fall back to flat shape.
        result = data.get("data") if isinstance(data.get("data"), dict) else data
        payment_url = (
            result.get("payment_url")
            or result.get("url")
            or result.get("checkout_url")
        )
        if not payment_url:
            raise KeyError("Invalid response from Tribute API: missing payment URL")

        return PaymentResultDto(id=UUID(order_id), url=str(payment_url))

    def _verify_webhook(self, request: Request, raw_body: bytes) -> bool:
        signature = request.headers.get(self.SIGNATURE_HEADER) or request.headers.get(
            "X-Signature"
        )
        if not signature:
            logger.warning(f"Webhook is missing '{self.SIGNATURE_HEADER}' header")
            return False

        webhook_secret = self.data.settings.webhook_secret  # type: ignore[union-attr]
        if not webhook_secret:
            logger.warning("Tribute webhook_secret is not configured")
            return False

        secret = webhook_secret.get_secret_value().encode()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Tribute webhook signature")
            return False

        return True
