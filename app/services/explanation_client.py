"""Fase 5 — final LLM inference. Assembles a deterministic "evidence packet"
from the computed MOS + indicator values and asks the resolved `explanation`
LLM (provider/model from .env — default ollama / gemma4:12b, see
app/services/llm/resolver.py) to turn it into a business-facing narrative of
the market-opportunity ranking ("where to sell"). The prompt lives HERE (in the
core) so it is provider-agnostic — switching to claude/gpt asks the exact same
thing.

Best-effort: any failure (provider down, timeout, LLM error) returns None and is
logged — the analysis still completes with the numbers, just without prose. The
LLM only EXPLAINS these deterministic values; it never computes or alters a
score (Cap. 4 §4.0, memory marketmind_mos_engine).
"""

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.models.mos_result import MosResult
from app.domain.enums import IndicatorCode
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.metric_explanation_repository import MetricExplanationRepository
from app.services.llm import resolve_explanation_llm
from app.services.mos.dimension_mapping import DIMENSION_BY_INDICATOR, compute_dimension_subscores
from app.services.mos.explanation_builder import ExplanationCatalog
from app.services.mos.interpretation import score_band

# Every indicator the packet may carry, in a stable narrative order: the four
# scored demand signals and the macro/readiness block (DIMENSION_BY_INDICATOR),
# then the two context-only ones (self-scaled / direction-neutral, never scored
# into the MOS but useful colour for the narrative).
_PACKET_INDICATORS: list[IndicatorCode] = [
    *DIMENSION_BY_INDICATOR,
    IndicatorCode.REGIONAL_DEMAND_SCORE,
    IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
]

_SYSTEM_PROMPT_PT = """Você é um analista de entrada em mercados (market-entry) que ajuda um negócio \
a decidir EM QUAIS PAÍSES vender um produto, a partir de um "pacote de evidências" já calculado \
pelo motor determinístico de MOS (Market Opportunity Score) da MarketMind.

REGRAS INVIOLÁVEIS:
- Use SOMENTE os números fornecidos no pacote. NUNCA invente, estime, arredonde de forma nova \
ou recalcule qualquer valor. Não escreva nenhum número que não esteja no input.
- Você EXPLICA os resultados determinísticos; você não os recalcula nem os contesta.
- Inclua TODOS os países do pacote (não omita nenhum), na ordem do MOS (maior primeiro). \
Cite o MOS de cada país EXATAMENTE como está no input — o ranking já é final, não reordene \
nem recalcule por conta própria.

O PACOTE traz, para CADA país: o MOS (com banda e um campo `explanation` em texto), a lista de \
`dimensions` e a lista de `signals` (indicadores). CADA item — MOS, dimensão e sinal — tem um campo \
`explanation`: uma leitura em linguagem natural do NÚMERO daquele score (o que aquele nível significa). \
Há ainda um `glossary` no topo definindo o que cada métrica mede. USE o `explanation` de cada score e o \
`glossary` para construir uma explicação RICA, aproveitando TODOS os dados que entram no MOS \
(todas as dimensões e sinais), não só o volume.

PRODUZA, nesta ordem:
1) Uma recomendação curta de "onde vender", com os países ranqueados do melhor para o pior, \
citando o MOS de cada um.
2) Um parágrafo por país explicando o seu MOS a partir dos seus sinais (signals) e dimensões \
(dimensions) — cite os scores e as leituras (`explanation`) dos fatores que mais pesam, tanto os \
favoráveis quanto os desfavoráveis.

DISTINÇÃO CRÍTICA (torne-a explícita usando o sinal de volume de demanda, cujo `context` traz buscas \
absolutas + população + penetração por 100 mil habitantes):
- Separe "vende muito principalmente porque o país é populoso" (volume alto, mas penetração BAIXA) \
de "vende muito porque uma parcela alta das pessoas realmente quer o produto" (penetração ALTA = \
apetite real).
- Deixe claro que um mercado menor, mas com penetração/qualidade alta, ainda pode ser uma forte \
oportunidade.
- Cite concretamente os números reais (buscas absolutas, população, penetração por 100 mil).

Seja conciso e com tom de negócio."""

_SYSTEM_PROMPT_EN = """You are a market-entry analyst helping a business decide IN WHICH COUNTRIES \
to sell a product, based on an "evidence packet" already computed by MarketMind's deterministic \
MOS (Market Opportunity Score) engine.

INVIOLABLE RULES:
- Use ONLY the numbers provided in the packet. NEVER invent, estimate, re-round or recompute any \
value. Do not write any number that is not in the input.
- You EXPLAIN the deterministic results; you do not recompute or dispute them.
- Include EVERY country in the packet (omit none), ordered by MOS (highest first). Quote each \
country's MOS EXACTLY as given in the input — the ranking is already final, do not reorder or \
recompute it yourself.

The PACKET carries, for EACH country: the MOS (with band and an `explanation` string), the list of \
`dimensions` and the list of `signals` (indicators). EVERY item — MOS, dimension and signal — has an \
`explanation` field: a plain-language reading of that score's NUMBER (what that level means). A \
top-level `glossary` defines what each metric measures. USE each score's `explanation` and the \
`glossary` to build a RICH explanation that draws on ALL the data feeding the MOS (every dimension and \
signal), not just volume.

PRODUCE, in this order:
1) A short "where to sell" recommendation, with the countries ranked best to worst, quoting each \
country's MOS.
2) One paragraph per country explaining its MOS from its signals and dimensions — cite the scores and \
readings (`explanation`) of the factors that weigh most, both favourable and unfavourable.

CRITICAL DISTINCTION (make it explicit using the demand-volume signal, whose `context` carries absolute \
searches + population + penetration per 100k inhabitants):
- Separate "sells a lot mainly because the country is populous" (high volume but LOW penetration) \
from "sells a lot because a high share of people genuinely want it" (high penetration = real \
appetite).
- Make clear that a smaller market with high penetration/quality can still be a strong opportunity.
- Concretely reference the real figures (absolute searches, population, penetration per 100k).

Be concise and business-toned."""


def _system_prompt(language: str) -> str:
    lang = (language or "").strip().lower()
    if lang in ("pt", "pt-br", "pt_br", "portuguese", "portugues", "português"):
        return _SYSTEM_PROMPT_PT
    if lang in ("en", "en-us", "en_us", "english"):
        return _SYSTEM_PROMPT_EN
    return f'{_SYSTEM_PROMPT_EN}\n\nWrite the entire narrative in this language: "{language}".'


class ExplanationClient:
    """Builds the evidence packet and calls the final-inference LLM service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)
        self._explanation_repo = MetricExplanationRepository(session)

    async def _country_evidence(
        self, entity_key: uuid.UUID, mos_result: MosResult, period_key: str, catalog: ExplanationCatalog
    ) -> dict[str, Any]:
        country = mos_result.country
        country_values = await self._indicator_repo.get_values_for(
            entity_key=None, country=country, period_type="month", period_key=period_key
        )
        product_values = await self._indicator_repo.get_values_for(
            entity_key=entity_key, country=country, period_type="month", period_key=period_key
        )
        merged = {**country_values, **product_values}

        signals: list[dict[str, Any]] = []
        for code in _PACKET_INDICATORS:
            iv = merged.get(code.value)
            if iv is None or iv.normalized_score is None:
                continue
            # Each signal carries its stored per-score explanation (built at
            # compute time from the DB-parametrized band phrases). Demand Volume
            # also carries the concrete numbers (absolute searches / population /
            # penetration) as `context` for the population-vs-appetite call.
            signal: dict[str, Any] = {
                "code": code.value,
                "label": catalog.label(code.value),
                "score": round(float(iv.normalized_score), 1),
                "band": iv.interpretation_band,
                "explanation": iv.explanation,
            }
            if code == IndicatorCode.TREND_SCORE:
                signal["context"] = iv.notes
            signals.append(signal)

        dimensions: list[dict[str, Any]] = []
        for dim, score in compute_dimension_subscores(merged).items():
            band = score_band(score)
            dimensions.append(
                {
                    "dimension": dim.value,
                    "label": catalog.label(dim.value),
                    "score": round(score, 1),
                    "band": band,
                    "explanation": catalog.render(code=dim.value, score=score, band=band),
                }
            )
        return {
            "country": country,
            "mos": round(float(mos_result.mos), 1) if mos_result.mos is not None else None,
            "mos_band": mos_result.mos_interpretation,
            "mos_explanation": mos_result.explanation,
            "dimensions": dimensions,
            "signals": signals,
        }

    async def generate(
        self, *, entity_key: uuid.UUID, product_name: str, mos_results: list[MosResult], period_key: str
    ) -> str | None:
        scored = [r for r in mos_results if r.mos is not None]
        if not scored:
            return None
        try:
            catalog = await self._explanation_repo.catalog()
            # Metric definitions once at the top (not repeated per country) so the
            # LLM has the full "what each score means" context without bloating
            # the packet.
            glossary = {
                code: {"label": entry.label, "meaning": entry.meaning} for code, entry in catalog.metrics.items()
            }
            countries = [await self._country_evidence(entity_key, r, period_key, catalog) for r in scored]
            system = _system_prompt(settings.ollama_llm_narrative_language)
            user = json.dumps(
                {"product_name": product_name, "glossary": glossary, "countries": countries},
                ensure_ascii=False,
                indent=2,
            )
            narrative = await resolve_explanation_llm().complete(system=system, user=user)
            if not narrative or not str(narrative).strip():
                logger.warning("Explanation LLM returned an empty narrative")
                return None
            return str(narrative)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never fails the analysis
            logger.warning(f"Final LLM explanation skipped (provider unavailable or errored): {exc}")
            return None
