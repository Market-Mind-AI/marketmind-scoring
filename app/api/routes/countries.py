from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.country_repository import CountryRepository
from app.schemas.countries import CountryItem

router = APIRouter()


@router.get(
    "/countries",
    response_model=list[CountryItem],
    summary="Available analysis countries",
    description=(
        "The markets available for analysis (those Google Ads can measure — see "
        "docs/paises-disponiveis.md), ordered by continent then name so the "
        "frontend can group + list them alphabetically directly from this order."
    ),
)
async def list_countries(session: AsyncSession = Depends(get_session)) -> list[CountryItem]:
    countries = await CountryRepository(session).list_all()
    return [
        CountryItem(iso3=c.iso3, iso2=c.iso2, name=c.name, continent=c.continent) for c in countries
    ]
