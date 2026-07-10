"""Fase 5 — POST /catalog/identify-image. Synchronous and lightweight (one
vision call), deliberately separate from POST /analyze (user request,
2026-07-08): the frontend shows this result and lets the user correct any
field before submitting the full analysis — never chains straight into
collection/calculation.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.catalog import IdentifyImageResponse, ImageMediaType
from app.services.catalog_service import CatalogService
from app.services.vision_client import VisionClient


class ProductIdentificationService:
    def __init__(self, session: AsyncSession, vision_client: VisionClient | None = None) -> None:
        self._session = session
        self._vision_client = vision_client or VisionClient()
        self._catalog_service = CatalogService(session)

    async def identify(
        self, *, image_base64: str, image_media_type: ImageMediaType, hint_text: str | None
    ) -> IdentifyImageResponse:
        identification = await self._vision_client.identify_product(
            image_base64=image_base64, image_media_type=image_media_type, hint_text=hint_text
        )

        resolve_text = (
            f"{identification.brand} {identification.product_name}".strip()
            if identification.brand
            else identification.product_name
        )
        resolved = await self._catalog_service.resolve(
            resolve_text,
            source="image_identification",
            category=identification.category,
            localized_terms=identification.localized_terms or None,
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
        )

        return IdentifyImageResponse(
            entity_key=resolved.entity_key,
            canonical_title=resolved.canonical_title,
            method=resolved.method,
            product_name=identification.product_name,
            brand=identification.brand,
            category=identification.category,
            localized_terms=identification.localized_terms or None,
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
            estimated_price=identification.estimated_price,
            price_currency=identification.price_currency,
            confidence=identification.confidence,
        )
