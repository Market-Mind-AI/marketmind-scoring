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

> **CORRECCIÓN ARQUITECTURAL ADOPTADA (actualizada 2026-07-08, ver memoria `marketmind_mos_engine`):** el texto de abajo describe Min-Max **por consulta** (`X` = valores solo de los mercados comparados en la ejecución actual). El usuario rechazó esa relatividad en dos etapas:
> 1. Primero se pasó a una **población de referencia fija** con percentil p5/p95 (versionada por `period_key`).
> 2. **2026-07-08 — decisión final:** se eliminó por completo la normalización por percentil (`MINMAX_REFERENCE`). El producto solo colecta datos de los países que el usuario efectivamente pide en cada `/analyze`, así que una "cesta de referencia" nunca se puebla orgánicamente sin forzar cada análisis a cubrir todos los países de referencia — lo que anula el propósito de la selección por país. **Ahora `X` es un bound absoluto fijo por indicador** (`normalization_reference`, `period_key="FIXED"`), idéntico para un país analizado solo o junto a otros 20 en la misma llamada. Los bounds **se auto-amplían** (nunca se estrechan) la primera vez que un valor real observado cae fuera del rango vigente (`indicator_repository.widen_reference_bounds`), así que la regla absoluta solo gana precisión con el tiempo en vez de recortar un outlier real en un límite adivinado. Las fórmulas de **valor bruto** de abajo permanecen exactas; solo el `X` del paso de normalización cambia. El score de cada indicador ya es información suficiente para saber si un país es bueno o no para el producto — **no se compara contra otros países**.

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

> **OBJETIVO DA DIMENSÃO DEMANDA (decisão fundacional, 2026-07-14):** a Demanda mede **INTERESSE DE COMPRA ONLINE** — usuários que buscam ativamente comprar o produto online — e **NÃO** consumo real/offline. Consequência (e é intencional, não defeito): um mercado onde o produto é consumido por **hábito e comprado offline** (ex.: jamón na Espanha, comprado no mercado físico) **legitimamente NÃO lidera** o score de demanda; a oportunidade para um **vendedor online** está onde há **busca ativa de compra** (ex.: o segmento gourmet online no Brasil/EUA que procura "comprar / preço / <varejista>"). Por isso o instrumento correto é o sinal de BUSCA (Google Trends) — ele mede o canal online. Isso torna defensável na banca que a Espanha não domine: o consumo espanhol é offline/saturado, fora do que a métrica (e o produto) se propõe a medir.

Endpoints usados para el MOS: `interest_over_time` (Growth + Demand Stability), `interest_by_country` (Trend Score, worldwide), `interest_by_region_worldwide` (Regional Demand, worldwide), `related_queries` (solo `top`, Search Intent). `suggestions` es identificación de producto (Fase 2, no entra al MOS). `related_topics` llegó null/null. `trending_rss` es general del país, no del producto. `categories` es catálogo auxiliar.

> **PRINCIPIO DE COMPLETUD 2026-07-11 (decisión del usuario, transversal a todo el MOS):** los 5 indicadores de Demanda son **obligatorios** — el MOS jamás se calcula sobre datos parciales. Todos los países de una consulta llevan el MISMO conjunto de indicadores en la MISMA regla 0-100; es eso lo que los hace comparables. La recolección debe COMPLETARSE para todos los países antes de calcular cualquier score/MOS (portón de completud en `analysis_service`); si algo falta tras la recolección, la consulta **falla por entero** en vez de devolver un ranking parcial/asimétrico. Los límites de tasa de Google se resuelven con reintento + el fallback DataForSEO (live), nunca saltándose una señal. La Demanda es la media simple de sus 5 señales (todas presentes → conjunto uniforme → media comparable).
>
> **Keyword per-país = término GANADOR (2026-07-11):** las señales per-país (Growth/Demand Stability/Regional Demand/Search Intent) se consultan (geo=XX) con el término local que **ganó** el arbitraje mundial — el de mayor valor en `interest_by_country` para ese país — no con el primer candidato del LLM. Motivo: el nombre local más específico (p.ej. "presunto de porco preto") sirve para el arbitraje mundial pero puede ser demasiado nicho para tener una serie temporal per-país propia (Google devuelve vacío/404); el ganador (p.ej. "presunto pata negra") sí tiene volumen. El dato ya nos dijo cuál es el término real — se reutiliza ese veredicto en todas las consultas per-país (`app/services/indicators/pytrends.py::best_localized_term`).

### 4.2.1 Trend Score

> **CORRECCIÓN 2026-07-10 (ver `marketmind_mos_engine`):** el Trend Score dejó de leer `interest_over_time` **por país** (que Google escala 0-100 al pico DE ESE país, así que no es comparable entre países — un término cerca de su pico nacional en EE.UU. superaba al mercado cultural del producto, p.ej. jamón/España, invirtiendo el ranking del MOS). Ahora lee la consulta **mundial** de Google Trends (`geo=''`, `resolution='COUNTRY'`, tabla `interest_by_country` del servicio pytrends), donde cada país está en una **régua global 0-100** = proporción de las búsquedas de ese país dedicadas al término (afinidad cultural — España ≈ 100 para jamón). Es independiente de la consulta (siempre mundial). Growth (4.2.2) y Demand Stability (4.2.3) siguen leyendo la serie `interest_over_time` por país (son razones/forma, ya comparables entre países).
>
> **KEYWORD LOCALIZADA 2026-07-10 (misma corrección):** la régua mundial mide la fracción de búsquedas por el **término exacto**, así que un único keyword en un idioma penaliza a los países que buscan el producto con otra palabra — Portugal salía en 5 con "jamón" (en español) porque busca **"presunto"**, un artefacto lingüístico, no falta de demanda, que dejaba su Trend Score bajo el piso no-compensatorio. Solución: cada país se mide con el término en **su propio idioma**, y **todos los términos localizados se comparan en un único eje** de Google Trends (comparación multi-término: `build_payload([t1,t2,...], geo='')`, encadenamiento con término-âncora cuando hay >5 idiomas). El LLM de `identify-image` genera el mapa `{idioma: término nativo}` (`product_catalog.localized_terms`); cada país usa `countries.search_language` para leer, en `interest_by_country`, la fila `(country, term_language)` — el valor de SU término en el eje común. Así España lee "jamón ibérico" (es), Portugal "presunto ibérico" (pt) e Inglaterra "iberico ham" (en), todos comparables.
>
> **ARBITRAJE POR DATOS + ACERVO 2026-07-11 (refuerzo):** un LLM que devuelve UN término por idioma puede dar una traducción literal errónea en vez del nombre real de mercado (p.ej. el jamón ibérico en Portugal se llama **"presunto de porco preto"** / **"presunto pata negra"**, no el literal "presunto ibérico"). Para no depender de que el LLM acierte, ahora devuelve una **LISTA de nombres locales reales** por idioma (`product_catalog.localized_terms` pasa a `{idioma: [variaciones]}`), TODAS entran en la comparación multi-término, y **el propio volumen de Google Trends arbitra** cuál es el nombre real. El Trend Score de cada país es el **MÁXIMO** de las variaciones de su idioma — el nombre local ganador que el dato eligió (decisión del usuario 2026-07-11, sustituyendo a la SUMA). El MÁXIMO es **independiente del número de candidatos** por idioma: la SUMA inflaba a quien tuviera más sinónimos, contaba doble los cuasi-sinónimos y, tras el clamp a 100, empataba a todos los países fuertes en 100 (España y Portugal ambos 100). El acervo de variaciones es **acumulativo**: cada `/analyze` fusiona los candidatos nuevos del LLM en la lista existente (dedup por clave normalizada), también en entidades ya creadas. Nota: el Trend Score mide **fracción de las búsquedas del país** (share), no volumen absoluto — el tamaño de mercado absoluto entra por la dimensión Market Size, no por Demanda.

**Pregunta de negocio:** ¿Cuánta demanda relativa por el producto tiene este país frente al mundo?
**Fuente/endpoint:** PyTrends / `interest_by_country` (worldwide, global 0-100 axis, multi-término localizado)
**Campos JSON:** `country` (ISO-2), `term`, `term_language` (ISO-639-1), `interest_value`
**Uso en el MOS:** Sí — indicador atómico de demanda, ahora comparable entre países.

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

> **ALCANCE 2026-07-11 (decisión del usuario):** Regional Demand es un indicador **PER-PAÍS** — la dispersión geográfica del interés DENTRO del país (`interest_by_region`, geo=XX, escalado al pico del propio país). NO se intentó hacerlo comparable entre países vía consulta sub-nacional mundial: eso solo lo da Google (`geo=''`,`resolution=REGION`), que rate-limita agresivamente y no tiene equivalente en DataForSEO. La comparabilidad cross-country la aporta el **Trend Score** (el "total mundial" = `interest_by_country`); Regional Demand se lee con el keyword localizado del país (geo=XX), como siempre.

**Pregunta de negocio:** ¿El interés está presente en varias regiones del país?
**Fuente/endpoint:** PyTrends / `interest_by_region` (per-país, geo=XX)
**Campos:** `geoName`, columna del keyword
**Uso en el MOS:** Sí (obligatorio).

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
| `FP.CPI.TOTL.ZG` | Inflation, consumer prices (annual %) | Única fuente de inflación (2026-07-08; OECD eliminado) |
| `IT.CEL.SETS.P2` | Mobile cellular subscriptions (per 100 people) | Conectividad móvil |
| `IT.NET.USER.ZS` | Individuals using the Internet (% of population) | Penetración de internet |
| `NY.GDP.MKTP.KD.ZG` | GDP growth (annual %) | Crecimiento económico reciente |
| `SP.POP.TOTL` | Population, total | Escala poblacional |
| `SP.URB.TOTL.IN.ZS` | Urban population (% of total population) | Urbanización |
| `NE.CON.PRVT.ZS` | Households and NPISHs final consumption expenditure (% of GDP) | Estructura de demanda — Consumo (C) |
| `NE.CON.GOVT.ZS` | General government final consumption expenditure (% of GDP) | Estructura de demanda — Gobierno (G), **solo contexto** |
| `NE.GDI.FTOT.ZS` | Gross fixed capital formation (% of GDP) | Estructura de demanda — Inversión (I) |
| `NE.RSB.GNFS.ZS` | External balance on goods and services (% of GDP) | Estructura de demanda — Exportaciones netas (NX) |
| `NE.TRD.GNFS.ZS` | Trade (% of GDP) | Estructura de demanda — Apertura comercial |

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

### 4.3.7 World Bank Inflation Score (**única fuente de inflación desde 2026-07-08**)
`FP.CPI.TOTL.ZG` (inflación anual del IPC, ya un % limpio). `k = min(3, años válidos)`.
```
InflationRaw = (Σ π_t últimos k años) / k
InflationScore = ((max(X) - InflationRaw) / (max(X) - min(X))) * 100
```
`X` = bound fijo `[-5, 50]` (`period_key="FIXED"`, auto-ampliable). **Antes era fallback de OECD; ahora es la única fuente de inflación** — el camino OECD (4.4.1/4.4.2) se eliminó porque `marketmind-oecd` no persiste las dimensiones SDMX (`EXPENDITURE`/`TRANSFORMATION`/`ADJUSTMENT`) necesarias para aislar una serie de crecimiento anual limpia, así que `measure='CPI'` mezclaba varios sub-índices reindexados distintos (confirmado en vivo: mismo `time_period`, valores de 114 a 146). Ver memoria `marketmind_mos_engine`.

### 4.3.8 Consumer Spending Score (añadido 2026-07-08)

**Pregunta de negocio:** ¿cuánto del PIB se explica por el consumo privado de los hogares? Primer término de la
identidad macroeconómica `PIB = C + I + G + NX`, sugerido en conversación externa con el usuario y su colega de
proyecto, confirmado contra la API real del World Bank antes de implementar.
`c_latest` = último valor válido (ya viene en % del PIB, no en valor absoluto — a propósito, para no duplicar la
señal de GDP per Capita Score, ver 4.3.1).
```
ConsumerSpendingRaw = c_latest
ConsumerSpendingScore = ((ConsumerSpendingRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia (percentil p5/p95). `max(X)=min(X)` → `50`. Dimensión: Macro Demand Structure.

### 4.3.9 Government Expenditure Score — CONTEXTO, no entra al MOS (añadido 2026-07-08)

**Pregunta de negocio:** ¿cuánto del PIB se explica por el gasto público? Segundo término de la identidad
(`G`). **No tiene una dirección mayor/menor defendible** — confirmado contra dato real: Francia y Dinamarca
(~23-24% del PIB, entre los más altos del mundo) son mercados consumidores maduros y ricos, mientras India
(~11%) es un mercado en desarrollo con poder de compra bien menor. El gasto público en % del PIB refleja
*modelo económico* (estado de bienestar vs. economía en desarrollo), no calidad del mercado consumidor privado —
mismo principio que Median Age Fit (4.5.3): "no existe interpretación universal".
```
GovernmentExpenditureRaw = g_latest   (guardado, nunca normalizado)
```
No se calcula `GovernmentExpenditureScore`. El `raw_value` se persiste en `indicator_values` únicamente para
alimentar el contexto de la explicación del LLM (ver 4.8) — nunca entra a `dimension_mapping.py`, nunca se agrupa
en ninguna dimensión, nunca influye en TOPSIS ni en el MOS.

### 4.3.10 Investment Score (añadido 2026-07-08)

**Pregunta de negocio:** ¿cuánto del PIB se reinvierte en capital fijo? Tercer término de la identidad (`I`) —
proxy de inversión/infraestructura, ya que pocos países publican "inversión en infraestructura" como campo
propio. `i_latest` = último valor válido (% del PIB).
```
InvestmentRaw = i_latest
InvestmentScore = ((InvestmentRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`. Dimensión: Macro Demand Structure.

### 4.3.11 Net Exports Score (añadido 2026-07-08)

**Pregunta de negocio:** ¿el país es exportador neto o importador neto? Cuarto término de la identidad (`NX`).
`nx_latest` = último valor válido (% del PIB, puede ser negativo — déficit comercial).
```
NetExportsRaw = nx_latest
NetExportsScore = ((NetExportsRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`. Dimensión: Macro Demand Structure.
**Limitación reconocida:** superávit comercial no es necesariamente "mejor" para la pregunta de negocio del MOS
("¿vende bien este producto aquí?") — un país con déficit comercial puede ser un mercado más abierto a
productos importados. Se mantiene `mayor=mejor` por convención del Cap. 4, junto a Trade Openness Score (4.3.12)
como señal complementaria, no sustituta — decisión explícita del usuario 2026-07-08.

### 4.3.12 Trade Openness Score (añadido 2026-07-08)

**Pregunta de negocio:** ¿qué tan integrada está esta economía al comercio global? No es uno de los 4 términos
de la identidad — es una magnitud complementaria (exportaciones + importaciones, no una participación que
compite por espacio con C/I/G/NX). `to_latest` = último valor válido (% del PIB).
```
TradeOpennessRaw = to_latest
TradeOpennessScore = ((TradeOpennessRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`. Dimensión: Macro Demand Structure. Mayor apertura
comercial generalmente implica un camino regulatorio/logístico más fácil para que un producto extranjero entre.

## 4.4 Indicadores derivados de OECD

> **4.4.1 OECD Inflation Level Score y 4.4.2 OECD Inflation Volatility Score — ELIMINADOS 2026-07-08.** El capítulo pedía filtrar la serie SDMX por `MEASURE=CPI`, `EXPENDITURE=_T`, `TRANSFORMATION=GY`, `ADJUSTMENT=N`, pero `marketmind-oecd` solo persiste `REF_AREA/INDICATOR/MEASURE/FREQ/UNIT` — las otras tres dimensiones no quedan almacenadas en `oecd_observations`. Con solo `measure='CPI'`, el mismo `time_period` devuelve varias filas de sub-índices reindexados no relacionados (confirmado en vivo para ESP: 2025-Q4 con valores de 114 a 146), así que el promedio no era una tasa de inflación real. **La inflación ahora viene únicamente de World Bank (`FP.CPI.TOTL.ZG`, ver 4.3.7)**, que ya es un % anual limpio. Reinstaurar 4.4.1/4.4.2 solo si `marketmind-oecd` empieza a persistir la clave SDMX completa. La única métrica OECD activa es Real Estate Growth (4.4.3). Ver memoria `marketmind_mos_engine`.

### 4.4.3 Real Estate Growth Score (añadido 2026-07-08, condicional)

**Pregunta de negocio:** ¿el mercado inmobiliario está calentándose o enfriándose? Dataflow
`DSD_AN_HOUSE_PRICES@DF_HOUSE_PRICES` (agencia `OECD.ECO.MPD`), medida `RHP` (Real House Prices), serie
trimestral. **Confirmado contra dato real (ESP y USA) que el índice está rebasado a 100 por país en su período
base (2015)** — el *nivel* no es comparable entre países, misma limitación ya documentada para el Trend Score
de Google Trends (4.2.1: "100 es el pico dentro del período, no volumen absoluto"). Solo una **tasa de
crecimiento** calculada dentro de la misma serie es válida — rebasear no afecta una razón entre dos puntos de la
misma serie, mismo principio ya usado en GDP Growth Score (4.3.4).

Variables: `V` = serie trimestral ordenada por `time_period` descendente, últimos 12 trimestres (~3 años);
`recent` = promedio de los 4 trimestres más recientes; `old` = promedio de los 4 trimestres más antiguos dentro
de esa ventana.

Preprocesamiento: **si `n < 8` trimestres (no hay 2 ventanas de 4), indicador `CONDITIONAL` sin valor** — solo
países OCDE publican este dataflow, nunca es obligatorio.
```
RealEstateGrowthRaw = ((recent - old) / max(abs(old), 1)) * 100
RealEstateGrowthScore = ((RealEstateGrowthRaw - min(X)) / (max(X) - min(X))) * 100
```
`X` = población de referencia. `max(X)=min(X)` → `50`. Dimensión: Economic Readiness (no Macro Demand Structure
— es momentum/efecto riqueza, no uno de los 4 términos de la identidad `C+I+G+NX`).

**Pendiente de optimización (no bloqueante):** el layout posicional de la clave de serie SDMX de este dataflow
en `marketmind-oecd` (`_SERIES_KEY_LAYOUT`) no fue confirmado contra la datastructure real todavía — la
colección usa el fallback "all" ya existente en ese servicio (más lento, pero funcional), igual que pasaba con
CPI antes de confirmarse su layout.

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
| GDP per Capita Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[500, 100000]` |
| Internet Penetration Score | World Bank | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| Mobile Connectivity Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[0, 150]` |
| GDP Growth Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[-10, 10]` |
| Population Scale Score | World Bank | Mayor = mejor | Sí | Min-Max sobre `ln(pop)`, bound fijo auto-ampliable `[ln(1M), ln(1.5B)]` |
| Urbanization Score | World Bank | Mayor = mejor | Sí | Passthrough (ya 0-100) |
| World Bank Inflation Score | World Bank | Menor = mejor | Sí (única fuente de inflación) | Min-Max, bound fijo auto-ampliable `[-5, 50]` |
| Population Growth Score | UN Data | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[-3, 5]` |
| Net Migration Rate Score | UN Data | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[-15, 15]` |
| Median Age Fit Score | UN Data + Product Profile | Depende del producto | Condicional | Passthrough (ya 0-100) |
| Consumer Spending Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[30, 85]` |
| Investment Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[15, 45]` |
| Net Exports Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[-15, 15]` |
| Trade Openness Score | World Bank | Mayor = mejor | Sí | Min-Max, bound fijo auto-ampliable `[20, 200]` |
| Government Expenditure Score | World Bank | Sin dirección defendible | **No — contexto únicamente** | Ninguna (nunca se normaliza) |
| Real Estate Growth Score | OECD | Mayor = mejor | Condicional (solo OCDE) | Min-Max, bound fijo auto-ampliable `[-30, 60]` |

Todos los bounds fijos (`period_key="FIXED"`) se auto-amplían cuando un valor real cae fuera del rango (nunca comparan contra otros países). OECD Inflation Level/Volatility fueron eliminados (ver 4.4). Regional Spread Insight y Rising Query Momentum (PyTrends) **no entran en el MOS v1** — fuera del alcance de la Fase 3.

## 4.7 Reglas para datos ausentes y duplicados

| Situación | Regla |
|---|---|
| Inflación | Única fuente: World Bank `FP.CPI.TOTL.ZG` (el camino OECD se eliminó 2026-07-08, ver 4.4). |
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
| `business_context` | Frase fija en inglés — qué significa este indicador (contexto para LLM, añadido 2026-07-08) | `business_context` |
| `interpretation_band` | Very Low/Low/Moderate/High/Very High derivado del `normalized_score` (añadido 2026-07-08) | `interpretation_band` |

`business_context`/`interpretation_band` son determinísticos (`app/services/mos/interpretation.py`), nunca
generados por el LLM — alimentan la explicación de la Fase 5, no participan del cálculo.

## 4.9 Conclusión

Este capítulo convierte los JSONs recibidos en una metodología reproducible: campos concretos → variables definidas → fórmula determinista → escala común. El MOS no se calcula con datos brutos ni opiniones: se calcula con indicadores normalizados, rastreables y comparables. La decisión metodológica más importante es evitar pesos internos inventados — el Core alimenta el motor Entropy Weight + TOPSIS (Fase 4) con indicadores atómicos, y los pesos finales emergen de los datos, no de suposición arbitraria.
