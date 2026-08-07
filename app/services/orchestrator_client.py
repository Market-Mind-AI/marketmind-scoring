import json
import uuid
import httpx
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.logging import logger
from app.repositories.analysis_repository import AnalysisRepository

async def run_via_orchestrator(session: AsyncSession, request_id: uuid.UUID) -> None:
    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.get(request_id)
    if analysis_request is None:
        logger.warning(f"AnalysisRequest {request_id} not found")
        return

    try:
        await analysis_repo.mark_running(analysis_request)
        await session.commit()

        payload = {
            "analysis_id": str(request_id),
            "product_description": analysis_request.user_prompt or "",
            "product_image_url": "",
            "attributes": {},
            "enrichment_results": [],
            "mos_score": {},
            "report": {},
            "errors": []
        }

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{settings.orchestrator_api_base_url}/run-analysis", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        # Maps orchestrator node completions to Postgres statuses if desired
                        # For now, just print or handle specific keys
                        logger.info(f"Orchestrator event: {data.keys()}")
                        
                        if "report" in data and "narrative" in data["report"]:
                            # Terminal state
                            await analysis_repo.mark_done(
                                analysis_request,
                                result=[],
                                explanation=data["report"]["narrative"],
                                breakdown={"status": "done via orchestrator"}
                            )
                            await session.commit()
                            break

    except Exception as exc:
        logger.exception(f"AnalysisRequest {request_id} failed via orchestrator")
        await session.rollback()
        failed_request = await analysis_repo.get(request_id)
        if failed_request is not None:
            await analysis_repo.mark_failed(failed_request, error_message=str(exc))
            await session.commit()
