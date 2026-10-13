from typing import Literal
from urllib.parse import quote

from pydantic_settings import BaseSettings

from app.db.ssl_config import sslmode_query_suffix


class Settings(BaseSettings):
    app_name: str = "marketmind-scoring"
    app_env: str = "local"
    app_port: int = 8000

    # CORS — the browser frontend (marketmind-ai, Astro dev server) calls this
    # API directly, so its origin must be allowed. Comma-separated list of
    # origins; override via the CORS_ALLOW_ORIGINS env var. Use "*" to allow any
    # origin (dev only). Default covers the Astro dev/preview ports on localhost.
    cors_allow_origins: str = "http://localhost:4321,http://127.0.0.1:4321,http://localhost:4322,http://127.0.0.1:4322"

    # Core's own database (product catalog, coverage, indicators, MOS, jobs)
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "marketmind_core"
    database_user: str = "marketmind"
    database_password: str = "marketmind_password"

    # TLS for every Postgres connection this service opens — its own database
    # and the 4 read-only ones below, which on Azure all live on the same
    # Flexible Server. "disable" keeps local docker-compose working unchanged;
    # Azure needs at least "require" because the server refuses plaintext
    # (`require_secure_transport = ON`). See app/db/ssl_config.py.
    database_ssl_mode: str = "disable"

    # HTTP APIs of the 4 collection services. Everything goes through these
    # now: dispatching a collection, polling an ingestion run, AND reading the
    # data they hold. Reading their tables directly used to be the shortcut, and
    # it coupled this service to four schemas it does not own.
    # Ports match each service's own .env (made distinct there so all four can
    # run at once): pytrends=8000, oecd=8001, undata=8002, worldbank=8003.
    pytrends_api_base_url: str = "http://host.docker.internal:8000"
    worldbank_api_base_url: str = "http://host.docker.internal:8003"
    oecd_api_base_url: str = "http://host.docker.internal:8001"
    undata_api_base_url: str = "http://host.docker.internal:8002"
    # LLM PROVIDER RESOLVER (2026-07-22). Every LLM call site picks its provider
    # and model INDEPENDENTLY from the env — swap models/providers by editing
    # .env only, no code change. Provider code is one of: "claude" (Anthropic
    # SDK), "gpt" (OpenAI SDK), "ollama" (routed through the marketmind-ollma-llm
    # gateway). See app/services/llm/resolver.py.
    #   - vision: the identify-image call (needs a VISION model).
    #   - enrichment: fills the JSON fields the vision model left out — above all
    #     localized_markets, which must never be empty (a TEXT model, not vision:
    #     the local vision model is unreliable at producing localized terms).
    #   - reference_market: chooses the ranked reference-market CANDIDATES (which
    #     countries are the product's biggest markets). Real market knowledge, so
    #     it defaults to a REAL (cloud) LLM — the local model doesn't know actual
    #     market sizes (e.g. it omitted Brazil for air fryer). Real search volume
    #     still elects the winner among the candidates; the LLM only proposes.
    #   - explanation: the final MOS-narrative call (text).
    # Default to claude-opus-5 on every site (2026-08-13, user decision): this
    # project ships published with no OpenAI dependency, so "gpt" stays a
    # supported provider code in the resolver but is no longer a default
    # anywhere. Vision included — Anthropic's vision endpoint is first-party.
    # A single model across all four sites also keeps the term-quality bar
    # (Japan's ノンフライヤー, the reason gpt-5 was picked in 2026-07-24) met by
    # the same model that identifies the product. Still swappable per-site
    # via .env.
    vision_llm_provider: str = "claude"
    vision_llm_model: str = "claude-opus-5"
    enrichment_llm_provider: str = "claude"
    enrichment_llm_model: str = "claude-opus-5"
    reference_market_llm_provider: str = "claude"
    reference_market_llm_model: str = "claude-opus-5"
    explanation_llm_provider: str = "claude"
    explanation_llm_model: str = "claude-opus-5"
    #   - catalog_match: decide se o texto consultado é o mesmo produto de
    #     alguma entidade do catálogo, quando alias exato e trigram falham.
    #     Substituiu o passo de embedding (2026-08-14). Medido contra os 89
    #     termos reais do catálogo: com os search_terms indexados como alias,
    #     o trigram resolve 71 deles no limiar 0.4 com precisão de 100%; os 18
    #     que sobram não têm parentesco de escrita com nada indexado
    #     (`odżywka bez spłukiwania`, `lockencreme`, `krulcrème`) ou são
    #     sinônimo de domínio (`quadcopter` para drone). Nenhum vetor decide
    #     esses casos: `quadcopter`~`drone` é conhecimento, não similaridade
    #     de string. Roda só nessa minoria.
    catalog_match_llm_provider: str = "claude"
    catalog_match_llm_model: str = "claude-opus-5"
    # Teto de entidades enviadas ao LLM. Com o catálogo pequeno cabem todas;
    # o limite existe para o prompt não crescer sem controle conforme ele
    # cresce. A entidade da mesma `category` tem prioridade na seleção.
    catalog_match_candidate_limit: int = 25

    # marketmind-ollma-llm gateway (port 8005) — the transport used when a site's
    # provider is "ollama"; routes to a LOCAL Ollama model. Best-effort for the
    # explanation (analysis still completes without a narrative if it's down).
    ollama_llm_api_base_url: str = "http://host.docker.internal:8005"
    ollama_llm_timeout_seconds: float = 300.0
    ollama_llm_narrative_language: str = "pt"

    # Shared secret the orchestrator sends on /internal/* calls. Those routes
    # WRITE -- they register analyses, collect, and complete requests -- so they
    # are not for the browser.
    #
    # Empty means every /internal/* call is refused, on purpose. An unset secret
    # that waved everything through would be indistinguishable, in a running
    # deployment, from one that was configured correctly.
    internal_api_token: str = ""

    # marketmind-orchestrator: where analyses are run. Not a choice — the
    # in-process loop that used to be the alternative is gone.
    orchestrator_api_base_url: str = "http://host.docker.internal:8081"

    # How long the Core waits for an ingestion run to finish before giving up
    # on a single collection call (the run itself keeps going server-side;
    # this only bounds how long the Core polls synchronously).
    collection_poll_interval_seconds: float = 2.0
    collection_poll_timeout_seconds: float = 120.0

    # How long the Core waits for the initial dispatch POST itself (before a
    # run even exists to poll). Found too tight at the old hardcoded 30s
    # during a real bulk-collection test (2026-07-03): WorldBank's own
    # client retries up to 3 times with backoff before its /collect endpoint
    # responds, up to ~96s in degraded conditions — Core's dispatch call was
    # timing out first and reporting a misleading empty-message failure.
    # Raised again 2026-07-08: PyTrends is the highest-weighted data source
    # (Demand dimension) and its own client now retries Google Trends rate
    # limits far more persistently (see marketmind-pytrends
    # trends_rate_limit_* settings), up to ~25 minutes worst case for one
    # gap — this must stay comfortably above that ceiling or Core gives up
    # on the HTTP call before PyTrends' own retry loop even finishes.
    collection_dispatch_timeout_seconds: float = 1800.0

    # Default freshness window applied to a coverage_index row when the
    # caller doesn't specify one explicitly (see coverage_service).
    # How old a collection service's last CHECK may be before this service asks
    # it to collect again. The check, not the datum: World Bank and UN Data
    # publish yearly, so a rule written against the data's own date would
    # re-fetch every indicator every day forever.
    # Quanto tempo uma reivindicação de execução vale sem ser renovada. Curto de
    # propósito: ela é renovada a cada progresso, então parada por mais que isso
    # é processo morto. É o que permite a um agente que acabou de subir retomar
    # órfãs na hora, em vez de esperar os 15 minutos da varredura.
    analysis_claim_stale_after_seconds: int = 120
    coverage_stale_after_days: int = 30
    default_coverage_ttl_days: int = 30

    # Freshness window for the country-level macro indicators (WorldBank, OECD,
    # UNData) — GDP, population, internet penetration, inflation, and so on.
    #
    # 90 days rather than the 30 above, because these move on an annual
    # publication cycle and the MOS reads the latest known value regardless of
    # when it was fetched. Data up to three months old changes no conclusion
    # here, and re-downloading it is the single most expensive thing an analysis
    # can do: one measured run spent 3343 of its 3666 seconds re-collecting
    # World Bank figures that had not changed.
    #
    # Staleness stays per indicator, not per country: whatever is still inside
    # the window is left alone and only the individual indicators past it are
    # re-collected.
    country_indicator_ttl_days: int = 90

    # Janela de frescor da DEMANDA (volume de busca e palavras relacionadas),
    # que é coisa diferente das duas acima e por isso tem botão próprio.
    #
    # Até 23/09/2026 não havia janela nenhuma: `missing` no serviço de pytrends
    # queria dizer "nunca coletado", e como só se coleta o que está em
    # `missing`, um volume baixado uma vez valia para sempre. A demanda é
    # justamente o único indicador que muda de mês para mês, e era o único que
    # nunca se renovava.
    #
    # 30 dias porque é a cadência do próprio dado: o volume do Google Ads é
    # mensal, e o gap já nasce com `period_key` do mês corrente. Recoletar antes
    # disso paga por número que não mudou -- e aqui se paga por chamada, ao
    # contrário das fontes macro.
    demand_stale_after_days: int = 30

    # Fase 5 — a análise NUNCA entrega ranking parcial (decisão D3, 2026-08-27:
    # "o usuário vai pagar por essa análise"). Um país sem indicador por falha
    # TRANSITÓRIA da fonte mantém a análise rodando, com backoff exponencial,
    # até o dado vir ou até este orçamento estourar — e aí ela termina em
    # `failed` com motivo, nunca em `done` incompleto (decisão D5).
    #
    # 24h é folga deliberada sobre o incidente que motivou isto: a API do UN
    # Population Data Portal ficou fora por 29 minutos em 25/08/2026 e derrubou
    # 9 dos 24 países de uma análise. O teto existe para não deixar um request
    # preso para sempre, não para expirar em uso normal.
    analysis_retry_budget_seconds: int = 24 * 60 * 60
    # Teto do backoff entre tentativas. Começa em 30s e dobra: 30, 60, 120,
    # 240, 480, 900, 900... Curto no começo porque a maioria das falhas de
    # fonte é blip de segundos; longo depois porque uma queda real não se
    # resolve insistindo rápido.
    analysis_retry_max_backoff_seconds: int = 900
    # Silêncio maior que isto significa que a análise morreu, não que está
    # demorando: é mais que o passo mais longo entre dois batimentos (a
    # cobertura de UM país). Quem varre para retomar respeita esta janela, então
    # análise viva nunca é retomada em paralelo com ela mesma.
    analysis_stale_after_seconds: int = 15 * 60
    # De quanto em quanto tempo a varredura roda. Zero desliga (útil em teste).
    analysis_sweep_interval_seconds: int = 5 * 60

    # Fase 2 — product catalog resolution pipeline (app/services/catalog_service.py).
    # A chave da OpenAI continua aqui porque `GptProvider` (app/services/llm/providers.py)
    # a usa quando algum call site é apontado para "gpt". O que sumiu foi o
    # passo de EMBEDDING: medido e retirado em 2026-08-14 (nenhum limiar de
    # distância separa "mesmo produto" de "produto vizinho" neste catálogo), e
    # a coluna `product_catalog.embedding` foi removida na migration 027.
    # Ver docs/marketmind-transparencia-tecnica.md §1.3.
    openai_api_key: str = ""

    # Busca semântica do catálogo (passo 3a, migration 052). O vetor voltou em
    # 2026-10-13 com OUTRO papel: não aceita match nenhum, só escolhe QUAIS
    # entidades vão para o LLM do passo 3 — antes a lista era "mesma categoria,
    # mais recentes primeiro", que deixa o candidato certo de fora assim que o
    # catálogo passa do teto. A medição de 14/08 continua valendo para
    # aceitação; ela não diz nada contra recall.
    #
    # Voyage AI porque a Anthropic não tem endpoint de embeddings e recomenda
    # este. Sem chave, o passo 3a desliga e a seleção volta a ser a antiga.
    voyage_api_key: str = ""
    voyage_api_base_url: str = "https://api.voyageai.com/v1"
    voyage_embedding_model: str = "voyage-4"
    # Tem que bater com a coluna `product_aliases.embedding` (migration 052).
    voyage_embedding_dimensions: int = 1024
    voyage_timeout_seconds: float = 20.0
    # Quantas entidades o vizinho mais próximo põe na frente da lista do LLM.
    catalog_semantic_top_k: int = 10
    # Tamanho da fila de candidatos do HNSW na consulta: maior = recall maior e
    # consulta mais lenta. 40 é o default do pgvector; ver
    # scripts/benchmark_semantic_recall.py para a curva medida.
    catalog_semantic_ef_search: int = 40

    # Fase 5 — identificação de produto por foto (app/services/vision_client.py,
    # product_identification_service.py). Anthropic tem endpoint de visão
    # nativo (ao contrário de embeddings) — usa o SDK oficial, não um stopgap.
    anthropic_api_key: str = ""
    # Only needed for an ORGANISATION-scoped key. Anthropic rejects those
    # without it: "This API key is not scoped to a workspace, so this request
    # must include the anthropic-workspace-id header." A workspace-scoped key
    # carries the workspace in the key itself and needs nothing here, so this
    # stays empty for most setups.
    anthropic_workspace_id: str = ""
    # How long Claude keeps a cached system prompt: "1h" or "5m". Chosen by the
    # start-to-start gap between calls that share it. Measured 2026-09-28, the
    # analyses here start 5-60 min apart — past a 5-minute entry, so on "5m"
    # every call would pay the write premium (1.25x) and read nothing; "1h"
    # writes at 2x and pays off from the third call within the hour. "5m" is
    # the cheaper choice only for bursts (an eval run), where calls start
    # under 5 minutes apart and keep the entry warm anyway.
    llm_prompt_cache_ttl: Literal["5m", "1h"] = "1h"

    # Langfuse tracing of the model calls (app/observability.py). Both keys or
    # nothing: unset means untraced, which is allowed — a missing trace loses an
    # explanation, it does not corrupt an answer.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # Which Langfuse label the prompts are fetched from. Runtime stays on
    # "production"; scripts/run_eval.py points a single run at another label to
    # score a candidate version against the same cases.
    langfuse_prompt_label: str = "production"

    # There is no anthropic_vision_model here: the vision model is chosen by
    # vision_llm_model above, like every other call site. A separate setting
    # existed until 2026-08-13 and was read by nothing — it predates the
    # resolver and only made the two look like they disagreed.
    #
    # Kept from that era, because it still governs the model choice: the
    # identify-image call returns a rich per-country `markets` payload (real
    # search terms for every requested country), and Haiku 4.5 degenerated on
    # that large structured output (repeated-token loop -> invalid JSON). Do
    # not point vision_llm_model at a small model without re-testing that
    # payload.

    # Limiar de similaridade do trigram (pg_trgm) para aceitar um alias fuzzy
    # sem cair para o passo 3 (LLM).
    #
    # É também o valor aplicado a `pg_trgm.similarity_threshold` por transação
    # em ProductRepository.trigram_search, para que o operador `%` — o único
    # que usa os índices GIN — filtre exatamente neste ponto.
    #
    # ⚠️ Calibrado apenas contra termos POSITIVOS (89 termos, 2026-08-14). O
    # conjunto negativo (excluded_terms) só passou a existir como guarda em
    # 2026-08-25; vale re-medir os dois lados com
    # `scripts/benchmark_catalog_resolution.py` antes de mexer neste número.
    catalog_trigram_similarity_threshold: float = 0.4
    log_level: str = "INFO"

    # Path prefix a reverse proxy serves this API under and strips before
    # forwarding — "/api" on Azure, empty locally. Only affects the URLs FastAPI
    # generates (Swagger, ReDoc, the OpenAPI document); routing is unchanged.
    root_path: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.database_user}:{quote(self.database_password, safe='')}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def database_url_sync(self) -> str:
        # Alembic runs through psycopg2, which reads `sslmode` straight off the
        # DSN. The async engines cannot — they get an SSLContext instead, built
        # from the same setting in app/db/ssl_config.py.
        return (
            f"postgresql://{self.database_user}:{quote(self.database_password, safe='')}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
            f"{sslmode_query_suffix(self.database_ssl_mode)}"
        )

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """Parse the comma-separated CORS origins into a list. A bare "*"
        allows any origin (dev convenience)."""
        raw = self.cors_allow_origins.strip()
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


settings = Settings()
