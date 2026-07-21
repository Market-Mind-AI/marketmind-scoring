from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "marketmind-core"
    app_env: str = "local"
    app_port: int = 8000

    # Core's own database (product catalog, coverage, indicators, MOS, jobs)
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "marketmind_core"
    database_user: str = "marketmind"
    database_password: str = "marketmind_password"

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
    # Final LLM inference service (marketmind-ollma-llm, port 8005) — turns the
    # deterministic MOS + context into a business narrative via a LOCAL Ollama
    # model (qwen3:14b). Best-effort: if it's down/slow the analysis still
    # succeeds with the numbers, just without the narrative.
    ollama_llm_api_base_url: str = "http://host.docker.internal:8005"
    ollama_llm_timeout_seconds: float = 300.0
    ollama_llm_narrative_language: str = "pt"

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

    # Fase 2 — product catalog resolution pipeline (app/services/catalog_service.py).
    # Embedding step uses OpenAI for now (explicit user decision, 2026-07-02):
    # Anthropic has no first-party embeddings endpoint (their own docs point
    # to Voyage AI); OpenAI was chosen as a stopgap ahead of a planned move to
    # a locally-hosted model — see memory marketmind_core_architecture.
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536

    # Fase 5 — identificação de produto por foto (app/services/vision_client.py,
    # product_identification_service.py). Anthropic tem endpoint de visão
    # nativo (ao contrário de embeddings) — usa o SDK oficial, não um stopgap.
    anthropic_api_key: str = ""
    # Haiku 4.5 (not Opus): identify-image is a simple, high-volume
    # classification task (product name/brand/category/age from one photo),
    # not deep reasoning — cost was the deciding factor (2026-07-08, user
    # request) over Opus's extra capability, which this task doesn't need.
    # identify-image now returns a rich per-country `markets` payload (real
    # search terms for every requested country). Haiku 4.5 degenerated on that
    # large structured output (repeated-token loop -> invalid JSON), so the
    # default is a more capable model. Overridable via ANTHROPIC_VISION_MODEL.
    anthropic_vision_model: str = "claude-opus-4-8"

    # Trigram (pg_trgm) similarity threshold to accept a fuzzy alias match
    # without falling through to the more expensive embedding step.
    catalog_trigram_similarity_threshold: float = 0.4
    # Cosine distance ceiling (0 = identical, 2 = opposite) to accept an
    # embedding match. Loosened twice from the original 0.15 after live
    # testing against real `text-embedding-3-small` calls (2026-07-03):
    # true cross-language paraphrases of the same product measured
    # 0.397-0.616 cosine distance ("air fryer" vs. "freidora de aire
    # caliente" / "friggitrice ad aria calda senza olio" / etc.) — 0.15 and
    # even 0.35 never accepted any of them. Settled on 0.5 as a middle
    # ground (accepts most tested paraphrases, still rejects the most
    # distant one at 0.616) — explicit user tradeoff accepting more
    # false-positive-merge risk in exchange for catching genuine cross-
    # language/rewording matches. Revisit with real product data, not just
    # this handful of synthetic test phrases.
    catalog_embedding_distance_threshold: float = 0.5

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def pytrends_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.pytrends_db_user}:{self.pytrends_db_password}"
            f"@{self.pytrends_db_host}:{self.pytrends_db_port}/{self.pytrends_db_name}"
        )

    @property
    def worldbank_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.worldbank_db_user}:{self.worldbank_db_password}"
            f"@{self.worldbank_db_host}:{self.worldbank_db_port}/{self.worldbank_db_name}"
        )

    @property
    def oecd_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.oecd_db_user}:{self.oecd_db_password}"
            f"@{self.oecd_db_host}:{self.oecd_db_port}/{self.oecd_db_name}"
        )

    @property
    def undata_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.undata_db_user}:{self.undata_db_password}"
            f"@{self.undata_db_host}:{self.undata_db_port}/{self.undata_db_name}"
        )


settings = Settings()
