from pydantic import BaseModel


class CountryItem(BaseModel):
    """One available analysis market. `continent` groups the frontend picker;
    the list is returned ordered by (continent, name)."""

    iso3: str
    iso2: str | None
    name: str
    continent: str | None
