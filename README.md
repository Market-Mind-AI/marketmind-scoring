# MarketMind Scoring

> **A arquitetura do sistema inteiro está em
> [docs/marketmind-arquitetura.md](docs/marketmind-arquitetura.md)**, e o que
> cada número do resultado significa em
> [docs/marketmind-transparencia-tecnica.md](docs/marketmind-transparencia-tecnica.md).
> Comece por lá; este README cobre só este serviço.

O **método** do MarketMind. Já se chamou `marketmind-core` e orquestrava a
análise; a orquestração é do agente (`marketmind-orchestrator`) desde 21/09/2026,
e o que ficou aqui é o que nenhum outro serviço pode fazer.

Não recolhe dado de API externa — isso é dos 4 microserviços de coleta
(PyTrends/Google Ads, WorldBank, OECD, UnitedNationsData). Este serviço:

- Mantém el catálogo único de productos (`product_catalog`/`product_aliases`),
  particionado por marca (`brands`, migration 031).
- **No** controla frescura: cada servicio de recolección es dueño de la suya y
  responde en `GET /freshness`. La `coverage_index` fue removida en la
  migration 036 por ser una segunda copia del mismo hecho.
- Calcula los indicadores determinísticos del Capítulo 4 (`indicator_values`),
  usando una referencia de normalización fija y versionada por período
  (`normalization_reference`), no relativa a la consulta de cada usuario.
- Calcula el MOS del Capítulo 5 (`mos_results`) — **Rank Reciprocal + TOPSIS
  contra anclas fijas (ideal 100 / anti-ideal 0)**, siempre por país,
  independiente, nunca agregado entre países — con pesos fijos y versionados
  (`indicator_weights`). **No hay techo no compensatorio** (decisión del
  usuario, 2026-07-15): `mos_before_cap`/`capped_by` siguen en el contrato de
  la API, siempre `mos`/`NULL`.
- Pide la recolección que falta a los 4 servicios, por HTTP, y espera el
  resultado (`jobs`/`job_tasks`).
- Hace las llamadas a modelo que el método necesita (visión, enriquecimiento de
  términos, mercados de referencia, arbitraje del catálogo, narrativa) y las
  traza en Langfuse — ver [Observabilidad](#observabilidad).

## Lo que NO hace

- No recolecta datos de Google Ads, World Bank, OECD o UN Data directamente.
- No abre las bases de los 4 servicios de recolección — ni para leer. Todo
  pasa por sus APIs HTTP (`PYTRENDS_API_BASE_URL`, etc.). No existe usuario
  read-only, ni `db/external.py`, ni `scripts/create_readonly_roles.sql`.
- No usa LLM para calcular o alterar indicadores, pesos o MOS — el LLM solo
  participa en la identificación de producto, en la traducción de términos por
  mercado, en la elección de candidatos de mercado de referencia, en el
  arbitraje de identidad del catálogo (paso 3) y en la explicación de
  resultados ya calculados.
- No orquesta la análisis: el grafo vive en `marketmind-orchestrator`, que llama
  a `/internal/*` con `INTERNAL_API_TOKEN` y reanuda análisis huérfanas.

## Arquitectura

Misma Clean Architecture usada en los otros 4 servicios del workspace:

```
app/
  api/routes/         # 41 endpoints HTTP (sin lógica de negocio)
  core/               # Configuración, logging, excepciones
  domain/             # Enums de dominio
  db/models/          # Modelos SQLAlchemy (base propia; 54 migrations en alembic/)
  repositories/       # Capa de acceso a datos
  schemas/            # Schemas Pydantic de request/response
  services/           # Catálogo, cobertura, indicadores, MOS, clientes LLM
  observability.py    # Tracing Langfuse (generations, observations de colecta)
  pricing.py          # Precio de cada llamada a modelo, por la tabla de Langfuse
  prompts.py          # Prompts leídos de Langfuse Prompt Management (fallback: código)
  utils/              # Utilidades
```

~15,8k líneas en `app/`.

## Stack

Python 3.12, FastAPI + Pydantic v2, SQLAlchemy 2.x + asyncpg, Alembic,
PostgreSQL 16 (`pgvector/pgvector:pg16`, extensiones `vector` y `pg_trgm`),
Docker + Docker Compose, Poetry — idéntico a los otros 4 servicios. Langfuse
para tracing, prompts y evaluación.

## Conectividad entre servicios (leer antes de ejecutar)

Este servicio habla con los 4 recolectores **solo por HTTP**: despacha una
recolección (`POST /.../collect`), hace polling del run (`GET /runs/{id}`) y lee
el dato ya almacenado por sus endpoints de lectura. Como corre en un contenedor
y los otros servicios publican puertos en el host, las URLs por defecto usan
`host.docker.internal`:

| Servicio | Puerto HTTP en el host | Variable |
|---|---|---|
| PyTrends (Google Ads / DataForSEO) | 8000 | `PYTRENDS_API_BASE_URL` |
| OECD | 8001 | `OECD_API_BASE_URL` |
| UNData | 8002 | `UNDATA_API_BASE_URL` |
| WorldBank | 8003 | `WORLDBANK_API_BASE_URL` |
| Scoring (este) | 8004 | — |

Los 4 servicios deben estar arriba (`docker compose up -d` en cada directorio)
antes de levantar este. `GET /health` reporta la conectividad con las 4 APIs;
`GET /health/nightly` la verifica a fondo.

## Running Locally

```bash
poetry install
cp .env.example .env
docker compose up postgres -d
alembic upgrade head
uvicorn app.main:app --reload --port 8004
```

## Running with Docker

```bash
cp .env.example .env
docker compose up --build
docker compose exec marketmind-scoring-api alembic upgrade head
```

API en `http://localhost:8004`, Swagger en `http://localhost:8004/docs`.

## pgAdmin

Instancia única del workspace, consolidada aquí (los otros 4 servicios ya no
tienen pgAdmin propio). `http://localhost:5054` — `admin@marketmind.dev` /
`admin`. Viene con los 5 servidores pre-registrados (`pgadmin/servers.json` +
`pgadmin/pgpass`): `marketmind-core` — el servidor sigue registrado con ese
nombre porque la BASE DE DATOS no fue renombrada (ver
`docs/marketmind-setup-ecossistema.md`) —, `marketmind-pytrends`,
`marketmind-oecd`, `marketmind-worldbank`, `marketmind-undata`. Es una
comodidad de inspección para el desarrollador: el servicio en sí nunca abre
esas bases.

## Modelo de datos

| Tabla | Rol |
|---|---|
| `product_catalog` / `product_aliases` / `product_excluded_terms` | Identidad única de producto; un vector Voyage por alias (`product_aliases.embedding`, HNSW, migration 052); términos excluidos como evidencia negativa |
| `brands` / `brand_markets` / `brand_reference_candidates` / `brand_champions` | La marca como partición de la identidad (migration 031): vocabulario, candidatos y campeón por (producto, marca) |
| `countries` | Tabla canónica ISO3 con los códigos de las 4 fuentes. **Cesta dinámica**: la migration 043 cargó 127 países MEDIDOS como teniendo los datos, con `analysis_ready = false`; el pre-calentamiento macro (`POST /admin/coverage/prewarm-macro`) los promueve cuando su dato llega. La 054 excluye CN/RU/BY porque el recolector de demanda los rechaza. `GET /countries` devuelve solo los `analysis_ready` |
| `monthly_snapshots` | Índice de cadencia mensual |
| `indicator_values` | Indicadores del Cap. 4, normalizados, con estado de disponibilidad |
| `normalization_reference` | Bounds fijos de normalización, versionados por período |
| `indicator_weights` | Pesos Rank Reciprocal de las dimensiones, fijos y versionados por período |
| `mos_results` | MOS por país, siempre independiente, con `library_mos` (pymcdm) para auditoría |
| `metric_explanations` / `metric_band_phrases` | Texto determinístico por métrica y banda, que el LLM final solo narra |
| `analysis_requests` | Registro de cada análisis (estado, heartbeat, reporte) que el orchestrator reclama y completa vía `/internal/*` |
| `jobs` / `job_tasks` | Recolección asíncrona (nunca de cálculo de MOS) — `job_task` = exactamente un gap / una llamada de recolección |

## Cobertura y recolección bajo demanda

- `POST /coverage/check` — lectura pura: pregunta a cada recolector qué gaps ya
  están frescos, sin disparar recolección. **Sin consumidor hoy (API manual)**.
- `POST /jobs` / `GET /jobs/{id}` — para cada gap: omite si ya está fresco, se
  adjunta a una task pending/running si otro caller ya recolecta el mismo gap
  (dedup de carrera), o dispara la recolección en el servicio correcto
  (`app/services/collection_client.py`) y hace polling hasta concluir.
  **Sin consumidor hoy (API manual)** — el orchestrator usa
  `POST /internal/coverage/ensure` y `POST /internal/candidates/coverage`, que
  pasan por el mismo `CoverageService`.
- **Dedup de carrera:** índice único parcial en `job_tasks` (`status IN
  ('pending','running')`) es el backstop; `SELECT ... FOR UPDATE SKIP LOCKED`
  (`app/repositories/job_repository.py`) es el camino rápido.
- **Traducción de código de país por fuente:** `collection_client.py` resuelve
  el código que cada servicio espera a partir de `countries`. Para la demanda,
  `location`/`language` viajan en `collection_params`, sacados del
  `localized_markets[ISO3]` del producto.
- **Frescura por fuente:** macro 90 días (`COUNTRY_INDICATOR_TTL_DAYS`),
  demanda 30 días (`DEMAND_STALE_AFTER_DAYS`), ambas decididas aquí pero
  medidas contra el `last_checked_at` que cada recolector reporta.

## Catálogo de productos

Resolución de identidad del más barato al más caro
(`app/services/catalog_service.py`, `POST /catalog/resolve`), siempre dentro de
una partición de marca:

1. **Alias exacto normalizado** (`product_aliases`, O(1)) —
   `app/utils/text.normalize_alias`. La identidad es el par (nombre, categoría).
2. **Fuzzy trigram** (`pg_trgm`, GIN sobre `alias_normalized`) **con veto por
   producto antagónico**: la entidad solo se acepta si el texto está más cerca
   de los términos que ella declara SER que de los que declara NO ser
   (`excluded_terms`/`antagonist_terms`). Empate o inversión escala al paso 3.
   Medido: 44/102 términos excluidos resolvían a la entidad que los excluye;
   con el veto, 0/102, sin perder positivos (`scripts/eval_antagonist_veto.py`).
3. **Decisión del LLM** entre candidatos del catálogo (`catalog_matcher.py`).
   Los candidatos vienen de la **búsqueda semántica** (3a): embeddings Voyage
   (`voyage-4`, 1024 dims, `VOYAGE_API_KEY`) sobre `product_aliases.embedding`
   con HNSW en pgvector (migration 052). **El vector solo recupera candidatos;
   nunca acepta un match** — la medición de 2026-08-14 mostró que ninguna
   distancia separa `freidora de aire` de `freidora de aceite`.
4. **Crea entidad nueva** — `entity_key` uuid4 local, nunca bloqueado por
   dependencia externa.

Un match en los pasos 2 o 3 graba un alias exacto, de modo que el mismo texto
resuelve por el paso 1 la próxima vez. Los `search_terms` de cada mercado
también se indexan como aliases, lo que hace al paso 2 alcanzar otros idiomas.

- `POST /catalog/identify-image` — identificación por visión (imagen y/o
  texto), vision-only y rápida; el enriquecimiento por país y los candidatos de
  mercado de referencia se difieren al `/analyze` en background.
- `POST /admin/catalog/backfill-embeddings`, `backfill-terms`,
  `backfill-antagonists`, `split-brand` — mantenimiento del catálogo.

## Motor de indicadores (Capítulo 4)

Una función de valor bruto por indicador
(`app/services/indicators/{pytrends,worldbank,oecd,undata}.py`), fórmulas en
`docs/marketmind-capitulo4-metricas.md`. `indicator_engine.py` calcula los
indicadores país a país (compartidos entre productos, `entity_key=NULL`) + los
de demanda específicos del producto, normaliza contra `normalization_reference`
y graba en `indicator_values`.

- **Demanda = volumen absoluto de búsqueda de Google Ads (DataForSEO), no Google
  Trends** (cambio de 2026-07-15): `trend_score` es la SUMA del volumen mensual
  de los términos aprobados del país (más expansión por `keywords_for_keywords`),
  normalizada linealmente contra el techo del mercado de referencia
  (`/internal/demand-ceiling`). Growth, Stability e Intent salen de la misma
  serie mensual.
- `POST /indicators/compute` — calcula todo para un país (+ producto).
- **La normalización no es relativa a la consulta:** bounds de
  `normalization_reference` (población fija, versionada por período), nunca de
  los países de la consulta actual — eso es lo que hace al MOS absoluto.
- **Fallback de inflación:** OECD primero; World Bank solo si la serie OECD no
  está (Cap. 4 §4.7).

## Motor de MOS (Capítulo 5)

Fórmulas en `docs/marketmind-capitulo5-mos.md`. Agregación en 2 etapas: los
indicadores atómicos se agrupan en dimensiones
(`app/services/mos/dimension_mapping.py`), los pesos de las dimensiones vienen
de **Rank Reciprocal** (`app/services/mos/rank_reciprocal.py`) y TOPSIS
(`app/services/mos/topsis.py`) calcula el score final — siempre por (producto,
marca, país, período).

- **Pesos vía Rank Reciprocal, no Entropy Weight ni ROC.** Entropy Weight fue
  abandonado el 2026-07-03 (subvaloraba Demand solo porque el interés variaba
  poco entre los países de la muestra); ROC lo sustituyó hasta el 2026-07-08,
  cuando la 6ª dimensión forzó una fórmula que acercara Market Size y Economic
  Readiness sin ajustar números a mano. El orden de prioridad es una decisión
  explícita del usuario; `w_j = (1/j) / Σ(1/k)`. Demand Volume es la primera.
- `POST /admin/mos/refresh-weights` — escribe los pesos del período
  (`weight_calibration_service.py`). Versionados por `period_key`, nunca
  sobrescriben un período cerrado: auditoría de "qué peso se usó".
- `POST /mos/compute` — un resultado independiente por país: valida los
  indicadores obligatorios (Cap. 5 §5.3), arma el vector de sub-scores por
  dimensión, aplica los pesos, corre TOPSIS contra **anclas fijas 100/0** (no
  "mejor/peor observado entre los países comparados" — el Cap. 4 ya normaliza
  contra población fija, así que el paso de normalización vectorial del TOPSIS
  desaparece). Excluido con `excluded_reason`, nunca descartado en silencio,
  cuando falta un indicador obligatorio o un peso.
- **Sin techo no compensatorio** (2026-07-15): la demanda ya es la dimensión
  de mayor peso; no se le permite además cortar el score final.
  `mos_before_cap`/`capped_by` se mantienen en el contrato por compatibilidad.
- **Validación con biblioteca, no producción:** cada cálculo también corre el
  TOPSIS + Entropy Weight *clásicos* de `pymcdm`
  (`app/services/mos/library_reference.py`), guardados en
  `library_topsis_score`/`library_mos`. Es el único lugar donde Entropy Weight
  sigue existiendo, y nunca decide el MOS real.
- `POST /internal/narrate`, `/narrate/revise`, `/evidence` — el paquete
  determinístico de evidencia (`explanation_builder.py`) y la narrativa del
  LLM, que solo relata lo ya calculado.

## Observabilidad

- **Tracing en Langfuse** (`app/observability.py`): cada llamada a modelo es
  una *generation* con nivel (`ERROR` con el tipo de excepción, `WARNING` si el
  proveedor no reportó tokens o si el prompt vino del código y no de Langfuse),
  uso (tokens de entrada/salida, cache read y cache write por TTL) y costo. El
  `traceparent` W3C que manda el orchestrator pone todo dentro del trace de la
  análisis. Cada despacho de recolección es una *observation* con su latencia,
  y lo que DataForSEO cobró entra como hijo con precio.
- **Costo en dólares** (`app/pricing.py`): la tabla de precios se lee de la
  API de Langfuse al arrancar (reintentada cada minuto hasta responder); una
  llamada que no se puede tasar queda como `None`, nunca como cero.
- **Headers de costo** en cada respuesta: `X-LLM-Input-Tokens`,
  `X-LLM-Output-Tokens`, `X-LLM-Cache-Read-Tokens`, `X-LLM-Cache-Write-Tokens`,
  `X-LLM-Calls`, `X-LLM-Cost-USD`, `X-LLM-Cost-By-Model`,
  `X-LLM-Calls-Without-Cost`, y `X-DataForSEO-Cost-USD` / `X-DataForSEO-Calls`
  (`app/services/demand_spend.py`). El orchestrator los suma en la línea COST
  de la corrida.
- **Prompts en Langfuse Prompt Management** (`app/prompts.py`,
  `app/prompt_registry.py`): se editan allá; el texto en código es el fallback
  y la semilla de `scripts/sync_prompts.py`.
- `scripts/probe_tracing.py` — manda un trace estándar por situación que las
  vistas de Langfuse filtran (ok, warning, error, slow, high cost, high tokens),
  sin llamar a ningún modelo.
- `scripts/run_eval.py` — corre datasets de casos como experimento de Langfuse
  (`identify-product`, `reference-markets`), dice cuánto va a costar y pregunta
  antes de gastar. Los datasets viven en `evals/`.
- `scripts/benchmark_*.py`, `scripts/eval_antagonist_veto.py` — mediciones del
  catálogo que respaldan los números citados arriba (`evaluation/`).
- Variables: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`,
  `LANGFUSE_TRACING_ENVIRONMENT` (`local`, `azure-dev`). Sin claves = sin
  trace, y eso está permitido.

## Running Tests

La suite **trunca tablas**. `tests/conftest.py` se niega a correr contra una
base cuyo nombre no contenga `test`. Crear y migrar la base dedicada una vez:

```bash
DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test alembic upgrade head
```

y correr siempre así:

```bash
DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test pytest tests -q
```

Nunca `pytest` a secas contra `marketmind_core`: borra `normalization_reference`
y los datos de desarrollo. ~600 funciones de test (672 casos con parametrización).

## Running Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

54 migrations en `alembic/versions/`.

## Linting and Type Checking

```bash
ruff check .
ruff format .
mypy .
```
