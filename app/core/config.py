from urllib.parse import quote

from pydantic_settings import BaseSettings

from app.db.ssl_config import sslmode_query_suffix


class Settings(BaseSettings):
    app_name: str = "marketmind-core"
    app_env: str = "local"
    app_port: int = 8000

    # CORS — the browser frontend (marketmind-ai, Astro dev server) calls this
    # API directly, so its origin must be allowed. Comma-separated list of
    # origins; override via the CORS_ALLOW_ORIGINS env var. Use "*" to allow any
    # origin (dev only). Default covers the Astro dev/preview ports on localhost.
    cors_allow_origins: str = (
        "http://localhost:4321,http://127.0.0.1:4321,"
        "http://localhost:4322,http://127.0.0.1:4322"
    )

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

    # Read-only connections to the 4 collection microservices.
    # These are separate Postgres instances (each service owns its own database).
    # See README "Conectividade entre serviços" for why explicit host/port are required
    # instead of relying on a shared Docker network.
    pytrends_db_host: str = "localhost"
    pytrends_db_port: int = 5432
    pytrends_db_name: str = "marketmind_pytrends"
    pytrends_db_user: str = "marketmind_readonly"
    pytrends_db_password: str = "marketmind_readonly_password"

    worldbank_db_host: str = "localhost"
    worldbank_db_port: int = 5434
    worldbank_db_name: str = "marketmind_worldbank"
    worldbank_db_user: str = "marketmind_readonly"
    worldbank_db_password: str = "marketmind_readonly_password"

    oecd_db_host: str = "localhost"
    oecd_db_port: int = 5433
    oecd_db_name: str = "marketmind_oecd"
    oecd_db_user: str = "marketmind_readonly"
    oecd_db_password: str = "marketmind_readonly_password"

    undata_db_host: str = "localhost"
    undata_db_port: int = 5435
    undata_db_name: str = "marketmind_united_nations_data"
    undata_db_user: str = "marketmind_readonly"
    undata_db_password: str = "marketmind_readonly_password"

    # HTTP APIs of the 4 collection services (used to dispatch on-demand
    # collection and poll ingestion runs — see app/services/collection_client.py).
    # Same host.docker.internal strategy as the DB connections above; ports
    # match each service's own .env (already made distinct there to run
    # simultaneously): pytrends=8000, oecd=8001, undata=8002, worldbank=8003.
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

    # marketmind-orchestrator API base URL and feature flag
    use_new_orchestrator: bool = False
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

    # Fase 2 — product catalog resolution pipeline (app/services/catalog_service.py).
    # A chave da OpenAI continua aqui porque `GptProvider` (app/services/llm/providers.py)
    # a usa quando algum call site é apontado para "gpt". O que sumiu foi o
    # passo de EMBEDDING: medido e retirado em 2026-08-14 (nenhum limiar de
    # distância separa "mesmo produto" de "produto vizinho" neste catálogo), e
    # a coluna `product_catalog.embedding` foi removida na migration 027.
    # Ver docs/marketmind-transparencia-tecnica.md §1.3.
    openai_api_key: str = ""

    # Fase 5 — identificação de produto por foto (app/services/vision_client.py,
    # product_identification_service.py). Anthropic tem endpoint de visão
    # nativo (ao contrário de embeddings) — usa o SDK oficial, não um stopgap.
    anthropic_api_key: str = ""
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

    @property
    def pytrends_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.pytrends_db_user}:{quote(self.pytrends_db_password, safe='')}"
            f"@{self.pytrends_db_host}:{self.pytrends_db_port}/{self.pytrends_db_name}"
        )

    @property
    def worldbank_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.worldbank_db_user}:{quote(self.worldbank_db_password, safe='')}"
            f"@{self.worldbank_db_host}:{self.worldbank_db_port}/{self.worldbank_db_name}"
        )

    @property
    def oecd_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.oecd_db_user}:{quote(self.oecd_db_password, safe='')}"
            f"@{self.oecd_db_host}:{self.oecd_db_port}/{self.oecd_db_name}"
        )

    @property
    def undata_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.undata_db_user}:{quote(self.undata_db_password, safe='')}"
            f"@{self.undata_db_host}:{self.undata_db_port}/{self.undata_db_name}"
        )


settings = Settings()
