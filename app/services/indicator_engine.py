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
from collections.abc import Awaitable, Callable
from typing import Any

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
from app.services.demand_inputs import demand_inputs
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
    # DOIS números da marca aparecem no mesmo pacote e medem coisas
    # diferentes: este, dos termos curados, e o de Demand Volume, que inclui a
    # expansão por palavras relacionadas. Sem dizer isso, a diferença lê-se
    # como contradição — medido em 24/09/2026, o juiz acusou "o texto diz
    # 45.010 e a evidência mostra 109.010" para uma análise da Philips, e os
    # dois números estavam certos.
    comparacao += (
        f"; {volume_marca:,.0f} counts the brand's CURATED terms only, which is why it is lower than the "
        f"Demand Volume figure above — that one also folds in related keywords"
    )
    return RawIndicatorResult(
        categoria.raw_value,
        categoria.direction,
        categoria.calculation_window,
        categoria.availability_status,
        f"{categoria.notes} for the category's GENERIC terms; {comparacao} — both sides count CURATED terms "
        f"only, so they are comparable; a small fraction of a large category means the market exists and "
        f"this brand is not in it, not that demand is absent",
    )


async def _ou_sem_cobertura(
    source: DataSource, code: str | None, compute: Callable[[str], Awaitable[RawIndicatorResult]]
) -> RawIndicatorResult:
    """Lê o indicador pelo código do país na fonte, ou o declara indisponível
    quando a fonte não cobre o país (ver resolve_source_country_code). Perguntar
    com um código inventado — o ISO3 no lugar do `oecd_ref_area` ausente — só
    produzia a mesma ausência por um caminho mais caro."""
    if code is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.UNAVAILABLE,
            f"{source.value} does not cover this country (no country code mapping)",
        )  # fmt: skip
    return await compute(code)


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
        candidate_market: dict[str, Any] | None = None,
        brand_key: uuid.UUID | None = None,
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
                entity,
                country,
                resolved_period_key,
                demand_ceiling=demand_ceiling,
                candidate_market=candidate_market,
                brand_key=brand_key,
            )
            results.append(
                await self._compute_median_age_fit(entity, country, resolved_period_key, brand_key)
            )

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
                IndicatorCode.MOBILE_CONNECTIVITY_SCORE, IndicatorCode.BROADBAND_ADOPTION_SCORE,
                IndicatorCode.GDP_GROWTH_SCORE,
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

        wb_iso2 = resolve_source_country_code(DataSource.WORLDBANK, country, reading=True)
        un_iso2 = resolve_source_country_code(DataSource.UNDATA, country)
        oecd_code = resolve_source_country_code(DataSource.OECD, country)

        gdp_per_capita = await worldbank_indicators.compute_gdp_per_capita(wb_iso2)
        internet = await worldbank_indicators.compute_internet_penetration(wb_iso2)
        mobile = await worldbank_indicators.compute_mobile_connectivity(wb_iso2)
        broadband = await worldbank_indicators.compute_broadband_adoption(wb_iso2)
        gdp_growth = await worldbank_indicators.compute_gdp_growth(wb_iso2)
        population = await worldbank_indicators.compute_population_scale(wb_iso2)
        urbanization = await worldbank_indicators.compute_urbanization(wb_iso2)
        population_growth = await _ou_sem_cobertura(
            DataSource.UNDATA, un_iso2, undata_indicators.compute_population_growth
        )
        net_migration = await _ou_sem_cobertura(
            DataSource.UNDATA, un_iso2, undata_indicators.compute_net_migration_rate
        )

        # Macro Demand Structure (2026-07-08, GDP = C+I+G+NX identity) +
        # Real Estate Growth (Economic Readiness, conditional/OECD-only).
        consumer_spending = await worldbank_indicators.compute_consumer_spending(wb_iso2)
        government_expenditure = await worldbank_indicators.compute_government_expenditure(wb_iso2)
        investment = await worldbank_indicators.compute_investment(wb_iso2)
        net_exports = await worldbank_indicators.compute_net_exports(wb_iso2)
        trade_openness = await worldbank_indicators.compute_trade_openness(wb_iso2)
        real_estate_growth = await _ou_sem_cobertura(
            DataSource.OECD, oecd_code, oecd_indicators.compute_real_estate_growth
        )

        raws: list[tuple[IndicatorCode, DataSource, RawIndicatorResult]] = [
            (IndicatorCode.GDP_PER_CAPITA_SCORE, DataSource.WORLDBANK, gdp_per_capita),
            (IndicatorCode.INTERNET_PENETRATION_SCORE, DataSource.WORLDBANK, internet),
            (IndicatorCode.MOBILE_CONNECTIVITY_SCORE, DataSource.WORLDBANK, mobile),
            (IndicatorCode.BROADBAND_ADOPTION_SCORE, DataSource.WORLDBANK, broadband),
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
        candidate_market: dict[str, Any] | None = None,
        brand_key: uuid.UUID | None = None,
    ) -> list[IndicatorValue]:
        # Demand from Google Ads ABSOLUTE search volume (not Trends), 2026-07-15:
        # each country is measured with its OWN user-approved search terms.
        # LEVEL = SUM of those terms' monthly search volume (real total online
        # search demand); growth/stability from the summed monthly series;
        # intent from the related keywords. `candidate_market` scores a
        # reference-candidate country (not in the vocabulary) with the terms its
        # demand was collected under — used for the off-request "champion".
        #
        # Os insumos vêm de demand_inputs, o mesmo lugar de onde a coleta e a
        # régua de demanda tiram os seus: a régua só compara igual com igual se
        # mede exatamente o que a nota mede.
        insumos = await demand_inputs(
            self._product_repo, entity, country, brand_key=brand_key, candidate_market=candidate_market
        )
        location = insumos.location
        search_terms = insumos.search_terms
        category_terms = insumos.category_terms
        seed_terms = insumos.seed_terms
        language = insumos.language
        excluded_terms = insumos.excluded_terms
        outras_marcas = insumos.outras_marcas
        intent_relevantes = insumos.intent_relevant_terms

        trend = await pytrends_indicators.compute_trend_score(
            search_terms,
            location,
            period_key,
            seed_terms=seed_terms,
            language=language,
            outras_marcas=outras_marcas,
            excluded_terms=excluded_terms,
        )
        # Decision context for the LLM narrative (not scored): pair the absolute
        # demand VOLUME with population so it can say WHETHER the volume comes
        # from sheer population or from genuine appetite. Penetration = searches
        # per 100k inhabitants — high volume + high penetration = big AND
        # enthusiastic; high volume + low penetration = big only because
        # populous; low volume + high penetration = small but strong appetite.
        if trend.availability_status == AvailabilityStatus.AVAILABLE and trend.raw_value is not None:
            wb_code = resolve_source_country_code(DataSource.WORLDBANK, country, reading=True)
            pop = await worldbank_indicators.population(wb_code) if wb_code else None
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
            search_terms,
            location,
            period_key,
            seed_terms=seed_terms,
            language=language,
            outras_marcas=outras_marcas,
            excluded_terms=excluded_terms,
        )
        stability = await pytrends_indicators.compute_demand_stability_score(
            search_terms,
            location,
            period_key,
            seed_terms=seed_terms,
            language=language,
            outras_marcas=outras_marcas,
            excluded_terms=excluded_terms,
        )
        intent = await pytrends_indicators.compute_search_intent_score(
            seed_terms,
            location,
            period_key,
            outras_marcas=outras_marcas,
            relevant_terms=intent_relevantes,
            excluded_terms=excluded_terms,
        )

        indicator_raws = [
            (IndicatorCode.TREND_SCORE, trend),
            (IndicatorCode.GROWTH_SCORE, growth),
            (IndicatorCode.DEMAND_STABILITY_SCORE, stability),
            (IndicatorCode.SEARCH_INTENT_SCORE, intent),
        ]

        if category_terms:
            # A categoria é medida SEM expansão: só os termos genéricos curados.
            #
            # A expansão dobra keywords relacionadas se elas contiverem um termo
            # aprovado, e as concorrentes passam por aí facilmente — medido ao
            # vivo em 27/08, a categoria da Espanha somou `grant's scotch whisky`
            # (720) e `chivas regal 12 premium scotch whisky precio` (40), porque
            # as duas contêm "scotch whisky". O veto por cadastro não resolve:
            # ele só conhece marcas que a plataforma já viu, e Grant's e Chivas
            # nunca foram analisadas.
            #
            # Este denominador existe para responder "existe mercado para este
            # tipo de produto aqui, independentemente desta marca?", e a leitura
            # que a narrativa faz dele — "a categoria é muito buscada e a marca
            # não está nela" — não sobrevive a um denominador feito de outras
            # marcas. Sem expansão ele é exatamente o que o rótulo promete, e não
            # depende de a plataforma conhecer as concorrentes.
            #
            # O veto de marcas fica como guarda dos termos CURADOS: eles são
            # genéricos por construção (o enriquecimento roda com `brand=None`),
            # e o veto é o que percebe se algum dia deixarem de ser.
            todas_as_marcas = await self._product_repo.list_all_brand_names()
            categoria = await pytrends_indicators.compute_trend_score(
                category_terms,
                location,
                period_key,
                seed_terms=seed_terms,
                language=language,
                outras_marcas=todas_as_marcas,
                expandir=False,
            )
            # A razão compara IGUAL com IGUAL: os termos curados dos dois lados.
            # O Demand Volume pontuado da marca soma a expansão (é a régua dela),
            # e a categoria não soma — então usar aquele número aqui compararia
            # "curados + 15 relacionadas" contra "curados". Medido ao vivo em
            # 27/08 no Reino Unido, a diferença troca a conclusão: 20.980 x
            # 18.460 diz "a marca é maior que a categoria", enquanto os curados
            # dos dois lados dizem outra coisa. É leitura de linhas já em cache,
            # sem coleta nova.
            marca_curada = await pytrends_indicators.compute_trend_score(
                search_terms,
                location,
                period_key,
                seed_terms=seed_terms,
                language=language,
                outras_marcas=outras_marcas,
                expandir=False,
            )
            categoria = _com_share_da_marca(categoria, brand_raw=marca_curada)
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
                    brand_key=brand_key,
                )  # fmt: skip
            )
        return saved

    async def _compute_median_age_fit(
        self, entity: ProductCatalog, country: Country, period_key: str, brand_key: uuid.UUID | None = None
    ) -> IndicatorValue:
        raw = await _ou_sem_cobertura(
            DataSource.UNDATA,
            resolve_source_country_code(DataSource.UNDATA, country),
            lambda code: undata_indicators.compute_median_age_fit(code, entity.target_age_min, entity.target_age_max),
        )
        return await self._normalize_and_save(
            IndicatorCode.MEDIAN_AGE_FIT_SCORE,
            DataSource.UNDATA,
            entity.entity_key,
            country.iso3,
            period_key,
            raw,
            brand_key=brand_key,
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
        brand_key: uuid.UUID | None = None,
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
            brand_key=brand_key,
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
