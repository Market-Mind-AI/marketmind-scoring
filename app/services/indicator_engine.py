"""Fase 3: Cap. 4 orchestration. For a given (country, optional entity,
period), computes every applicable indicator's raw value, normalizes it,
and upserts into indicator_values. Never computes MOS (Fase 4) — this is
purely the indicator layer, one function per Cap. 4 indicator.

Country-level indicators (GDP, Internet, Mobile, GDP Growth, Population,
Urbanization, Inflation, Population Growth, Net Migration — Cap. 4 "país-a-
país product-agnostic") are always computed with entity_key=NULL and are
shared across every product for that country+period. Product-specific
indicators (Trend/Growth/Demand Stability/Regional Demand/Search Intent,
plus the conditional Median Age Fit) only run when an entity_key is given.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.core.logging import logger
from app.db.models.country import Country
from app.db.models.indicator_value import IndicatorValue
from app.db.models.product_catalog import ProductCatalog
from app.domain.enums import (
    AvailabilityStatus,
    DataSource,
    IndicatorCode,
    IndicatorDirection,
    NormalizationMethod,
    PeriodType,
)
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.metric_explanation_repository import MetricExplanationRepository
from app.repositories.product_repository import ProductRepository
from app.services.collection_client import resolve_source_country_code
from app.services.indicators import oecd as oecd_indicators
from app.services.indicators import pytrends as pytrends_indicators
from app.services.indicators import undata as undata_indicators
from app.services.indicators import worldbank as worldbank_indicators
from app.services.indicators.normalization import (
    FIXED_BOUNDS_PERIOD_KEY,
    normalization_method,
    normalize,
    normalize_demand_volume,
    self_widens,
)
from app.services.indicators.results import RawIndicatorResult
from app.services.mos.interpretation import INDICATOR_MEANING, score_band
from app.utils.period import current_month_period_key
from app.utils.text import as_term_list


def _com_share_da_marca(categoria: RawIndicatorResult, *, brand_raw: RawIndicatorResult) -> RawIndicatorResult:
    """Acrescenta o share da marca dentro da categoria à nota do indicador de
    contexto — determinístico, calculado aqui, nunca pedido ao LLM.

    É a frase que a narrativa precisa para separar "mercado não existe" de
    "mercado existe e a marca não está nele" (decisão D2: contexto, nunca sinal
    pontuado). O share compara o volume da MARCA com o volume dos termos
    GENÉRICOS da categoria — não com a demanda total do mercado, que ninguém
    mede —, e a nota diz isso com os dois números à vista para o leitor poder
    conferir.
    """
    if (
        categoria.availability_status != AvailabilityStatus.AVAILABLE
        or categoria.raw_value is None
        or brand_raw.availability_status != AvailabilityStatus.AVAILABLE
        or brand_raw.raw_value is None
        or float(categoria.raw_value) <= 0
    ):
        return categoria
    volume_marca, volume_categoria = float(brand_raw.raw_value), float(categoria.raw_value)
    razao = volume_marca / volume_categoria
    # NÃO é "share da categoria", e a diferença não é cosmética: o denominador
    # são os termos GENÉRICOS da categoria, não a demanda total do mercado
    # (que incluiria as outras marcas e ninguém mede). Medido ao vivo em 27/08,
    # a Cardhu na Espanha tem 9.630 buscas contra 3.140 dos termos genéricos —
    # chamar isso de "share de 306%" seria uma frase falsa saindo de um número
    # correto. A marca simplesmente é procurada mais que a categoria em abstrato.
    if razao < 1:
        comparacao = f"the brand is {razao * 100:.0f}% of that ({volume_marca:,.0f} vs {volume_categoria:,.0f})"
    else:
        comparacao = (
            f"the brand is searched {razao:.1f}x MORE than the generic category terms "
            f"({volume_marca:,.0f} vs {volume_categoria:,.0f})"
        )
    return RawIndicatorResult(
        categoria.raw_value,
        categoria.direction,
        categoria.calculation_window,
        categoria.availability_status,
        f"{categoria.notes} for the category's GENERIC terms; {comparacao} — a small fraction of a large "
        f"category means the market exists and this brand is not in it, not that demand is absent",
    )


class IndicatorEngine:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)
        self._country_repo = CountryRepository(session)
        self._product_repo = ProductRepository(session)
        self._explanation_repo = MetricExplanationRepository(session)

    async def compute_all(
        self,
        *,
        country_iso3: str,
        entity_key: uuid.UUID | None = None,
        period_key: str | None = None,
        demand_ceiling: float | None = None,
        terms_override: list[str] | None = None,
    ) -> list[IndicatorValue]:
        resolved_period_key = period_key or current_month_period_key()
        country = await self._country_repo.get_by_iso3(country_iso3)
        if country is None:
            raise MarketMindCoreError(f"Unknown country {country_iso3} (not in canonical countries table)")

        results = await self._compute_country_level_indicators(country, resolved_period_key)

        if entity_key is not None:
            entity = await self._product_repo.get_by_entity_key(entity_key)
            if entity is None:
                raise EntityNotFoundError(f"Entity {entity_key} not found")
            results += await self._compute_pytrends_indicators(
                entity, country, resolved_period_key, demand_ceiling=demand_ceiling, terms_override=terms_override
            )
            results.append(await self._compute_median_age_fit(entity, country, resolved_period_key))

        await self._session.commit()
        return results

    async def _compute_country_level_indicators(self, country: Country, period_key: str) -> list[IndicatorValue]:
        if country.iso2 is None:
            logger.warning(f"Country {country.iso3} has no ISO2 mapping — country-level indicators skipped")
            missing = RawIndicatorResult(
                None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.UNAVAILABLE,
                f"country {country.iso3} has no iso2 mapping",
            )  # fmt: skip
            codes = [
                IndicatorCode.GDP_PER_CAPITA_SCORE, IndicatorCode.INTERNET_PENETRATION_SCORE,
                IndicatorCode.MOBILE_CONNECTIVITY_SCORE, IndicatorCode.GDP_GROWTH_SCORE,
                IndicatorCode.POPULATION_SCALE_SCORE, IndicatorCode.URBANIZATION_SCORE,
                IndicatorCode.POPULATION_GROWTH_SCORE, IndicatorCode.NET_MIGRATION_RATE_SCORE,
                IndicatorCode.WORLDBANK_INFLATION_SCORE, IndicatorCode.CONSUMER_SPENDING_SCORE,
                IndicatorCode.INVESTMENT_SCORE, IndicatorCode.NET_EXPORTS_SCORE,
                IndicatorCode.TRADE_OPENNESS_SCORE, IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
                IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
            ]  # fmt: skip
            return [
                await self._normalize_and_save(code, DataSource.WORLDBANK, None, country.iso3, period_key, missing)
                for code in codes
            ]

        wb_iso2 = country.iso2
        un_iso2 = country.iso2
        oecd_code = resolve_source_country_code(DataSource.OECD, country)

        gdp_per_capita = await worldbank_indicators.compute_gdp_per_capita(wb_iso2)
        internet = await worldbank_indicators.compute_internet_penetration(wb_iso2)
        mobile = await worldbank_indicators.compute_mobile_connectivity(wb_iso2)
        gdp_growth = await worldbank_indicators.compute_gdp_growth(wb_iso2)
        population = await worldbank_indicators.compute_population_scale(wb_iso2)
        urbanization = await worldbank_indicators.compute_urbanization(wb_iso2)
        population_growth = await undata_indicators.compute_population_growth(un_iso2)
        net_migration = await undata_indicators.compute_net_migration_rate(un_iso2)

        # Macro Demand Structure (2026-07-08, GDP = C+I+G+NX identity) +
        # Real Estate Growth (Economic Readiness, conditional/OECD-only).
        consumer_spending = await worldbank_indicators.compute_consumer_spending(wb_iso2)
        government_expenditure = await worldbank_indicators.compute_government_expenditure(wb_iso2)
        investment = await worldbank_indicators.compute_investment(wb_iso2)
        net_exports = await worldbank_indicators.compute_net_exports(wb_iso2)
        trade_openness = await worldbank_indicators.compute_trade_openness(wb_iso2)
        real_estate_growth = await oecd_indicators.compute_real_estate_growth(oecd_code)

        raws: list[tuple[IndicatorCode, DataSource, RawIndicatorResult]] = [
            (IndicatorCode.GDP_PER_CAPITA_SCORE, DataSource.WORLDBANK, gdp_per_capita),
            (IndicatorCode.INTERNET_PENETRATION_SCORE, DataSource.WORLDBANK, internet),
            (IndicatorCode.MOBILE_CONNECTIVITY_SCORE, DataSource.WORLDBANK, mobile),
            (IndicatorCode.GDP_GROWTH_SCORE, DataSource.WORLDBANK, gdp_growth),
            (IndicatorCode.POPULATION_SCALE_SCORE, DataSource.WORLDBANK, population),
            (IndicatorCode.URBANIZATION_SCORE, DataSource.WORLDBANK, urbanization),
            (IndicatorCode.POPULATION_GROWTH_SCORE, DataSource.UNDATA, population_growth),
            (IndicatorCode.NET_MIGRATION_RATE_SCORE, DataSource.UNDATA, net_migration),
            (IndicatorCode.CONSUMER_SPENDING_SCORE, DataSource.WORLDBANK, consumer_spending),
            (IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE, DataSource.WORLDBANK, government_expenditure),
            (IndicatorCode.INVESTMENT_SCORE, DataSource.WORLDBANK, investment),
            (IndicatorCode.NET_EXPORTS_SCORE, DataSource.WORLDBANK, net_exports),
            (IndicatorCode.TRADE_OPENNESS_SCORE, DataSource.WORLDBANK, trade_openness),
            (IndicatorCode.REAL_ESTATE_GROWTH_SCORE, DataSource.OECD, real_estate_growth),
        ]

        # Inflation: World Bank only (2026-07-08) — OECD's CPI series was
        # removed as a source. marketmind-oecd doesn't persist the
        # EXPENDITURE/TRANSFORMATION/ADJUSTMENT dimensions Cap. 4 needs to
        # isolate a clean annual-growth series, so `measure='CPI'` alone
        # was averaging several unrelated rebased sub-indices together
        # (confirmed live: same time_period, wildly different values —
        # e.g. Spain "124% inflation" was actually a mix of index levels,
        # not a real annual rate). See marketmind_mos_engine.
        inflation = await worldbank_indicators.compute_inflation(wb_iso2)
        raws.append((IndicatorCode.WORLDBANK_INFLATION_SCORE, DataSource.WORLDBANK, inflation))

        return [
            await self._normalize_and_save(code, source, None, country.iso3, period_key, raw)
            for code, source, raw in raws
        ]

    async def _compute_pytrends_indicators(
        self,
        entity: ProductCatalog,
        country: Country,
        period_key: str,
        demand_ceiling: float | None = None,
        terms_override: list[str] | None = None,
    ) -> list[IndicatorValue]:
        # Demand from Google Ads ABSOLUTE search volume (not Trends), 2026-07-15:
        # each country is measured with its OWN user-approved search terms
        # (product_catalog.localized_markets[ISO3]). LEVEL = SUM of those terms'
        # monthly search volume (real total online search demand);
        # growth/stability from the summed monthly series; intent from the
        # related keywords. location is the ISO2 code Google Ads expects.
        # `terms_override` scores a reference-candidate country (not in
        # localized_markets) with the SAME reference terms its demand was
        # collected under — used only for the off-request "champion" benchmark.
        location = country.iso2 or resolve_source_country_code(DataSource.PYTRENDS, country)
        language = country.search_language
        if terms_override:
            search_terms = as_term_list(terms_override)
        else:
            market = (entity.localized_markets or {}).get(country.iso3) or {}
            search_terms = as_term_list(market.get("search_terms")) or as_term_list(market.get("primary_name"))
        # A CATEGORIA do produto, medida ao lado da marca (decisão D4). Os termos
        # genéricos vêm da entidade-família (`generic_entity_key`), que o
        # enriquecimento preenche com `brand=None` — nenhum deles carrega marca.
        #
        # Custo: zero de coleta nova. Os dois conjuntos viajam na MESMA request
        # de `search_volume` (que aceita 1000 keywords por task) e dividem os 20
        # seeds de `keywords_for_keywords` (14/6, ver build_seed_terms).
        #
        # `terms_override` fica de fora: são os países candidatos pontuados só
        # para o benchmark do campeão, e a categoria ali não seria usada.
        category_terms: list[str] = []
        if entity.generic_entity_key is not None and not terms_override:
            familia = await self._product_repo.get_by_entity_key(entity.generic_entity_key)
            if familia is not None:
                mercado_familia = (familia.localized_markets or {}).get(country.iso3) or {}
                category_terms = as_term_list(mercado_familia.get("search_terms")) or as_term_list(
                    mercado_familia.get("primary_name")
                )

        seed_terms = pytrends_indicators.build_seed_terms(search_terms, category_terms)
        # Portão de relevância da intenção, ligado SÓ quando os seeds atendem os
        # dois assuntos: sem ele, as related genéricas comporiam a intenção de
        # compra da marca. Ver compute_search_intent_score.
        intent_relevantes = search_terms if category_terms else None
        # Veto cruzado de marcas: as keywords relacionadas que o Google devolve
        # para os termos desta marca incluem as concorrentes, e o filtro de
        # relevância da expansão não as pega quando a keyword carrega ao mesmo
        # tempo um termo aprovado e outra marca (`glenfiddich 12 vs cardhu 12`).
        # Vazio para produto sem marca, onde consulta de marca é demanda
        # legítima da categoria. Ver ProductRepository.list_other_brand_names.
        outras_marcas = await self._product_repo.list_other_brand_names(entity.brand_key)

        trend = await pytrends_indicators.compute_trend_score(
            search_terms, location, period_key, seed_terms=seed_terms, language=language, outras_marcas=outras_marcas
        )
        # Decision context for the LLM narrative (not scored): pair the absolute
        # demand VOLUME with population so it can say WHETHER the volume comes
        # from sheer population or from genuine appetite. Penetration = searches
        # per 100k inhabitants — high volume + high penetration = big AND
        # enthusiastic; high volume + low penetration = big only because
        # populous; low volume + high penetration = small but strong appetite.
        if trend.availability_status == AvailabilityStatus.AVAILABLE and trend.raw_value is not None:
            pop = await worldbank_indicators.population(country.iso2) if country.iso2 else None
            if pop:
                penetration = float(trend.raw_value) / pop * 100_000
                trend = RawIndicatorResult(
                    trend.raw_value, trend.direction, trend.calculation_window, trend.availability_status,
                    f"{trend.notes}; population {pop:,.0f}; penetration {penetration:.1f} searches per 100k "
                    "inhabitants (share of people wanting it — high = genuine appetite, not just population size)",
                )  # fmt: skip
        # Mesmos seeds/idioma do Demand Volume: os três sinais têm de ler o
        # MESMO conjunto de termos, senão um produto cujos termos curados não
        # têm volume no país perde growth/stability e derruba a análise inteira
        # com o volume de demanda já calculado (ver _demand_rows).
        growth = await pytrends_indicators.compute_growth_score(
            search_terms, location, period_key, seed_terms=seed_terms, language=language, outras_marcas=outras_marcas
        )
        stability = await pytrends_indicators.compute_demand_stability_score(
            search_terms, location, period_key, seed_terms=seed_terms, language=language, outras_marcas=outras_marcas
        )
        intent = await pytrends_indicators.compute_search_intent_score(
            seed_terms,
            location,
            period_key,
            language,
            outras_marcas=outras_marcas,
            relevant_terms=intent_relevantes,
        )

        indicator_raws = [
            (IndicatorCode.TREND_SCORE, trend),
            (IndicatorCode.GROWTH_SCORE, growth),
            (IndicatorCode.DEMAND_STABILITY_SCORE, stability),
            (IndicatorCode.SEARCH_INTENT_SCORE, intent),
        ]

        if category_terms:
            # Sem veto de marcas aqui, ao contrário dos sinais da marca: a
            # categoria inclui as concorrentes por definição. Quem procura
            # "cardhu" está no mercado de whisky, e tirar isso subestimaria a
            # escala contra a qual a marca deve ser lida.
            categoria = await pytrends_indicators.compute_trend_score(
                category_terms, location, period_key, seed_terms=seed_terms, language=language
            )
            categoria = _com_share_da_marca(categoria, brand_raw=trend)
            indicator_raws.append((IndicatorCode.CATEGORY_VOLUME_SCORE, categoria))

        saved: list[IndicatorValue] = []
        for code, raw in indicator_raws:
            # Only Demand Volume (trend) uses the reference-market ceiling; the
            # others normalize against their own fixed bounds (demand_ceiling ignored).
            ceiling = demand_ceiling if code == IndicatorCode.TREND_SCORE else None
            saved.append(
                await self._normalize_and_save(
                    code,
                    DataSource.PYTRENDS,
                    entity.entity_key,
                    country.iso3,
                    period_key,
                    raw,
                    demand_ceiling=ceiling,
                )  # fmt: skip
            )
        return saved

    async def _compute_median_age_fit(
        self, entity: ProductCatalog, country: Country, period_key: str
    ) -> IndicatorValue:
        un_iso2 = country.iso2
        if un_iso2 is None:
            raw = RawIndicatorResult(
                None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.UNAVAILABLE,
                f"country {country.iso3} has no iso2 mapping",
            )  # fmt: skip
        else:
            raw = await undata_indicators.compute_median_age_fit(un_iso2, entity.target_age_min, entity.target_age_max)
        return await self._normalize_and_save(
            IndicatorCode.MEDIAN_AGE_FIT_SCORE, DataSource.UNDATA, entity.entity_key, country.iso3, period_key, raw
        )

    async def _normalize_and_save(
        self,
        indicator_code: IndicatorCode,
        source: DataSource,
        entity_key: uuid.UUID | None,
        country_iso3: str,
        period_key: str,
        raw: RawIndicatorResult,
        demand_ceiling: float | None = None,
    ) -> IndicatorValue:
        normalized_score: float | None = None
        note = raw.notes

        if raw.raw_value is not None and indicator_code == IndicatorCode.TREND_SCORE and demand_ceiling is not None:
            # Demand Volume ruled by the product's reference-market volume
            # (Cap. 4 §4.2.1, 2026-07-22): score = volume / reference x 100.
            # Only the last-resort fixed-bound path (demand_ceiling=None) still
            # uses the old 2M table.
            normalized_score, normalize_note = normalize_demand_volume(raw.raw_value, demand_ceiling)
            if normalize_note is not None:
                note = f"{note}; {normalize_note}" if note else normalize_note
        elif raw.raw_value is not None:
            method = normalization_method(indicator_code)
            reference = None
            if method == NormalizationMethod.MINMAX_FIXED:
                if self_widens(indicator_code):
                    # Self-widening (never narrows) — see
                    # indicator_repository.py::widen_reference_bounds. Never
                    # compares this country against any other country in the
                    # same request, only against its own indicator's fixed,
                    # absolute yardstick.
                    reference = await self._indicator_repo.widen_reference_bounds(
                        indicator_code.value, FIXED_BOUNDS_PERIOD_KEY, float(raw.raw_value)
                    )
                else:
                    # Fixed ceiling (Demand Volume): never widens, values above
                    # saturate at 100 (see normalization._NO_SELF_WIDEN).
                    reference = await self._indicator_repo.get_reference(indicator_code.value, FIXED_BOUNDS_PERIOD_KEY)

            normalized_score, normalize_note = normalize(
                indicator_code=indicator_code, raw_value=raw.raw_value, direction=raw.direction, reference=reference
            )
            if normalize_note is not None:
                note = f"{note}; {normalize_note}" if note else normalize_note

        band = None if normalized_score is None else score_band(normalized_score)
        # Deterministic per-score explanation from the DB-parametrized content
        # (label + band phrase), stored on the row and fed to the final LLM. For
        # Demand Volume, attach the concrete absolute search count (penetration
        # rides along in `notes`); the LLM narrates, never recomputes.
        detail = None
        if indicator_code == IndicatorCode.TREND_SCORE and raw.raw_value is not None:
            detail = f"about {float(raw.raw_value):,.0f} absolute monthly searches"
        catalog = await self._explanation_repo.catalog()
        explanation = catalog.render(code=indicator_code.value, score=normalized_score, band=band, detail=detail)

        return await self._indicator_repo.upsert_value(
            entity_key=entity_key,
            country=country_iso3,
            indicator_code=indicator_code.value,
            period_type=PeriodType.MONTH.value,
            period_key=period_key,
            source=source.value,
            raw_value=raw.raw_value,
            normalized_score=normalized_score,
            direction=raw.direction.value,
            calculation_window=raw.calculation_window,
            availability_status=raw.availability_status.value,
            notes=note,
            business_context=INDICATOR_MEANING.get(indicator_code),
            interpretation_band=band,
            explanation=explanation,
        )
