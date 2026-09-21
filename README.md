# MarketMind Core

Servicio de orquestación de MarketMind. No recolecta datos de ninguna API
externa — eso es trabajo de los 4 microservicios ya existentes (PyTrends,
WorldBank, OECD, UnitedNationsData). El Core:

- Mantiene el catálogo único de productos (`product_catalog`/`product_aliases`).
- Sabe qué ya fue recolectado y para qué período (`coverage_index`,
  `monthly_snapshots`) — los 4 servicios de recolección no tienen esa noción.
- Calcula los indicadores determinísticos del Capítulo 4 (`indicator_values`),
  usando una referencia de normalización fija y versionada por período
  (`normalization_reference`), no relativa a la consulta de cada usuario.
- Calcula el MOS del Capítulo 5 (`mos_results`) — Entropy Weight + TOPSIS,
  **siempre por país, independiente, nunca agregado entre países** — con pesos
  fijos y versionados (`indicator_weights`) y un techo no compensatorio
  basado en demanda real.
- Orquesta la recolección bajo demanda cuando falta un dato (`jobs`/`job_tasks`).

## Lo que NO hace

- No recolecta datos de Google Trends, World Bank, OECD o UN Data directamente.
- No escribe en las bases de los 4 servicios de recolección — solo lee, con
  usuario read-only.
- No usa LLM para calcular o alterar indicadores, pesos o MOS — el LLM solo
  participa en la identificación de producto (texto candidato) y en la
  explicación de resultados ya calculados.

## Arquitectura

Misma Clean Architecture usada en los otros 4 servicios del workspace:

```
app/
  api/routes/         # Endpoints HTTP (sin lógica de negocio)
  core/               # Configuración, logging, excepciones
  domain/             # Enums y entidades de dominio
  db/models/          # Modelos SQLAlchemy (base propia del Core)
  db/external.py      # Conexiones read-only a las 4 bases externas (SQLAlchemy Core, sin ORM)
  repositories/        # Capa de acceso a datos (base propia)
  schemas/            # Schemas Pydantic de request/response
  services/           # Orquestación
  utils/              # Utilidades
```

## Stack

Python 3.12, FastAPI + Pydantic v2, SQLAlchemy 2.x + asyncpg, Alembic,
PostgreSQL 16, Docker + Docker Compose, Poetry — idéntico a los otros 4
servicios.

## Conectividad entre servicios (leer antes de ejecutar)

El Core necesita leer directamente de las bases de los 4 microservicios de
recolección (decisión de simplicidad documentada en
`docs/marketmind-implementation-plan.md`, sección 2). **Esto no funciona
automáticamente hoy**: los `docker-compose.yml` de los 4 servicios declaran
una red `marketmind-network` sin `external: true`, por lo que Docker Compose
aísla una red por proyecto — a pesar del nombre igual, no son la misma red
física.

En vez de editar los 4 repositorios existentes para exponer una red
compartida (más invasivo), el Core se conecta por los **puertos ya expuestos
en el host** por cada servicio:

| Servicio | Puerto por defecto del propio servicio | Puerto recomendado para ejecutar todo junto |
|---|---|---|
| PyTrends | `DATABASE_PORT` (default 5432) | 5432 |
| OECD | `DATABASE_PORT_HOST` (default 5433) | 5433 |
| WorldBank | `DATABASE_EXTERNAL_PORT` (default 5432) | **5434** (ajustar en el `.env` del WorldBank) |
| UNData | `DATABASE_EXTERNAL_PORT` (default 5432) | **5435** (ajustar en el `.env` del UNData) |
| Core (este) | `DATABASE_PORT_HOST` (default 5436) | 5436 |

**Pasos manuales necesarios antes de ejecutar el Core por primera vez:**

1. En los `.env` de WorldBank y de UNData, defina `DATABASE_EXTERNAL_PORT=5434` y
   `DATABASE_EXTERNAL_PORT=5435` respectivamente (evita colisión de puerto 5432
   con PyTrends cuando los 3 se ejecutan al mismo tiempo en el mismo host).
2. Ejecute `scripts/create_readonly_roles.sql` contra cada una de las 4 bases
   (instrucciones al inicio del archivo) — crea el usuario `marketmind_readonly`
   usado por el Core.
3. Confirme que los 4 servicios están en pie (`docker compose up -d` en cada
   directorio) antes de levantar el Core.

El endpoint `GET /health` del Core verifica y reporta la conectividad con las 4
fuentes — es la forma más rápida de confirmar que los pasos anteriores
funcionaron.

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
`admin`. Ya viene con los 5 servidores pre-registrados (`pgadmin/servers.json` +
`pgadmin/pgpass`): `marketmind-core`, `marketmind-pytrends`,
`marketmind-oecd`, `marketmind-worldbank`, `marketmind-undata` — los 4
externos alcanzados mediante la misma estrategia de `host.docker.internal` +
puertos de host descrita arriba, por lo que dependen de los mismos pasos
manuales (puertos distintos + servicios en pie). Si algún servidor aparece con
error de conexión, es señal de que el puerto de ese servicio en el host no
coincide con `pgadmin/pgpass` — revise la tabla de la sección anterior.

## Modelo de datos (Fase 0)

| Tabla | Rol |
|---|---|
| `product_catalog` / `product_aliases` | Identidad única de producto (entity_key siempre local, `google_mid` es enriquecimiento opcional) |
| `countries` | Tabla canónica ISO3 con correspondencia de las 4 fuentes — sembrada (migration 003) con una cesta de ~27 países (G20 + Iberia/Nórdicos) |
| `monthly_snapshots` | Índice de cadencia mensual (mes abierto/cerrado) |
| `coverage_index` | Qué ya fue recolectado, por indicador/período |
| `indicator_values` | Indicadores del Cap. 4, normalizados, con ventana y estado de disponibilidad |
| `normalization_reference` | Bounds fijos de normalización, versionados por período |
| `indicator_weights` | Pesos del Entropy Weight, fijos y versionados por período, agrupados por dimensión |
| `mos_results` | MOS por país, siempre independiente, con techo anticompensación expuesto |
| `jobs` / `job_tasks` | Orquestación asíncrona de recolección (nunca de cálculo de MOS) — `job_task` = exactamente un gap de cobertura / una llamada de recolección |

Ver `docs/marketmind-implementation-plan.md` para el racional completo y las
próximas fases (motor de indicadores, motor de MOS, orquestación del frontend).

## Fase 1 — Cobertura y recolección bajo demanda (implementada)

- `POST /coverage/check` — lectura pura: reporta qué gaps ya están cubiertos
  (fresh en `coverage_index`), sin disparar recolección.
- `POST /jobs` — para cada gap: omite si ya está fresco, se adjunta a una task
  pending/running ya existente si otro caller ya está recolectando el mismo gap
  (dedup de carrera), o dispara la recolección en el servicio correcto (`app/services/collection_client.py`)
  y hace polling en `GET /runs/{id}` hasta concluir. Síncrono en esta fase — la
  Fase 5 decide cuándo esto necesita convertirse en 202+SSE.
- `GET /jobs/{id}` — estado de un job ya creado.
- `POST /admin/reconciliation/run` — compara `coverage_index` contra las
  tablas crudas de los 3 servicios país por país (WorldBank/OECD/UNData; PyTrends
  queda fuera en esta fase — depende del catálogo de productos de la Fase 2 para
  resolver `entity_key` → `keyword`). Diagnóstico bajo demanda, sin scheduler
  en proceso (ver `app/services/reconciliation_service.py`).
- **Dedup de carrera:** índice único parcial en `job_tasks` (`status IN
  ('pending','running')`) es el backstop real; `SELECT ... FOR UPDATE SKIP
  LOCKED` (`app/repositories/job_repository.py`) es el camino rápido no propenso
  a condiciones de carrera que la mayoría de los callers recorre.
- **Traducción de código de país por fuente:** `app/services/collection_client.py`
  resuelve el código que cada servicio espera a partir de la tabla `countries`
  (con fallback a ISO3/ISO2 cuando el campo específico de la fuente está nulo —
  solo UNData exige `location_id` numérico explícito, sin fallback posible).
- **Bug corregido durante la implementación:** todo modelo con columna
  `datetime` tenía un tipo ORM inconsistente con la migration (`TIMESTAMP`
  sin timezone en el lado Python vs. `TIMESTAMPTZ` en la base de datos) — nunca
  daba error en la Fase 0 porque nada hacía round-trip de datetime con
  aritmética. Corregido de una vez para todos los modelos vía
  `type_annotation_map` en `app/db/base.py`. `app/utils/time.utc_now()` pasó a
  devolver un datetime timezone-aware.

## Fase 2 — Catálogo de productos (implementada)

Pipeline de resolución de identidad determinístico, del más barato al más caro
(`app/services/catalog_service.py`, `POST /catalog/resolve`):

1. **Alias exacto normalizado** (`product_aliases`, lookup O(1)) — `app/utils/text.normalize_alias`
   (minúsculas, sin acento, sin puntuación, espacios colapsados).
2. **Fuzzy trigram** (`pg_trgm`, índice GIN en `product_aliases.alias_normalized`)
   — cubre error de tipeo/variación pequeña sin necesidad de embedding.
3. **Similitud semántica** (`pgvector`, `product_catalog.embedding`,
   distancia de coseno) — solo se ejecuta si los pasos 1-2 no dieron match.
4. **Crea entidad nueva** — `entity_key` siempre generado localmente (uuid4),
   nunca bloqueado por dependencia externa.

Un match en los pasos 2 o 3 graba un alias exacto nuevo apuntando a la entidad
encontrada, de modo que la misma consulta caiga en el paso 1 (barato) la
próxima vez.

- **El embedding es un stopgap vía OpenAI (decisión explícita del usuario, 2026-07-02):**
  Anthropic no tiene endpoint de embeddings propio (la documentación oficial
  recomienda Voyage AI) — usando `text-embedding-3-small` de OpenAI por ahora,
  con migración planificada a modelo local después. Aislado por completo en
  `app/services/embedding_client.py` para que ese cambio sea un solo archivo.
  **Nunca bloquea** la resolución: si la llamada falla (o `OPENAI_API_KEY` no
  está configurada), cae directo a la creación de entidad con `embedding=NULL`.
  `POST /admin/catalog/backfill-embeddings` completa después las entidades que
  quedaron sin embedding.
- **`google_mid` es enriquecimiento best-effort, asíncrono, nunca bloqueante**
  (`POST /admin/catalog/enrich-mid`) — reutiliza el `CollectionClient` de la
  Fase 1 (`suggestions` del PyTrends) y lee el resultado de vuelta directo de
  la base de PyTrends (mismo patrón de `reconciliation_service`). La elección
  del candidato es una regla determinística (match exacto de título, si no el
  primer resultado — Google ya devuelve ranqueado), nunca una decisión de LLM.
- **Merge de duplicados** (`POST /admin/catalog/merge`) — entidades que
  descubren compartir el mismo `google_mid` (solo después del enriquecimiento)
  se fusionan: la más antigua (`created_at`) se convierte en canónica,
  `merged_into` se setea en la otra, y los aliases de esta se repuntan
  directamente a la canónica (el lookup exacto del paso 1 nunca necesita seguir
  la cadena de merge). `GET /catalog/{entity_key}` expone `merged_into`
  explícitamente en lugar de esconderlo — quien llama decide si lo sigue.
- **Infra:** se cambió la imagen de Postgres de `postgres:16-alpine` a
  `pgvector/pgvector:pg16` (misma major version, solo agrega la extensión) —
  necesario para ejecutar `CREATE EXTENSION vector`.

## Fase 3 — Motor de indicadores, Capítulo 4 (implementada)

Una función de valor bruto por indicador (`app/services/indicators/{pytrends,worldbank,oecd,undata}.py`),
fórmulas exactas transcritas en `docs/marketmind-capitulo4-metricas.md`
(reanexado por el usuario en 2026-07-02 — la versión original nunca había sido
guardada como archivo, solo compartida una vez en el chat). `app/services/indicator_engine.py`
orquesta: calcula los 9 indicadores país por país (siempre, `entity_key=NULL`,
compartidos entre productos del mismo país/período) + los 5 indicadores
específicos de producto de PyTrends + el condicional Median Age Fit (solo si
se informa `entity_key`), normaliza cada uno y lo graba en `indicator_values`.

- `POST /indicators/compute` — calcula todo para un país (+ producto, si se
  informa `entity_key`) y devuelve las líneas de `indicator_values`.
- `POST /admin/indicators/refresh-reference` — recalcula `normalization_reference`
  (bounds p5/p95) para los 9 indicadores empíricos, contra la población de
  referencia (tabla `countries` sembrada, ~27 países — "crece
  orgánicamente" vía consultas de usuario aún no se rastrea, simplificación
  documentada). Nunca sobrescribe un período ya cerrado.
- **La normalización sigue la corrección de la sección 7 del plan, no el texto
  literal del Cap. 4:** los bounds vienen de `normalization_reference`
  (población de referencia fija, versionada por período), nunca de los países
  de la consulta actual — eso es lo que hace que el MOS sea absoluto y
  comparable entre ejecuciones. Growth Score y Demand Stability (CV) usan
  límites teóricos fijos (`GrowthRaw ∈ [-50,150]`, `CV ∈ [0,1]`, sembrados una
  vez en la migration 005 con `period_key="FIXED"`), no población empírica —
  no tienen un universo natural de países con el cual comparar.
- **Fallback de inflación:** se intenta primero con OECD; solo cae a World
  Bank (`availability_status=fallback`) si la serie de la OECD está
  indisponible para el país/período — nunca ambos al mismo tiempo (Cap. 4
  §4.7).
- **Fallback de granularidad SKU→categoría:** si el Trend Score del término
  específico del producto queda `unavailable` (pocas observaciones), se
  intenta de nuevo con `product_catalog.category` como keyword; si eso
  funciona, los 5 indicadores de PyTrends de ese producto quedan marcados
  `availability_status=fallback` con nota explícita.
- **Limitación documentada, no escondida:** la tabla cruda de la OECD
  (`marketmind-oecd`) no persiste las dimensiones SDMX `EXPENDITURE`/
  `TRANSFORMATION`/`ADJUSTMENT` como columnas — solo `MEASURE`/`FREQ`/`UNIT`
  (confirmado leyendo el código de ingesta de ese servicio, no asumido). El
  filtrado de inflación de la OECD usa solo `país + measure='CPI'`, confiando
  en que la llamada de recolección en sí ya fue delimitada correctamente — ver
  comentario en `app/services/indicators/oecd.py`.
- **Suposición documentada:** el Cap. 4 no da una regla léxica para distinguir
  "0.6 marca/variante" de "0.0 irrelevante" en el Search Intent Score — se
  trata cualquier query de `related_queries.top` que no coincida con ninguna
  regla específica como variante de producto (0.6), ya que vino de la propia
  búsqueda del keyword. Ver `app/services/indicators/pytrends.py`.
- **Fuera del alcance de esta fase, a propósito:** Regional Spread Insight (HHI)
  y Rising Query Momentum (PyTrends) — el propio Cap. 4 marca "no entra al
  MOS v1", son insight de narrativa para la explicación del LLM en la Fase 5.

## Fase 4 — Motor de MOS, Capítulo 5 (implementada)

Fórmulas exactas transcritas en `docs/marketmind-capitulo5-mos.md` (reanexado
por el usuario en 2026-07-02, misma situación que el Cap. 4). Agregación en 2
etapas: los 17 indicadores atómicos (Cap. 4) se agrupan en 5 dimensiones
(`app/services/mos/dimension_mapping.py`), los pesos de las 5 dimensiones
vienen de Rank Order Centroid — **no Entropy Weight** (sustituido en
2026-07-03, ver abajo) — y TOPSIS (`app/services/mos/topsis.py`) calcula el
score final — siempre por (producto, país, período), nunca agregando países
entre sí.

- **Pesos de dimensión vía Rank Order Centroid (ROC), no Entropy Weight —
  corrección de 2026-07-03** (`app/services/mos/roc_weights.py`): una prueba
  en vivo mostró que el Entropy Weight subvaloraba Demand repetidamente
  (2,6%-5% del peso total, incluso con muestra real de 12 países) solo porque
  el interés de búsqueda del producto probado variaba poco entre los países
  de la muestra — "no discrimina entre mercados" no es lo mismo que "no
  importa", más aún en una herramienta cuyo trabajo es justamente evaluar el
  potencial de un producto específico. ROC convierte un **orden de prioridad
  explícito** (decisión del usuario, fundamentada en el propósito del
  producto — "indicar el potencial de venta") en pesos matemáticos (Barron &
  Barrett, 1996), sin depender de ningún dato de indicador: Demand (45,7%) >
  Economic Readiness (25,7%) > Market Size (15,7%) > Digital Readiness
  (9,0%) > Demographic Sustainability (4,0%).
- `POST /admin/mos/refresh-weights` — escribe los 5 pesos ROC para el período
  informado (`app/services/weight_calibration_service.py`). Como los pesos ya
  no dependen de ningún dato, el cálculo es instantáneo — pero sigue
  versionado por `period_key` y nunca sobrescribe un período ya cerrado, por
  el mismo motivo que `normalization_reference` funciona así: auditoría
  ("qué peso fue usado en el período X") y posibilidad de revisar el orden de
  prioridad en el futuro sin alterar resultados históricos ya cerrados.
- `POST /mos/compute` — calcula el MOS para un producto en cada país de la
  lista (`app/services/mos_engine.py`), un resultado independiente por país:
  valida indicadores obligatorios (Cap. 5 §5.3 — todo indicador excepto
  Median Age Fit condicional y el trío de inflación, que acepta OECD nivel +
  volatilidad O el fallback World Bank solo), arma el vector de 5 sub-scores
  de dimensión, busca los pesos calibrados (compartidos, sin `entity_key`),
  ejecuta TOPSIS y aplica el techo anticompensación. Excluido (nunca
  descartado silenciosamente) cuando falta un indicador obligatorio, una
  dimensión entera o un peso calibrado para el `period_key` — el motivo
  siempre queda en `excluded_reason`.
- **MOS absoluto e independiente por país Y por producto — corrección sobre
  el texto literal del Cap. 5** (ver sección 12 del plan): el ideal/anti-ideal
  del TOPSIS son fijos en 100/0 (no "mejor/peor observado entre los países
  comparados"), ya que el Cap. 4 ya normaliza cada indicador contra la
  población de referencia fija. Esto también evita el paso clásico de
  normalización vectorial del TOPSIS — solo la distancia ponderada permanece.
  **El peso de Demand dejó de ser por producto (decisión revertida en
  2026-07-03):** tener un peso diferente por producto rompía la
  comparabilidad entre productos distintos (dos productos en el mismo país
  tendrían el MOS calculado con fórmulas distintas) y exigía recolección
  síncrona de PyTrends con cada producto nuevo — frágil en la práctica
  (rate-limit real de Google Trends observado en prueba en vivo).
- **Techo anticompensación — decisión del usuario, no está en el Cap. 5:**
  los indicadores macro (población, PIB, internet) nunca pueden compensar un
  producto sin demanda de búsqueda real. Basado en el Trend Score (reutiliza
  los rangos de interpretación del Cap. 4 §4.2.1): `<20` → techo 39; `20–40` →
  techo 59; `≥40` → sin techo. `mos_before_cap`/`capped_by` quedan expuestos
  para transparencia en la explicación futura del LLM (Fase 5).
- **Comparación con biblioteca de mercado (validación, no producción):** cada
  cálculo de MOS también ejecuta, en paralelo, el TOPSIS + Entropy Weight
  *clásicos* vía `pymcdm` (`app/services/mos/library_reference.py`),
  guardados en `library_topsis_score`/`library_mos` en `mos_results` — para
  comparación manual contra nuestra implementación corregida. Nunca decide el
  MOS real del producto, es solo un artefacto de auditoría/defensa del TFM.

## Running Tests

```bash
pytest
```

## Running Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

## Linting and Type Checking

```bash
ruff check .
black .
mypy .
```
