"""Request and response shapes for the /internal routes — the tools the
orchestrator drives an analysis with.

Every one of them takes `period_key` explicitly rather than reading a clock.
Two calls that belong to the same analysis must agree on the period even when
the analysis runs across midnight or is resumed a day later, and a tool that
decides its own period cannot be replayed to the same answer.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import NarrativeLanguage
from app.schemas.coverage import JobTaskResult


class RegisterAnalysisRequest(BaseModel):
    entity_key: uuid.UUID | None = None
    canonical_title: str | None = None
    countries: list[str] = Field(..., min_length=1)
    estimated_price: Decimal | None = None
    price_currency: str | None = None
    user_prompt: str | None = None
    # Como o catálogo chegou nesta entidade: `alias`, `trigram`, `llm` ou
    # `created`. Quem resolve é quem sabe, e é ele que conta — aqui não se
    # tenta deduzir. Ver a migration 039.
    catalog_method: str | None = None
    catalog_match_score: Decimal | None = None
    catalog_match_reasoning: str | None = None
    catalog_query_text: str | None = None
    # A marca desta análise. Nulo é uma resposta — "foi do produto" —, e é o
    # par (produto, marca) que dá a rastreabilidade de quem foi analisado com
    # marca e quem não.
    brand_key: uuid.UUID | None = None
    brand_name: str | None = None
    # O idioma da interface de quem pediu, em que a narrativa deve sair.
    # Nulo é o configurado no serviço.
    language: NarrativeLanguage | None = None


class RegisteredAnalysisResponse(BaseModel):
    request_id: uuid.UUID
    status: str
    # O que a resolução decidiu, para quem registrou não ter de perguntar de
    # novo. Preenchidos por POST /internal/analyses/register.
    entity_key: uuid.UUID | None = None
    canonical_title: str | None = None
    brand_key: uuid.UUID | None = None
    brand_name: str | None = None


class MarkRunningRequest(BaseModel):
    # Até quando insistir numa fonte fora do ar, decidido por quem executa (o
    # orquestrador dá 24h). Gravado na primeira vez e nunca adiado: uma
    # retomada manda o prazo de novo, e cada uma empurraria o fim para frente.
    # É o que a varredura de retomada e o sentinela leem para encerrar uma
    # análise parada além do prazo — e nunca era escrito.
    deadline_at: datetime | None = None


class ProgressRequest(BaseModel):
    # None clears the note. The heartbeat is stamped either way: reporting that
    # nothing is happening is still a sign of life, and the sweep that resumes
    # dead analyses reads the heartbeat, not the note.
    message: str | None = None


class ProgressResponse(BaseModel):
    request_id: uuid.UUID
    progress: str | None = None


class CompleteAnalysisRequest(BaseModel):
    result: list[dict[str, Any]] = Field(..., min_length=1)
    breakdown: dict[str, Any] | None = None
    explanation: str | None = None


class FailAnalysisRequest(BaseModel):
    error_message: str = Field(..., min_length=1)


class AnalysisStateResponse(BaseModel):
    request_id: uuid.UUID
    status: str


class AnalysisDetailResponse(BaseModel):
    """Everything a resumed run needs to pick the analysis back up.

    A resumed run must not re-decide what was already decided: which product,
    which countries, what the buyer asked. Reading them back is what makes
    resuming cheap, since every step is idempotent and re-running is the whole
    recovery strategy.
    """

    request_id: uuid.UUID
    status: str
    entity_key: uuid.UUID | None = None
    canonical_title: str | None = None
    # Lido de volta, não redecidido: uma retomada não resolve o produto outra
    # vez, então sem isto ela relataria "não sei" para algo que já se soube.
    catalog_method: str | None = None
    catalog_match_score: Decimal | None = None
    catalog_match_reasoning: str | None = None
    catalog_query_text: str | None = None
    # Derivado na leitura, não guardado: o limiar mora aqui e pode mudar, e o
    # rótulo honesto é "pelo corte de hoje, isto passou raspando".
    catalog_match_borderline: bool = False
    brand_key: uuid.UUID | None = None
    brand_name: str | None = None
    countries: list[str] = []
    user_prompt: str | None = None
    language: NarrativeLanguage | None = None
    progress: str | None = None
    explanation: str | None = None
    error_message: str | None = None
    result: list[dict[str, Any]] | None = None


class CalibrateWeightsRequest(BaseModel):
    period_key: str = Field(..., min_length=1)


class CalibrateWeightsResponse(BaseModel):
    period_key: str
    weights: dict[str, float]


class EvidenceRequest(BaseModel):
    entity_key: uuid.UUID
    countries: list[str] = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    demand_ruler: dict[str, Any] | None = None
    # A marca desta análise: o pacote explica os números que produziram
    # AQUELE MOS, não os da categoria.
    brand_key: uuid.UUID | None = None
    # O idioma da análise: rótulos, faixas, glossário e frases saem nele. Nulo
    # é o padrão do serviço (app/utils/language.py).
    language: NarrativeLanguage | None = None


class EvidenceResponse(BaseModel):
    # None when no country in the request has a stored MOS for that period.
    # Absent evidence is reported as absent, never as an empty packet that
    # would read downstream as "nothing to say about this market".
    packet: dict[str, Any] | None = None


class NarrateRequest(BaseModel):
    packet: dict[str, Any]
    product_name: str
    # A marca, quando a análise tem uma. O texto final fala de PRODUTO + MARCA:
    # os números vieram do vocabulário da marca, e chamá-los de "café" seria
    # atribuir à categoria o que se mediu da Juan Valdez.
    brand: str | None = None
    user_prompt: str | None = None
    # O idioma que a análise pediu (lido do registro dela). Nulo é o
    # configurado no serviço — o que toda análise recebia antes.
    language: NarrativeLanguage | None = None


class NarrateResponse(BaseModel):
    # None when the model failed. Best-effort by design: the numbers are the
    # answer, the prose is a reading of them, and a missing reading never
    # invalidates the answer.
    narrative: str | None = None


class EnrichTermsRequest(BaseModel):
    entity_key: uuid.UUID
    countries: list[str] = Field(..., min_length=1)
    # The generic family behind a branded product is enriched WITHOUT reference
    # candidates: the demand ruler belongs to the brand, and the category is
    # context that is never scored.
    with_candidates: bool = True
    # A marca desta análise, quando há. É ela que decide QUAL vocabulário
    # medir — sem ela uma análise de marca mediria a categoria.
    brand_key: uuid.UUID | None = None


class EnrichTermsResponse(BaseModel):
    entity_key: uuid.UUID
    # Which countries now carry curated search terms. Enrichment only fills what
    # is missing, so an entity that already had everything reports the same
    # countries without any model having been called.
    countries_with_terms: list[str]
    reference_candidates: int


class EnsureCoverageRequest(BaseModel):
    country_iso3: str = Field(..., min_length=3, max_length=3)
    entity_key: uuid.UUID | None = None
    # O período da análise, que quem a começou fixou. As tarefas de coleta são
    # carimbadas com ele; lido de um relógio aqui, uma análise que atravessa a
    # meia-noite do último dia do mês carimbaria dois meses.
    period_key: str = Field(..., min_length=1)
    # A marca desta análise, quando há. É ela que decide QUAL vocabulário
    # medir — sem ela uma análise de marca mediria a categoria.
    brand_key: uuid.UUID | None = None


class EnsureCoverageResponse(BaseModel):
    country: str
    # The vocabulary this country was searched for. The one part of the
    # pipeline a model wrote, and nothing downstream reveals it: a wrong native
    # term produces a perfectly formed analysis of the wrong words, with no
    # symptom anywhere else.
    location: str | None = None
    language: str | None = None
    search_terms: list[str] = []
    category_terms: list[str] = []
    # The dispatched collection tasks, empty when nothing needed collecting.
    #
    # This list is how the two reasons a country can still lack an indicator are
    # told apart, and the distinction decides whether to insist or to declare.
    # A FAILED task is a source that is down, and it is worth retrying. A task
    # that finished without failing and still left the indicator missing is a
    # source that does not have that datum, and no amount of retrying will
    # change it.
    tasks: list[JobTaskResult]
    # O outro lado da conta: quantos indicadores de cada fonte já estavam no
    # banco, dentro do prazo, e por isso não viraram tarefa nenhuma. Sem isto
    # uma fonte inteiramente aproveitada não aparece em lugar algum do relato,
    # e "não apareceu" é indistinguível de "não foi consultada".
    reused: dict[str, int] = {}
    # As fontes que não cobrem ESTE país — resposta, não omissão. Lida do
    # mapeamento em `countries`: sem o código do país naquela fonte, ela não o
    # conhece. A OCDE não publica preço imobiliário para Argentina e Peru.
    not_covering: list[str] = []
    # As palavras relacionadas (que alimentam o intent score) já estavam
    # guardadas e dentro do prazo. Contada à parte porque é UM conjunto de
    # sementes, não N termos — somá-la aos termos de volume misturaria
    # unidades diferentes numa contagem só.
    related_fresh: bool = False


class CandidatesCoverageRequest(BaseModel):
    entity_key: uuid.UUID
    requested_countries: list[str] = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    # A marca desta análise, quando há. É ela que decide QUAL vocabulário
    # medir — sem ela uma análise de marca mediria a categoria.
    brand_key: uuid.UUID | None = None


class CandidatesCoverageResponse(BaseModel):
    entity_key: uuid.UUID
    # O que esta coleta fez, que antes não voltava de jeito nenhum. É o passo
    # mais caro do sistema — três análises seguidas de um produto levaram 594s,
    # 203s e 396s, e quase todo o custo repetido era trabalho de candidato
    # refeito — e era o único sem nenhum relato.
    tasks: list[JobTaskResult] = []
    reused: dict[str, int] = {}
    related_fresh: int = 0
    # The champion this product already has for the period, when it has one.
    # The caller needs the figure, not just the fact: a stored champion that a
    # requested market now beats must be replaced, or it is reloaded and
    # crowned again on every later analysis.
    champion_country: str | None = None
    champion_mos: float | None = None
    # Whether a champion computed for this period already exists. When it does,
    # candidate collection is the slowest work in the system and it produces the
    # same answer, so the caller should skip it: three consecutive analyses of
    # one product took 594s, 203s and 396s, and nearly all of the repeat cost
    # was candidate work redone.
    champion_cached: bool


class DemandCeilingRequest(BaseModel):
    entity_key: uuid.UUID
    countries: list[str] = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    # A marca desta análise, quando há. É ela que decide QUAL vocabulário
    # medir — sem ela uma análise de marca mediria a categoria.
    brand_key: uuid.UUID | None = None


class DemandCeilingResponse(BaseModel):
    # The largest real search volume among the product's reference markets, or
    # null when none could be read. It only READS collected demand and never
    # collects, so it is stable across repeats rather than drifting.
    demand_ceiling: float | None = None
    # Which ruler was used, carried into the narrative so the text can say so
    # rather than presenting a normalised figure as if it were absolute.
    demand_ruler: dict[str, Any] | None = None


class ScoreCandidatesRequest(BaseModel):
    entity_key: uuid.UUID
    requested_countries: list[str] = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    demand_ceiling: float | None = None
    # A marca desta análise: os candidatos dela são os DELA. Onde o café é mais
    # procurado não é onde a Juan Valdez é mais procurada.
    brand_key: uuid.UUID | None = None


class ScoredCandidate(BaseModel):
    country: str
    mos: float | None = None
    availability_status: str
    excluded_reason: str | None = None


class ScoreCandidatesResponse(BaseModel):
    # The product's reference markets, scored. The MOS uses fixed anchors, so
    # scoring these never changes what a requested country scores -- which is
    # what makes it safe to surface the best one as a benchmark.
    candidates: list[ScoredCandidate]


class SetChampionRequest(BaseModel):
    entity_key: uuid.UUID
    period_key: str = Field(..., min_length=1)
    # Null records "nothing scorable this period", which is a real outcome and
    # has to be stored: without it the same fruitless candidate work is
    # attempted again on every later analysis.
    country: str | None = None
    mos: float | None = None
    # A marca desta análise, quando há. É ela que decide QUAL vocabulário
    # medir — sem ela uma análise de marca mediria a categoria.
    brand_key: uuid.UUID | None = None


class CrownChampionRequest(BaseModel):
    entity_key: uuid.UUID
    period_key: str = Field(..., min_length=1)
    requested_countries: list[str] = Field(..., min_length=1)
    # As linhas de MOS dos países pedidos, como POST /mos/compute as devolveu.
    # Só `country` e `mos` são lidos.
    requested_results: list[dict[str, Any]] = []
    brand_key: uuid.UUID | None = None


class CrownChampionResponse(BaseModel):
    # Nulo quando nada foi pontuável — nem pedido, nem candidato.
    champion_country: str | None = None
    champion_mos: float | None = None
    # O campeão é um dos países pedidos.
    requested: bool = False
    # A linha de MOS do campeão quando ele NÃO foi pedido, no formato de POST
    # /mos/compute, para entrar no resultado ao lado das pedidas.
    champion_row: dict[str, Any] | None = None
    # O campeão guardado para este período e esta régua foi reaproveitado, e os
    # candidatos não foram pontuados de novo.
    from_cache: bool = False
    # A régua contra a qual o campeão foi medido (nula = régua fixa).
    demand_ceiling: float | None = None
