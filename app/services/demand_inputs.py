"""O que se lê para medir a demanda de UM país: os termos, os da categoria, as
sementes das palavras relacionadas, os vetos da curadoria, as marcas
concorrentes e o idioma.

Um lugar só, porque eram quatro. A nota do país (indicator_engine), a coleta
dele (analysis_service.ensure_coverage), a régua de demanda
(resolve_demand_ceiling) e a coleta dos candidatos de referência montavam cada
uma o seu conjunto, com regras um pouco diferentes: a régua lia sem os termos
da categoria nas sementes, sem os vetos e sem o veto de outras marcas, e os
candidatos eram pontuados com `excluded_terms=[]`. A régua media uma coisa e a
nota media outra, e um país pedido que entrava na régua tinha dois números
para a mesma demanda — o das sementes sem categoria nem achava as palavras
relacionadas coletadas, que são gravadas sob o rótulo das sementes COM ela.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from app.domain.enums import DataSource
from app.repositories.product_repository import ProductRepository
from app.services.collection_client import resolve_source_country_code
from app.services.indicators.pytrends import build_seed_terms
from app.utils.text import as_term_list


@dataclass(frozen=True)
class DemandInputs:
    country: str
    # O código que o Google Ads espera. None quando o país não tem um, e aí
    # não há demanda para coletar nem para ler.
    location: str | None
    language: str
    search_terms: list[str]
    category_terms: list[str]
    seed_terms: list[str]
    excluded_terms: list[str]
    outras_marcas: list[str]

    @property
    def volume_terms(self) -> list[str]:
        """O que vai na coleta de volume: os termos E os da categoria, na mesma
        request (o `search_volume` aceita 1000 keywords por task)."""
        return [*self.search_terms, *self.category_terms]

    @property
    def intent_relevant_terms(self) -> list[str] | None:
        """O portão de relevância da intenção, ligado SÓ quando as sementes
        atendem dois assuntos. Ver compute_search_intent_score."""
        return self.search_terms if self.category_terms else None


def market_terms(market: dict[str, Any] | None) -> list[str]:
    """Os termos de busca de um mercado, ou o nome dele quando não há termos."""
    market = market or {}
    return as_term_list(market.get("search_terms")) or as_term_list(market.get("primary_name"))


def market_language(market: dict[str, Any] | None, country: Any) -> str:  # noqa: ANN401
    """O idioma em que a busca deste país foi escrita.

    Dito pelo modelo na MESMA chamada que produziu os termos — ele acabou de
    escolher o idioma ao escrevê-los, então perguntar de novo, ou manter uma
    tabela dizendo o mesmo, é trabalho sem informação nova.

    Era uma coluna preenchida à mão em `countries`. Duas coisas estavam erradas
    nisso: acrescentar um mercado exigia alguém cadastrar a linha antes, e o
    valor era UM por país para sempre — Índia e Canadá ficaram "en", o que é
    defensável na média e falso para produtos cujo público busca em hindi ou em
    francês.

    A coluna continua como último recurso para mercados enriquecidos antes
    desta mudança, e só por isso; nada novo precisa ser cadastrado nela.
    """
    do_mercado = (market or {}).get("search_language")
    if do_mercado:
        return str(do_mercado)
    return getattr(country, "search_language", None) or "en"


async def demand_inputs(
    product_repo: ProductRepository,
    entity: Any,  # noqa: ANN401
    country: Any,  # noqa: ANN401
    *,
    brand_key: uuid.UUID | None = None,
    candidate_market: dict[str, Any] | None = None,
) -> DemandInputs:
    """Os insumos de demanda de (produto, marca, país).

    `candidate_market` mede um candidato de referência que ninguém pediu, com
    o vocabulário que o candidato trouxe. Fora isso tudo é igual — categoria,
    sementes, vetos, concorrentes —, e é essa igualdade que deixa a régua
    comparar igual com igual.
    """
    if candidate_market is not None:
        market = candidate_market
    else:
        # O vocabulário DESTA análise: da marca quando há marca, do produto
        # quando não. Medido em 24/09/2026, antes disso: a coleta já usava os
        # termos da marca e a MEDIÇÃO ainda lia os do produto, então "coffee" e
        # "coffee + Juan Valdez" saíam com MOS idêntico.
        market = (await product_repo.markets_for(entity, brand_key)).get(country.iso3) or {}

    # A CATEGORIA, medida ao lado da marca (decisão D4): o MESMO produto sem
    # marca, o outro vocabulário da mesma entidade. Num SUB-PRODUTO é o produto
    # PAI — `instant coffee` se lê contra `coffee` do mesmo jeito que uma marca
    # se lê contra a categoria dela. Numa análise de produto sem pai não há o
    # que acrescentar: os termos já SÃO os da categoria.
    category_terms: list[str] = []
    if brand_key is not None:
        category_terms = market_terms((await product_repo.markets_for(entity, None)).get(country.iso3))
    elif getattr(entity, "generic_entity_key", None) is not None:
        pai = await product_repo.get_by_entity_key(entity.generic_entity_key)
        if pai is not None:
            category_terms = market_terms((pai.localized_markets or {}).get(country.iso3))

    search_terms = market_terms(market)
    # Veto cruzado de marcas: as keywords relacionadas que o Google devolve
    # para os termos desta marca incluem as concorrentes. Vazio para produto
    # sem marca, onde consulta de marca é demanda legítima da categoria. A marca
    # é a da análise; a da entidade só vale para produtos de antes da partição.
    marca = brand_key or getattr(entity, "brand_key", None)
    return DemandInputs(
        country=country.iso3,
        location=resolve_source_country_code(DataSource.PYTRENDS, country),
        language=market_language(market, country),
        search_terms=search_terms,
        category_terms=category_terms,
        seed_terms=build_seed_terms(search_terms, category_terms),
        # Os parecidos que a curadoria descartou para este mercado: termos que
        # aqui querem dizer OUTRA coisa. "picanha suína" (porco) era vetada na
        # porta e entrava pela expansão a 14.800 buscas/mês por conter um termo
        # aprovado.
        excluded_terms=as_term_list(market.get("excluded_terms")),
        outras_marcas=await product_repo.list_other_brand_names(marca),
    )
