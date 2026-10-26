"""Reads the DB-parametrized metric explanation content (metric_explanations +
metric_band_phrases) into an ExplanationCatalog. The DB rows are the editable
runtime source (seeded from app/services/mos/explanation_seed.py); loading here
is memoized on the instance, so create one repo per request and edits to the
tables take effect on the next request.

`catalog(language)` serve os textos no idioma da análise — rótulo, significado
e frase por faixa — com o inglês como último recurso de cada um. As frases
gravadas nos scores (`explanation`) continuam em inglês, que é a língua do
cálculo; o pacote de evidências as remonta no idioma de quem lê."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.metric_explanation import MetricBandPhrase, MetricExplanation
from app.services.mos.explanation_builder import ExplanationCatalog, MetricEntry


class MetricExplanationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._catalogs: dict[str, ExplanationCatalog] = {}

    async def catalog(self, language: str = "en") -> ExplanationCatalog:
        if language in self._catalogs:
            return self._catalogs[language]

        phrases_by_code: dict[str, dict[str, str]] = {}
        for phrase in (await self._session.execute(select(MetricBandPhrase))).scalars():
            texto = (phrase.translations or {}).get(language) or phrase.phrase
            phrases_by_code.setdefault(phrase.metric_code, {})[phrase.band] = texto

        metrics: dict[str, MetricEntry] = {}
        for row in (await self._session.execute(select(MetricExplanation))).scalars():
            traduzido = (row.translations or {}).get(language) or {}
            metrics[row.metric_code] = MetricEntry(
                kind=row.metric_kind,
                label=traduzido.get("label") or row.label,
                meaning=traduzido.get("meaning") or row.meaning,
                higher_is_better=row.higher_is_better,
                band_phrases=phrases_by_code.get(row.metric_code, {}),
            )

        self._catalogs[language] = ExplanationCatalog(metrics=metrics)
        return self._catalogs[language]
