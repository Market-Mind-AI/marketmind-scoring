> **SUPERADO em 21/09/2026 — ver [marketmind-arquitetura.md](marketmind-arquitetura.md).**
>
> Este documento planejava a orquestração dentro do `marketmind-core`. Essa
> orquestração foi para o agente (`marketmind-orchestrator`) e o serviço passou
> a se chamar `marketmind-scoring`. Fica aqui porque comentários no código ainda
> citam suas seções, e porque registra o raciocínio de onde o método veio — não
> como descrição do que o sistema é hoje.

# MarketMind — Plan de Implementación Final

> Consolida las decisiones de arquitectura y metodología discutidas para el TFM MarketMind, cubriendo el nuevo servicio `marketmind-core` y su integración con los 4 microservicios de recolección ya existentes (PyTrends, WorldBank, OECD, UnitedNationsData).

## 0. Principios que atraviesan todo el diseño

- **Determinismo siempre.** Ningún número que alimenta el MOS es generado o alterado por LLM. El LLM solo propone texto (identificación de producto, candidatos de keyword, traducción de diccionarios) o explica resultados ya calculados — nunca decide qué valor se usa.
- **Sin imputación.** Dato ausente se convierte en `UNAVAILABLE`, nunca una suposición.
- **Reproducibilidad.** Misma entrada, mismo período cerrado → siempre el mismo resultado, para siempre.
- **El MOS es absoluto e independiente por país.** No existe un "MOS general" que combine países, ni dependencia entre países comparados en la misma consulta (decisión que sustituye el texto literal del Cap. 5 §5.8 — ver sección 11).
- **Herramienta de cribado, no de predicción.** MarketMind prioriza mercados para investigación humana; no es un forecast de ventas ni una decisión automática de go/no-go. Ese enfoque debe aparecer en el producto y en la disertación.

---

## 1. Estado actual (confirmado por exploración de código)

Los 4 microservicios de recolección ya existen y funcionan: **PyTrends, WorldBank, OECD, UnitedNationsData** — todos FastAPI + SQLAlchemy async + Postgres propio (`marketmind_pytrends`, `marketmind_worldbank`, `marketmind_oecd`, `marketmind_united_nations_data`), en la misma red docker `marketmind-network`. Cada uno ya tiene:
- Tablas parseadas granulares (`interest_over_time`, `worldbank_observations`, `oecd_observations`, `undata_observations`) + `raw_api_responses` (JSONB bruto) + `ingestion_runs` (auditoría con `started_at`/`finished_at`/`status`).
- Endpoints `POST` de recolección bajo demanda (ej. `POST /interest-over-time`, `POST /indicators/collect`, `POST /observations`) y `GET /runs/{id}` de estado.
- **Ninguno** tiene programación (cron/Celery) ni índice de cobertura — queda a cargo del Core.

El **`marketmind-core` todavía no existe** — es el objeto de este plan.

---

## 2. Arquitectura de componentes

```
Frontend (chat: prompt + imagen + CSV)
        │
        ▼
   marketmind-core (nuevo)
        │
        ├── lee directo (read-only) ──► Postgres PyTrends
        ├── lee directo (read-only) ──► Postgres WorldBank
        ├── lee directo (read-only) ──► Postgres OECD
        ├── lee directo (read-only) ──► Postgres UNData
        │
        ├── dispara POST /coleta ──► los 4 servicios (cuando falta dato)
        │
        └── Postgres propio del Core (coverage, catálogo, indicadores, MOS, jobs)
```

**Decisión de simplicidad (MVP/TFM):** el Core accede a los 4 Postgres directamente vía usuario read-only, en lugar de crear endpoints HTTP de bulk-read en cada servicio. Los 4 servicios siguen siendo "tontos": solo recolectan lo que el Core pida, sin noción de cobertura ni calendario.

**Corrección de infraestructura descubierta en la implementación de la Fase 0:** los `docker-compose.yml` de los 4 servicios declaran la red `marketmind-network` sin `external: true` — Docker Compose aísla una red por proyecto, así que no son de hecho compartidas a pesar del nombre igual. En lugar de editar los 4 repositorios existentes, el Core se conecta por los puertos ya expuestos en el host de cada servicio (con `host.docker.internal` desde el contenedor del Core). Esto exige dos pasos manuales: (1) ejecutar `scripts/create_readonly_roles.sql` contra cada una de las 4 bases, (2) garantizar puertos de host distintos entre los 4 servicios cuando corren simultáneamente (WorldBank y UNData colisionan en 5432 por defecto — ajustar `DATABASE_EXTERNAL_PORT` en el `.env` de cada uno). Detalles completos en `marketmind-core/README.md`, sección "Conectividade entre serviços".

**pgAdmin consolidado en el Core (2026-07-02):** por pedido del usuario, el pgAdmin fue removido de los 4 `docker-compose.yml` existentes (servicio, volumen `pgadmin_data`, carpetas `pgadmin/` y variables `PGADMIN_*` en los `.env`/`.env.example`) y existe ahora solo en `marketmind-core`, con los 5 servidores pre-registrados en `pgadmin/servers.json`/`pgadmin/pgpass` (los 4 externos alcanzados por la misma estrategia `host.docker.internal` + puertos de host distintos ya usada por la API del Core).

---

## 3. Modelo de datos del Core

```sql
-- Catálogo de produtos (identidade determinística, nunca bloqueada por dependência externa)
product_catalog(entity_key UUID PK, canonical_title, google_mid NULL,
                 google_type NULL, category, target_age_min, target_age_max,
                 embedding VECTOR, merged_into UUID NULL, created_at)
product_aliases(alias_normalizado, entity_key FK, source, hit_count, first_seen_at)

-- Cobertura / cadência mensal
monthly_snapshot(source, entity_key, country, period_key, is_closed,
                  collected_at, raw_ref)
coverage_index(source, entity_key, country, indicator_code, period_key,
                is_closed, last_fetched_at, ttl_days)

-- Indicadores (Cap. 4) — camada cacheável
indicator_values(entity_key, country, indicator_code, period_type
                  ['MONTH','QUARTER','SEMESTER','YEAR','CUSTOM'], period_key,
                  raw_value, normalized_score, direction, calculation_window,
                  availability_status, notes, computed_at)

-- Referência fixa e pesos (Cap. 5) — versionados por período, imutáveis após fechados
normalization_reference(indicator_code, period_key, min_x, max_x, is_closed)
indicator_weights(indicator_code, period_key, weight, dimension, is_closed)

-- MOS — sempre por país, nunca agregado
mos_results(entity_key, country, period_type, period_key, mos,
            mos_before_cap, capped_by, topsis_score, availability_status,
            excluded_reason NULL, computed_at)

-- Orquestração
jobs(job_id, status, current_step, requested_by, created_at, finished_at)
job_tasks(job_id FK, source, entity_key, country, period_key, status)
```

---

## 4. Cadencia de recolección: mensual, con mes abierto/cerrado

| Fuente | Qué significa "mensual" | Observación |
|---|---|---|
| World Bank / UN Data | recheck barato del último valor anual (`y_latest`) | el valor usado es siempre el más reciente, independiente del período pedido en el MOS |
| OECD | serie mensual nativa acumulando | las fórmulas del Cap. 4 ya usan ventana de últimos k meses |
| PyTrends `interest_over_time` | **no** es recolección aislada por mes — es un corte de una serie continua amplia (`today 5-y`), actualizada mensualmente + cola diaria en el mes corriente | evita el problema de escala relativa de Google Trends (pico=100 por ventana consultada) |
| PyTrends `interest_by_region` / `related_queries` | snapshot por período; para período >1 mes, rehace la llamada exacta (no aproxima por promedio de meses) | decisión explícita: precisión > economía de llamada |

- Mes **cerrado** = pasó al mes civil siguiente + margen de algunos días (fuentes atrasadas) → inmutable para siempre.
- Mes **corriente** = abierto, actualizado diaria o bajo demanda.
- Período mayor a 1 mes (trimestre/semestre/año/custom) siempre **recalcula la fórmula del Cap. 4 sobre los registros brutos agregados de los meses cubiertos** — nunca hace promedio de scores mensuales ya listos.

---

## 5. Detección de cobertura y disparo de recolección

- Solo el Core sabe lo que falta (`coverage_index`) — los 4 servicios no tienen ese concepto.
- El tipo de verificación varía por indicador: último valor (¿existe + fresco?), serie continua (¿intervalo cubierto + cola fresca?), serie mensual nativa (¿qué períodos faltan?), snapshot por período (¿existe línea exacta?).
- **Conclusión de recolección (MVP):** polling en `GET /runs/{id}` (endpoint ya existe, cero cambio en los 4 servicios). Evolución v2: webhook de callback.
- **Dedup de carrera:** `SELECT ... FOR UPDATE SKIP LOCKED` en Postgres — dos usuarios pidiendo el mismo gap no disparan recolección duplicada.
- **Reconciliación periódica** (job de baja prioridad) compara `coverage_index` contra las tablas brutas reales.

---

## 6. Catálogo de productos

Pipeline de resolución, del más barato al más caro:

1. Alias exacto normalizado (`product_aliases`).
2. Fuzzy trigram (`pg_trgm`).
3. Embedding/similitud (`pgvector`).
4. Ninguno coincidió → crea entidad nueva con UUID **generado localmente, en el momento**.
5. Enriquecimiento con `google_mid` (vía `suggestions` de PyTrends) es **asíncrono, best-effort, nunca bloqueante** — si Google cae, el catálogo sigue funcionando (PyTrends acepta keyword de texto puro, no exige `mid`).
6. LLM usado para: extraer nombre candidato de imagen/prompt, y proponer **jerarquía de términos** (SKU específico + categoría madre) — nunca decide solo la identidad final.
7. Job de reconciliación en background enriquece `mid` retroactivamente y hace merge de entidades duplicadas cuando descubre coincidencia.
8. Fallback de granularidad: si el término específico tiene volumen/cobertura insuficiente en PyTrends, sube automáticamente al término de categoría (regla determinística, umbral documentado) antes de aplicar cualquier tope del MOS.

---

## 7. Capa de indicadores (Capítulo 4) — normalización absoluta

Corrección adoptada en relación al texto literal del Cap. 4 (que normaliza Min-Max **dentro de la consulta actual**, §4.1):

- **Indicadores país a país product-agnostic** (GDP, Internet, Mobile, GDP Growth, Población, Urbanización, Inflación, Population Growth, Net Migration): `min(X)/max(X)` calculado **una vez contra toda la población de referencia del catálogo**, no contra los países de la consulta. Bounds por **percentil (p5/p95)** en lugar de min/max literal, para no dejar que un outlier (ej. hiperinflación) estire la regla para todos.
- **Indicadores product-specific sin universo natural de países** (Growth Score, Demand Stability/CV): límites teóricos fijos y documentados (clipping), no min-max empírico.
- **Indicadores ya absolutos** (Trend Score, Regional Demand, Internet %, Urbanización %, Search Intent): sin cambio.
- **GDP per capita:** considerar usar la versión PPP (`NY.GDP.PCAP.PP.CD`) además o en lugar de la nominal en US$ corriente, que es ruidosa por variación cambiaria.
- **Search Intent Score:** diccionario de palabras clave por idioma, **curado offline con ayuda de LLM** (revisado y versionado como config estático), la clasificación en producción sigue siendo lookup determinístico.
- **El cero de PyTrends es ambiguo** (sin interés real vs. muestra insuficiente) — cruzar con presencia/ausencia en `related_queries`/`related_topics` antes de tratarlo como "demanda baja" real.
- **Mapeo de país entre las 4 fuentes:** tabla canónica ISO3 en el Core, construida una vez contra dato de referencia estandarizado (ISO 3166 / UN M49) — **sin LLM**, es dato cerrado sin ambigüedad.

---

## 8. Capa de MOS (Capítulo 5) — por país, absoluto, no compensatorio

Corrección adoptada en relación al texto literal del Cap. 5 §5.8 ("No existe un MOS absoluto"):

1. **Agregación jerárquica en 2 etapas:** indicadores agrupados en las dimensiones del `resumo.pdf` (Demand, Digital Readiness, Economic Readiness, Market Size, Demographic Sustainability) → sub-score por dimensión → los pesos de las 5 dimensiones se combinan vía TOPSIS, no los ~15 indicadores crudos. Evita que Demand se diluya por tener menos indicadores que el bloque macro. Verificar correlación entre indicadores dentro de cada dimensión (evitar doble conteo de la misma señal, ej. GDP+Internet+Mobile+Urbanización todos midiendo "desarrollo").
2. **Pesos vía Rank Order Centroid (ROC), no Entropy Weight — corrección de 2026-07-03.** La primera implementación usó Entropy Weight (pesos derivados de la variación estadística de cada dimensión entre países). Una prueba en vivo mostró que esto subvaloraba Demand repetidamente (2,6%-5% del peso total, incluso con muestra real de 12 países) solo porque el interés de búsqueda del producto probado variaba poco entre los países de la muestra — "no discrimina entre mercados en esta muestra" no es lo mismo que "no importa", sobre todo para una herramienta cuyo propósito es justamente evaluar el potencial de venta de un producto específico. La discusión con el usuario llevó a un orden de prioridad explícito, fundamentado en el propósito de la app ("decir el potencial de venta de un producto"): **Demand > Economic Readiness > Market Size > Digital Readiness > Demographic Sustainability**. ROC (Barron & Barrett, 1996) convierte ese orden en pesos matemáticos sin depender de ningún dato de indicador: 45,7% / 25,7% / 15,7% / 9,0% / 4,0%, respectivamente. `app/services/mos/roc_weights.py`; `app/services/mos/entropy_weight.py` (la implementación propia de Entropy Weight) fue removida por quedar huérfana — la comparación con la lib clásica (ítem 12 abajo) ya usa `pymcdm.weights.entropy_weights` directamente, así que mantener una segunda implementación custom ya no tenía sentido.
3. **Pesos y bounds fijos, recalculados periódicamente** (misma cadencia mensual), no por consulta — `indicator_weights`/`normalization_reference` versionados por `period_key`, **inmutables después de que el mes cierra**. **Corrección de 2026-07-03:** esto vale para los 5 pesos, sin excepción — incluso Demand, que en la primera implementación había sido calibrado por producto (exigiendo recolección PyTrends síncrona a cada producto nuevo, quebrando la comparabilidad entre productos diferentes y mostrándose frágil contra el rate-limit real de Google Trends en prueba en vivo). Como los pesos ahora vienen de ROC (ítem 2) y no de dato observado, el "crece orgánicamente"/"cesta de calibración" descrito originalmente aquí solo se aplica ahora a `normalization_reference` (Cap. 4), no a los pesos de dimensión.
4. **TOPSIS con ideal/anti-ideal fijos:** vector ideal = 100 en todo, anti-ideal = 0 en todo (ya no el mejor/peor observado entre los países de la consulta) → TOPSIS se convierte en función pura del vector de indicadores del propio país.
5. **Tope no compensatorio basado en demanda real:**
   ```
   MOS_final = min(MOS_topsis, teto(TrendScore))
   TrendScore < 20   → teto = 39
   20 ≤ TS < 40       → teto = 59
   TS ≥ 40            → sem teto
   ```
   Impide que indicadores macro (población, GDP, internet) compensen la ausencia de demanda real de búsqueda. Cuando el tope reduce el MOS, la respuesta expone `capped_by`/`mos_before_cap`, y la explicación del LLM narra esto explícitamente.
6. **Regla de exclusión por país:** indicador obligatorio ausente (incluso después del fallback OECD→WB) → el país queda `UNAVAILABLE`, no recibe MOS, sin imputación. Clasificación por indicador: obligatorio / con fallback / condicional (Median Age Fit).
7. **Sin mínimo de países.** Como el cálculo ya no depende de quién más está en la consulta, un usuario puede pedir el MOS de un único país.
8. **Salida siempre en lista por país, nunca agregada:**
   ```json
   {
     "product": "air fryer",
     "results": [
       { "country": "ES", "indicators": {...}, "mos": 71.4, "capped_by": null, "availability_status": "AVAILABLE" },
       { "country": "BR", "indicators": {...}, "mos": null, "availability_status": "UNAVAILABLE", "notes": "..." }
     ]
   }
   ```

**Pendiente para la disertación:** la Sección 5.8 del Cap. 5 necesita ser reescrita para reflejar el MOS absoluto (con referencia/pesos versionados periódicamente) en lugar del texto actual ("no existe un MOS absoluto"). Una sección nueva necesita documentar el tope no compensatorio como decisión metodológica explícita.

---

## 9. Comunicación con el frontend

```
POST /analyze (prompt + imagen/csv)
  → Core resuelve entidad(es) + país(es) + período(s)
  → calcula diff de cobertura
  → ¿todo cubierto y fresco?
        SÍ → calcula indicadores + MOS al instante → 200 síncrono
        NO → crea job, dispara recolecciones faltantes → 202 + {job_id}
```

- El camino asíncrono usa **SSE** (combina con la UX de chat) para mostrar progreso (`"recolectando World Bank..."`, `"calculando MOS..."`) hasta el mensaje final.
- CSV con varios productos: devuelve en el momento los ya cubiertos, completa el resto vía SSE.
- El MOS en sí es **siempre síncrono y rápido** (cálculo puro sobre indicadores ya en caché) — el job asíncrono es solo sobre recolección de datos, nunca sobre el cálculo del MOS.

---

## 10. Capacidad para servidor modesto (MVP / presentación del TFM)

- El cuello de botella real es el rate limit de APIs externas (PyTrends vía lib no oficial, propensa a 429) — no CPU/RAM del servidor.
- Cola única por fuente con concurrencia limitada (`asyncio.Semaphore`; PyTrends=1 con backoff, WB/OECD/UN un poco más) — sin Celery/Redis en esta etapa.
- Circuit breaker para PyTrends (back off en lugar de insistir y arriesgar bloqueo de IP).
- **Día de la defensa:** precalentar la caché con los productos/países de la demo antes de la presentación, para que la demo en vivo caiga en el camino síncrono rápido.

---

## 11. Pruebas, validación y confiabilidad

- **Determinismo:** pruebas unitarias de Entropy Weight/TOPSIS con vectores pequeños calculados a mano + "golden run" de regresión (vector de indicadores fijo → MOS esperado fijado).
- **Confianza/completitud:** exponer `data_completeness` (ej. "9 de 10 indicadores disponibles") junto a cada MOS — evita falsa precisión en países con cobertura débil.
- **Transparencia de pesos:** exponer el desglose de cuánto contribuyó cada dimensión al MOS final, no solo el número.
- **Explicación del LLM:** debe narrar siempre dirección (Growth) junto al nivel (Trend); nunca traducir "interés de búsqueda" en "demanda comprobada" o intención de compra segura; versionar modelo/prompt/timestamp usados en cada explicación.
- **Backtesting (mayor retorno para la defensa del TFM):** validar el MOS contra 5-10 casos reales conocidos (producto que tuvo éxito/fracaso en determinado mercado) antes de la presentación — el rigor metodológico solo no prueba que el score correlaciona con la realidad.

---

## 12. Fases de implementación

**Fase 0 — Esqueleto del Core (implementada 2026-07-02)**
Nuevo servicio `marketmind-core` (FastAPI + Postgres). Tablas de la sección 3. Usuarios read-only en los 4 Postgres existentes + Core en la red `marketmind-network`. Tabla canónica de países (ISO3 + aliases de las 4 fuentes).

**Fase 1 — Cobertura y recolección bajo demanda (implementada 2026-07-02)**
`coverage_index`, `check_or_collect` (`POST /jobs`, lectura pura en `POST /coverage/check`), cliente HTTP por fuente con polling en `GET /runs/{id}` (`app/services/collection_client.py`), dedup de carrera vía índice único parcial en `job_tasks` + `SELECT ... FOR UPDATE SKIP LOCKED` (`app/repositories/job_repository.py`), job de reconciliación bajo demanda (`POST /admin/reconciliation/run`, alcance WorldBank/OECD/UNData — PyTrends queda pendiente del catálogo de la Fase 2). `job_task` ganó `indicator_code`/`ingestion_run_id` (migration 002, no previstos en el diseño original de la Fase 0). `countries` sembrada con cesta de referencia de 27 países (migration 003). Bug de tipo de columna `datetime` (ORM sin timezone vs. migration con timezone) corregido globalmente vía `type_annotation_map` en `app/db/base.py`, descubierto por las pruebas de integración de esta fase contra Postgres real.

**Fase 2 — Catálogo de productos (implementada 2026-07-02)**
Pipeline alias → trigram → embedding → creación local → enriquecimiento best-effort de `mid` → job de merge (`POST /catalog/resolve`, `GET /catalog/{entity_key}`, `POST /admin/catalog/{enrich-mid,merge,backfill-embeddings}`). Corrección en relación al diseño original: Anthropic no tiene embeddings propios (la recomendación oficial es Voyage AI) — usando OpenAI (`text-embedding-3-small`) como solución provisional por decisión explícita del usuario, con migración planeada a modelo local después, aislado en `app/services/embedding_client.py`. `CollectionRequest.country` (Fase 1) pasó a ser opcional para acomodar el endpoint `suggestions` de PyTrends, que no usa país. Imagen del Postgres cambiada a `pgvector/pgvector:pg16` (migration 004: extensiones `pg_trgm`/`vector`, columna `product_catalog.embedding`, índice GIN trigram).

**Fase 3 — Motor de indicadores (Cap. 4) (implementada 2026-07-02)**
Una función de valor bruto por indicador (17 códigos, `app/services/indicators/`), fórmulas exactas del Capítulo 4 (readjuntado por el usuario en esta fase y guardado en `docs/marketmind-capitulo4-metricas.md` — nunca había sido persistido como archivo antes, solo compartido una vez en el chat). `normalization_reference` fija y versionada por `period_key`, calculada por `reference_calculator_service.py` (percentil p5/p95 contra la población de referencia sembrada, ~27 países) — Growth Score y Demand Stability usan bounds teóricos fijos en lugar de población empírica (migration 005). Fallback de inflación OECD→World Bank y de granularidad SKU→categoría implementados. Diccionario de Search Intent implementado literalmente como vino en el Capítulo 4 (términos en ES/EN) — la expansión multilingüe curada por LLM queda para después, no bloqueante. `POST /indicators/compute`, `POST /admin/indicators/refresh-reference`. 126 pruebas, incluyendo verificación exacta de las fórmulas contra los valores del capítulo.

**Fase 4 — Motor de MOS (Cap. 5) (implementada 2026-07-03, rediseñada el mismo día tras prueba en vivo)**
Fórmulas exactas del Capítulo 5 (readjuntado por el usuario en esta fase y guardado en `docs/marketmind-capitulo5-mos.md` — misma situación del Cap. 4). Agregación jerárquica en 2 etapas: `app/services/mos/dimension_mapping.py` agrupa los 17 indicadores en 5 dimensiones y calcula sub-score por dimensión + regla de obligatoriedad (Cap. 5 §5.3, con la excepción del trío de inflación: OECD nivel+volatilidad O el fallback World Bank solo, y Median Age Fit condicional). `app/services/mos/topsis.py` corre TOPSIS con ideal=100/anti-ideal=0 fijos (saltando la normalización vectorial clásica, redundante con la normalización del Cap. 4) y aplica el tope no compensatorio basado en el Trend Score (decisión del usuario, no está en el capítulo: `<20`→39, `20-40`→59, `≥40` sin tope).

**Rediseño del peso de Demand (2026-07-03, mismo día, tras subir la solución completa y probarla en vivo):** la primera versión calibraba el peso de Demand por producto (`indicator_weights.entity_key`, migration 006), disparando `check_or_collect` + `IndicatorEngine.compute_all` contra los ~27 países de referencia a cada producto nuevo. En la prueba en vivo esto bloqueó una solicitud por ~2h de peor caso (3 llamadas PyTrends × hasta 120s de timeout × 27 países) y de hecho chocó con rate-limit real de Google Trends (429/CAPTCHA). La discusión con el usuario reveló un problema más profundo, no solo de rendimiento: el peso por producto rompe la comparabilidad *entre productos* (dos productos en el mismo país tendrían MOS sumado con fórmulas diferentes), yendo en contra del mismo principio de "MOS absoluto" que motivó sacar los pesos del nivel "por consulta" al nivel "por período". La migration 007 revierte `entity_key` de `indicator_weights` (vuelve a ser solo `indicator_code`+`period_key`, único). `app/services/mos/country_vectors.py` calcula el valor de Demand por país como el **promedio entre todos los productos que ya tienen indicadores calculados en ese país** (orgánicamente, sin disparar recolección nueva). `app/services/mos_engine.py` orquesta por país: valida obligatoriedad, arma el vector de 5 dimensiones, busca pesos (sin `entity_key`), corre TOPSIS, aplica el tope, hace upsert en `mos_results` — nunca agrega países entre sí, exclusión siempre con `excluded_reason` explícito (nunca silenciosa).

**De Entropy Weight a Rank Order Centroid (2026-07-03, mismo día, después del rediseño anterior):** con Demand ahora país a país, calibré los 5 pesos con Entropy Weight contra una muestra real de 12 países (dato de PyTrends de "air fryer" ya recolectado a lo largo de la sesión) — Demand salió con 2,6% del peso, Demographic Sustainability con 61%. La conversación con el usuario reveló la causa: Entropy Weight mide solo variación estadística, sin noción de importancia causal — que Demand varíe poco entre los países probados (todos con interés "razonable" por air fryer) no significa que la demanda sea irrelevante, solo que no *diferenciaba* esa muestra específica. Decisión: abandonar Entropy Weight como método de producción, sustituirlo por **Rank Order Centroid (ROC — Barron & Barrett, 1996)**, que convierte un orden de prioridad explícito (no estadístico) en pesos matemáticos. Orden definido con el usuario, fundamentado en el propósito de la app ("decir el potencial de venta de un producto específico"): **Demand > Economic Readiness > Market Size > Digital Readiness > Demographic Sustainability** — pesos resultantes 45,7% / 25,7% / 15,7% / 9,0% / 4,0%. `app/services/mos/roc_weights.py` implementa la fórmula; `app/services/weight_calibration_service.py` quedó trivial (escribe los 5 pesos fijos para el período, sin leer ningún dato); `app/services/mos/entropy_weight.py` (la implementación propia de Entropy Weight, usada solo en la versión anterior) fue removida por quedar huérfana — probada con valores calculados a mano hasta el día en que fue descartada, ver historial del archivo.

**Validación cruzada con biblioteca de mercado (pedido explícito del usuario, para la defensa del TFM):** `app/services/mos/library_reference.py` corre el TOPSIS + Entropy Weight *clásicos* (biblioteca `pymcdm`, ideal/anti-ideal relativos a la población comparada, normalización vectorial estándar) en paralelo a nuestro cálculo corregido, guardando `library_topsis_score`/`library_mos` en `mos_results` (migration 007) solo para comparación manual — nunca decide el MOS real. Sigue usando Entropy Weight (vía `pymcdm.weights.entropy_weights`) incluso después de que producción migró a ROC — el objetivo de esta comparación es contrastar contra el método *clásico* de mercado, así que debe seguir representando fielmente lo que haría una lib estándar, independiente de qué método use producción.

`POST /mos/compute`, `POST /admin/mos/refresh-weights` (ya no recibe `entity_key`, escribe los 5 pesos ROC de una vez, cálculo instantáneo por no depender de dato). 167 pruebas, incluyendo casos de exclusión (indicador obligatorio ausente, peso no calibrado), tope activo/inactivo, independencia de MOS entre países del mismo producto, y los pesos ROC coincidiendo con los valores canónicos publicados para n=5.

**También durante la prueba en vivo de esta fase:** descubiertas y corregidas dos fallas de infraestructura no relacionadas con el código del Core — los puertos del Postgres de WorldBank y de UNData estaban intercambiados entre sí en los respectivos `.env` (documentado en el README del Core, nunca detectado antes por falta de una prueba de integración de punta a punta con los 4 microservicios reales); y el `pgadmin/servers.json` no tenía `SavePassword: true`, así que el pgAdmin siempre pedía contraseña incluso con el `pgpass` correcto — corregido, y aprovechado el momento para cambiar todas las contraseñas de base de datos (Core + 4 microservicios, usuario admin y `marketmind_readonly`) a `admin`, a pedido del usuario.

**Fase 5 — Orquestación `/analyze` y frontend**
Identificación de producto (imagen/prompt/CSV), resolución de gap síncrona/asíncrona, SSE de progreso, explicación final vía LLM con reglas de lenguaje restrictivas.

**Fase 6 — Validación y endurecimiento pre-defensa**
Backtesting con casos reales, precalentamiento de caché para la demo, revisión de los Capítulos 4 y 5 de la disertación para reflejar las decisiones de este documento.

**Fase 7 — Evolución (post-MVP, no bloqueante)**
Webhooks en lugar de polling, banco vectorial para reutilización de insights, bounds por percentil, GDP PPP. (Ítem antiguo "CRITIC/PCA en lugar de Entropy Weight puro" removido del roadmap — producción ya migró de Entropy Weight a Rank Order Centroid en 2026-07-03, ver sección 8 ítem 2 y sección 12 Fase 4; CRITIC/PCA serían alternativas a Entropy Weight, no a ROC, así que dejaron de ser un próximo paso natural.)

---

## 13. Pendientes conocidos sin solución cerrada todavía

- Outlier en min-max de referencia → bounds por percentil (dirección definida, no implementada).
- Multicolinealidad dentro del bloque macro → verificar correlación, considerar CRITIC.
- Cero ambiguo de PyTrends → cruzar con `related_queries`/`related_topics`.
- Sesgo estructural contra mercados emergentes (menor cobertura de dato) → declarar como limitación de alcance explícita.
- Plan de backtesting aún no diseñado en detalle (solo recomendado).

Lista completa y viva mantenida en la memoria del proyecto (`marketmind_known_gaps`).
