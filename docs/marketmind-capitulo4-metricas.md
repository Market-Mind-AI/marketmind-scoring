# MarketMind — Capítulo 4: Especificación funcional de indicadores

> Transcrito del PDF `MarketMind_Capitulo4 - Metricas deterministas.pdf`, adjuntado por el usuario el 2026-07-02 (la versión anterior se había perdido por no haber sido guardada como archivo). Fuente única de verdad para las fórmulas exactas usadas en `app/services/indicators/`. Ver `docs/marketmind-implementation-plan.md` sección 7 para las correcciones arquitecturales adoptadas sobre el texto literal de este capítulo (normalización absoluta contra población de referencia en vez de por consulta — ver 4.1 abajo).

## 4.0 Principio metodológico del capítulo

Este capítulo no inventa indicadores ni asume campos que no existen. Cada fórmula se construye a partir de campos observados en los JSONs das APIs. Si un dato no aparece en esos JSONs, no se utiliza para el cálculo del MOS en esta versión.

La regla central es simple:

```
JSON recibido -> campo disponible -> variable definida -> fórmula -> normalización 0-100 -> matriz MOS
```

El LLM puede explicar el resultado, pero no calcula ni altera ninguno de estos valores.

| Regla | Decisión metodológica |
|---|---|
| Sin variables implícitas | Toda variable usada en una fórmula se define antes de la fórmula. |
| Sin pesos arbitrarios | Los indicadores atómicos entran en la matriz del MOS. Los pesos finales los calcula Entropy Weight. |
| Sin imputación por IA | Si una fuente no trae un valor, el indicador se marca como no disponible. |
| Sin duplicidad innecesaria | Si dos fuentes traen el mismo concepto, se elige una fuente oficial para el MOS y la otra queda como respaldo o contexto. |
| Escala común | Todo indicador que entra en el MOS debe terminar entre 0 y 100. |

## 4.1 Normalización común para el MOS

> **CORRECCIÓN ARQUITECTURAL ADOPTADA (ver plan sección 7 y memoria `marketmind_mos_engine`):** el texto de abajo describe Min-Max **por consulta** (`X` = valores solo de los mercados comparados en la ejecución actual). El usuario rechazó esa relatividad — `X` en la implementación real es la **población de referencia fija del catálogo**, versionada por `period_key` en `normalization_reference`, con bounds por **percentil p5/p95** (no min/max literal) para los 9 indicadores país-a-país product-agnostic. Growth Score y Demand Stability (CV) usan límites teóricos fijos (no población empírica) — ver sección 4.6 y el código para los valores exactos. Las fórmulas de **valor bruto** de abajo permanecen exactas; solo el `X` del paso de normalización cambia.

El MOS necesita comparar indicadores muy diferentes: búsquedas de Google, inflación, población, migración, internet, PIB y regiones. Por eso todos se convierten a una escala común de 0 a 100.

| Caso | Fórmula | Cuándo se usa |
|---|---|---|
| Mayor es mejor | `Score = ((x - min(X)) / (max(X) - min(X))) * 100` | Demanda, internet, PIB per cápita, crecimiento, población, migración positiva. |
| Menor es mejor | `Score = ((max(X) - x) / (max(X) - min(X))) * 100` | Inflación, volatilidad, coeficiente de variación, riesgo. |
| Valor ya viene 0-100 | `Score = x` | Google Trends, usuarios de internet (%), urbanización (%), siempre validando límites. |
| Todos los países iguales | `Score = 50` para todos, o indicador sin peso efectivo | Cuando `max(X)=min(X)`, el indicador no diferencia mercados. |
| Valor faltante | `Indicador = unavailable` | No se inventa ni se completa con LLM. |

Notación: `X` = conjunto de valores del mismo indicador para todos los mercados comparados; `x` = valor del mercado actual; `min(X)`/`max(X)` = menor/mayor valor observado.

## 4.2 Indicadores derivados de PyTrends

Endpoints usados para el MOS: `interest_over_time`, `interest_by_region`, `related_queries` (solo `top`). `suggestions` es identificación de producto (Fase 2, no entra al MOS). `related_topics` llegó null/null. `trending_rss` es general del país, no del producto. `categories` es catálogo auxiliar.

### 4.2.1 Trend Score

**Pregunta de negocio:** ¿Existe demanda observable por el producto durante el período analizado?
**Fuente/endpoint:** PyTrends / `interest_over_time`
**Campos JSON:** `date`, columna dinámica del keyword (ej. `"air fryer"`), `isPartial`
**Uso en el MOS:** Sí — indicador atómico de demanda.

Variables: `O` = observaciones con `isPartial=false` y valor numérico; `n` = número de observaciones válidas; `v_i` = valor de interés (0-100) en la observación `i`.

Preprocesamiento: eliminar `isPartial=true`; eliminar nulos/no numéricos; ordenar por `date` ascendente; validar `n >= 2`.

```
TrendRaw = (Σ v_i) / n
TrendScore = TrendRaw   (ya es 0-100)
```

Interpretación: 0-20 muy baja, 20-40 baja, 40-60 moderada, 60-80 alta, 80-100 muy alta.
**Limitación:** Google Trends es relativo — 100 es el pico dentro del período, no volumen absoluto.

### 4.2.2 Growth Score

**Pregunta de negocio:** ¿La demanda está creciendo o cayendo dentro del período analizado?
**Fuente/endpoint:** PyTrends / `interest_over_time` (misma serie validada del Trend Score)
**Uso en el MOS:** Sí.

Variables: `V` = serie ordenada `[v1..vn]`; `n`; `k` = tamaño de ventana = `min(30, floor(n/3))` (para series diarias tipo `today 3-m`); `media_inicial` = promedio(`v1..vk`); `media_final` = promedio(`v(n-k+1)..vn`).

Preprocesamiento: misma serie del Trend Score; excluir `isPartial=true`; **si `n < 6`, indicador `unavailable`**; si `media_inicial = 0`, usar `max(media_inicial, 1)`.

```
GrowthRaw = ((media_final - media_inicial) / max(media_inicial, 1)) * 100
GrowthScore = ((GrowthRaw - min(X)) / (max(X) - min(X))) * 100
```

`X` = bounds teóricos fijos (ver 4.6), no población empírica por consulta. Si `max(X)=min(X)` → `GrowthScore = 50`.

Interpretación: `GrowthRaw < -10%` caída; `-10% a 10%` estabilidad; `10% a 30%` crecimiento moderado; `>30%` crecimiento fuerte.
**Limitación:** períodos cortos capturan ruido semanal.

### 4.2.3 Demand Stability Score

**Pregunta de negocio:** ¿La demanda es estable o muy volátil?
**Fuente/endpoint:** PyTrends / `interest_over_time` (misma serie)
**Uso en el MOS:** Sí (llamar "estabilidad de demanda", no "estacionalidad anual" — la ventana es la recibida, ej. 3 meses).

Variables: `µ` = media de la serie `V`; `σ` = desviación estándar poblacional de `V`; `CV = σ / µ`.

Preprocesamiento: misma serie del Trend Score; **si `µ = 0`, indicador `unavailable`**.

```
StabilityRaw = CV = σ / µ
DemandStabilityScore = ((max(X) - StabilityRaw) / (max(X) - min(X))) * 100
```

`X` = bounds teóricos fijos (ver 4.6). Si `max(X)=min(X)` → `50`.

Interpretación: score alto = más estable; score bajo = picos/caídas fuertes.
**Limitación:** no identifica temporadas específicas.

### 4.2.4 Regional Demand Score

**Pregunta de negocio:** ¿El interés está presente en varias regiones del país?
**Fuente/endpoint:** PyTrends / `interest_by_region`
**Campos:** `geoName`, columna del keyword
**Uso en el MOS:** Sí.

Variables: `m` = número de regiones con valor válido; `r_j` = valor de la región `j` (0-100).

Preprocesamiento: eliminar regiones sin valor numérico; **no excluir región con valor 0**; validar `m >= 1`.

```
RegionalDemandRaw = (Σ r_j) / m
RegionalDemandScore = RegionalDemandRaw   (ya es 0-100)
```

**Limitación:** no pondera por población de la región.

> **4.2.5 Regional Spread Insight (HHI) — no entra en el MOS v1.** Queda documentado aquí pero fuera del alcance de implementación de la Fase 3 (es insight de narrativa, usado en la explicación del LLM en la Fase 5, no en el cálculo).

### 4.2.6 Search Intent Score

**Pregunta de negocio:** ¿Las búsquedas relacionadas muestran intención de compra o solo interés informativo?
**Fuente/endpoint:** PyTrends / `related_queries` / **solo `top`** (nunca mezclar con `rising`, que es crecimiento % y puede superar 100)
**Campos:** `query`, `value`
**Uso en el MOS:** Sí.

Variables: `q_j` = texto de la consulta; `w_j` = value de la consulta; `c_j` = peso de intención por regla léxica determinista.

Reglas de `c_j` (case-insensitive, verificar en este orden — la primera regla que coincida gana):
- `1.0` si contiene términos transaccionales: `amazon, lidl, comprar, compra, buy, price, precio, oferta, deal`
- `0.8` si contiene términos de evaluación: `best, mejor, review, opiniones, comparativa, vs, top`
- `0.4` si contiene términos de uso/receta: `how to, como, cómo, recetas, receta, pollo, patatas, boniato, huevos, alitas, salmon, use`
- `0.6` si es marca/modelo/variante no clasificada arriba (fallback — ver nota de implementación abajo)
- `0.0` si es irrelevante

```
IntentRaw = (Σ(w_j * c_j) / Σ w_j) * 100
SearchIntentScore = IntentRaw   (ya es 0-100)
```

**Nota de implementación (no especificada en el documento):** la regla `0.6 "marca/modelo/variante no clasificada"` vs `0.0 "irrelevante"` no tiene un criterio léxico distinguible en el texto — como no hay señal determinística para separar los dos en el v1, tratamos cualquier query que no coincida con ninguna de las 3 reglas específicas (transaccional/evaluación/uso) como `0.6` (fallback optimista: se asume que es una variante de producto, ya que la query proviene de `related_queries` del propio keyword). Documentado como suposición a validar, no una laguna escondida.

Preprocesamiento: usar solo `related_queries.top`; eliminar consultas nulas.

Interpretación: 80-100 predomina intención comercial; 50-80 moderada; <50 predomina uso/investigación.
**Limitación:** clasificación léxica determinista, no semántica profunda.

> **4.2.7 Rising Query Momentum — no entra en el MOS v1.** Insight de narrativa (Fase 5), fuera del alcance de la Fase 3.

## 4.3 Indicadores derivados de World Bank

Campos comunes: `date`, `value`, `country`, `indicator`, `countryiso3code`.

| Código | Nome | Uso |
|---|---|---|
| `NY.GDP.PCAP.CD` | GDP per capita (current US$) | Poder económico medio |
| `FP.CPI.TOTL.ZG` | Inflation, consumer prices (annual %) | Fallback si OECD no disponible |
| `IT.CEL.SETS.P2` | Mobile cellular subscriptions (per 100 people) | Conectividad móvil |
| `IT.NET.USER.ZS` | Individuals using the Internet (% of population) | Penetración de internet |
| `NY.GDP.MKTP.KD.ZG` | GDP growth (annual %) | Crecimiento económico reciente |
| `SP.POP.TOTL` | Population, total | Escala poblacional |
| `SP.URB.TOTL.IN.ZS` | Urban population (% of total population) | Urbanización |

### 4.3.1 GDP per Capita Score
`y_latest` = último valor válido (ordenado por `date`, sin conversión de moneda — ya viene en US$ corrientes).
```
GDPpcRaw = y_latest
GDPpcScore = ((GDPpcRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia (percentil p5/p95). `max(X)=min(X)` → `50`.

### 4.3.2 Internet Penetration Score
`u_latest` = último valor válido, `0 <= u_latest <= 100`.
```
InternetRaw = u_latest
InternetScore = InternetRaw   (truncar a [0,100] si es necesario)
```

### 4.3.3 Mobile Connectivity Score
`m_latest` = último valor válido (acepta `>100`, puede haber más suscripciones que personas).
```
MobileRaw = m_latest
MobileScore = ((MobileRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

### 4.3.4 GDP Growth Score
`k = min(3, años válidos)`; `g_recent` = media de los últimos `k` años (no usar solo 1 año si hay historial).
```
GDPGrowthRaw = (Σ g_t últimos k años) / k
GDPGrowthScore = ((GDPGrowthRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

### 4.3.5 Population Scale Score
`p_latest` = último valor válido; logaritmo natural para no dejar que los países grandes dominen.
```
PopulationRaw = ln(p_latest)
PopulationScore = ((PopulationRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia (bounds ya en escala `ln`). `max(X)=min(X)` → `50`.

### 4.3.6 Urbanization Score
`urb_latest`, `0 <= urb_latest <= 100`.
```
UrbanizationRaw = urb_latest
UrbanizationScore = UrbanizationRaw   (truncar a [0,100])
```

### 4.3.7 World Bank Inflation Score (fallback)
**Solo usar si OECD no tiene serie CPI adecuada para el país/período.** `k = min(3, años válidos)`.
```
InflationRaw = (Σ π_t últimos k años) / k
InflationScore = ((max(X) - InflationRaw) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`. **No duplicar con OECD para el mismo país/período.**

## 4.4 Indicadores derivados de OECD

CPI/HICP mensual interanual. Filtro de serie: `MEASURE=CPI`, `EXPENDITURE=_T` (total, no subcategoría), `TRANSFORMATION=GY` (growth rate 1 año), `ADJUSTMENT=N` (no ajustado estacionalmente), preferir `FREQ=M`. Campos: `TIME_PERIOD`, `OBS_VALUE`, `REF_AREA`, `FREQ`, `MEASURE`, `UNIT_MEASURE`, `EXPENDITURE`, `TRANSFORMATION`.

**No usar** `consumer confidence`, `disposable income`, `household consumption` — no aparecen en los JSONs recibidos.

### 4.4.1 OECD Inflation Level Score
`k = min(12, observaciones mensuales válidas)`; `π_recent` = media de los últimos `k`.
```
InflationLevelRaw = (Σ π_t últimos k períodos) / k
InflationLevelScore = ((max(X) - InflationLevelRaw) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

### 4.4.2 OECD Inflation Volatility Score
Misma serie filtrada del Inflation Level Score. `σ_π` = desviación estándar de los últimos `k` valores (`k = min(12, observaciones válidas)`).
```
InflationVolatilityRaw = std(π_t últimos k períodos)
InflationVolatilityScore = ((max(X) - InflationVolatilityRaw) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

## 4.5 Indicadores derivados de UN Data

Fuente: WPP2024, `indicators_collect`. Campos: `value`, `timeLabel`, `timeMid`, `indicatorId`, `locationId`, `sex`, `ageLabel`, `estimateType`, `estimateMethod`.

| Indicador | Uso |
|---|---|
| `TOTAL_POPULATION` | No usado como fuente principal (World Bank ya lo cubre) — solo para calcular Net Migration Rate |
| `POPULATION_DENSITY` | Contexto, no entra en el MOS v1 |
| `POPULATION_GROWTH` | Sí |
| `NET_MIGRATION` | Sí (normalizado por población) |
| `MEDIAN_AGE` | Condicional (necesita rango etario del producto) |

### 4.5.1 Population Growth Score (UN)
`k = min(3, años válidos)`; acepta valores negativos.
```
PopulationGrowthRaw = (Σ pg_t últimos k años) / k
PopulationGrowthScore = ((PopulationGrowthRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

### 4.5.2 Net Migration Rate Score
Cruzar `NET_MIGRATION` y `TOTAL_POPULATION` (UN, no World Bank) por `locationId`+`timeLabel`/año; eliminar años sin ambos. `k = min(3, años válidos con ambos)`.
```
rate_t = (nm_t / pop_t) * 1000
NetMigrationRateRaw = (Σ rate_t últimos k años) / k
NetMigrationRateScore = ((NetMigrationRateRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`.

### 4.5.3 Median Age Fit Score (condicional)
**Solo calcular si el Product Profile define `target_age_min`(`L`)/`target_age_max`(`U`).** Si no está definido, el indicador se convierte en insight, no entra en el MOS.
`A` = último valor válido de `MEDIAN_AGE`. `C = (L+U)/2`; `W = (U-L)/2`; `D = abs(A - C)`.
```
AgeFitRaw = max(0, 1 - (D / max(W, 1))) * 100
MedianAgeFitScore = AgeFitRaw   (ya es 0-100)
```
**No existe interpretación universal** — mayor edad mediana no es siempre mejor ni peor (depende del producto).

## 4.6 Matriz final de indicadores para el MOS v1

| Indicador atómico | Fuente | Dirección | Entra en el MOS v1 | Normalización (implementación real, ver 4.1) |
|---|---|---|---|---|
| Trend Score | PyTrends | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| Growth Score | PyTrends | Mayor = mejor | Sí | Min-Max, bounds **fijos**: `GrowthRaw ∈ [-50, 150]` (%) |
| Demand Stability Score | PyTrends | Menor CV = mejor | Sí | Min-Max, bounds **fijos**: `CV ∈ [0, 1]` |
| Regional Demand Score | PyTrends | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| Search Intent Score | PyTrends | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| GDP per Capita Score | World Bank | Mayor = mejor | Sí | Min-Max, población de referencia (p5/p95) |
| Internet Penetration Score | World Bank | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| Mobile Connectivity Score | World Bank | Mayor = mejor | Sí | Min-Max, población de referencia (p5/p95) |
| GDP Growth Score | World Bank | Mayor = mejor | Sí | Min-Max, población de referencia (p5/p95) |
| Population Scale Score | World Bank | Mayor = mejor | Sí | Min-Max sobre `ln(pop)`, población de referencia (p5/p95) |
| Urbanization Score | World Bank | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| OECD Inflation Level Score | OECD | Menor = mejor | Sí si disponible | Min-Max, población de referencia (p5/p95) |
| OECD Inflation Volatility Score | OECD | Menor = mejor | Sí si disponible | Min-Max, población de referencia (p5/p95) |
| World Bank Inflation Score | World Bank | Menor = mejor | Fallback (solo si OECD ausente) | Min-Max, población de referencia (p5/p95) |
| Population Growth Score | UN Data | Mayor = mejor | Sí | Min-Max, población de referencia (p5/p95) |
| Net Migration Rate Score | UN Data | Mayor = mejor | Sí | Min-Max, población de referencia (p5/p95) |
| Median Age Fit Score | UN Data + Product Profile | Depende del producto | Condicional | Passthrough (ya 0-100) |

Regional Spread Insight y Rising Query Momentum (PyTrends) **no entran en el MOS v1** — fuera del alcance de la Fase 3.

## 4.7 Reglas para datos ausentes y duplicados

| Situación | Regla |
|---|---|
| OECD disponible para inflación | Usar OECD Inflation Level + Volatility. **No** usar World Bank Inflation para el mismo país/período. |
| OECD no disponible | Usar World Bank Inflation Score como fallback. |
| UN total population y World Bank population disponibles | Usar World Bank para Population Scale. Usar UN total population **solo** para calcular Net Migration Rate. |
| `related_topics` top/rising = null | No calcular. Registrar como no disponible. |
| `trending_rss` disponible | No usar en el MOS (es tendencia general del país, no del producto). |
| Solo existe un país en la comparación | No es posible Min-Max entre países de la consulta — pero en la implementación real esto no se aplica (la normalización es contra población de referencia fija, no por consulta). |
| `max(X)=min(X)` | Asignar 50 (indicador sin peso efectivo — Entropy Weight lo va a desvalorizar naturalmente). |
| Valor parcial de PyTrends | Excluir si `isPartial=true`. |

## 4.8 Contrato funcional para el MarketMind Core

Cada indicador enviado al motor MOS mantiene valor bruto, score normalizado y fuente — mapea 1:1 a la tabla `indicator_values` (ya creada en la Fase 0):

| Campo funcional | Descripción | Columna en `indicator_values` |
|---|---|---|
| `indicator_code` | Nombre interno, ej. `TREND_SCORE` | `indicator_code` |
| `source` | `PYTRENDS`, `WORLDBANK`, `OECD`, `UNDATA` | `source` |
| `raw_value` | Valor antes de normalizar | `raw_value` |
| `normalized_score` | Valor 0-100 usado por el MOS | `normalized_score` |
| `direction` | `DIRECT` o `INVERSE` | `direction` |
| `calculation_window` | Ventana usada: `3m`, `últimos 12 meses`, `últimos 3 años`, etc. | `calculation_window` |
| `availability_status` | `AVAILABLE`, `UNAVAILABLE`, `FALLBACK`, `CONDITIONAL` | `availability_status` |
| `notes` | Explicación breve para auditoría/reporte | `notes` |

## 4.9 Conclusión

Este capítulo convierte los JSONs recibidos en una metodología reproducible: campos concretos → variables definidas → fórmula determinista → escala común. El MOS no se calcula con datos brutos ni opiniones: se calcula con indicadores normalizados, rastreables y comparables. La decisión metodológica más importante es evitar pesos internos inventados — el Core alimenta el motor Entropy Weight + TOPSIS (Fase 4) con indicadores atómicos, y los pesos finales emergen de los datos, no de suposición arbitraria.
