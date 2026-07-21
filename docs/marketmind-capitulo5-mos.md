# MarketMind — Capítulo 5: Cálculo del Market Opportunity Score (MOS)

> Transcrito del PDF `MarketMind_Capitulo5 - MOS.pdf`, adjuntado por el usuario el 2026-07-02. Fuente única de verdad para el motor de MOS (`app/services/mos_engine.py`). Ver `docs/marketmind-implementation-plan.md` sección 8 y memoria `marketmind_mos_engine` para las correcciones arquitecturales adoptadas sobre el texto literal (MOS absoluto, no "no existe un MOS absoluto" como dice la sección 5.8 abajo).

## 5.0 Objetivo

El MOS es el indicador final de MarketMind para medir el potencial comercial de un producto en uno o más mercados. No usa datos crudos de las APIs — todos los datos ya fueron transformados en indicadores determinísticos normalizados 0-100 (Capítulo 4). Este capítulo describe cómo esos indicadores se combinan mediante un método multicriterio para producir el MOS.

## 5.1 Flujo general

```
Consulta del usuario → Validación de países → Obtención de indicadores (Cap. 4)
→ Construcción de la matriz → Entropy Weight → TOPSIS → MOS (0-100) → LLM explica
```

El LLM nunca participa en el cálculo del MOS.

## 5.2 Entrada del algoritmo

El usuario informa: producto + lista de países. Si no se informa ningún país, el cálculo no comienza.

> **Corrección adoptada:** como el MOS pasó a ser absoluto e independiente por país (ver 5.8), la exigencia de múltiples países deja de tener sentido matemáticamente — 1 único país es suficiente. Solo sigue vigente "al menos 1 país".

## 5.3 Validación de disponibilidad

Antes de construir la matriz, el Core verifica si hay datos suficientes por país. Cada país necesita tener todos los indicadores **obligatorios** definidos en el Capítulo 4. Si un indicador obligatorio no está disponible: `availability_status = UNAVAILABLE`, el país queda excluido de la comparación. **Sin imputación de datos, sin estimación vía IA.**

## 5.4 Construcción de la matriz de decisión

Después de validar los países, el Core construye la matriz automáticamente: filas = países, columnas = indicadores, todos ya normalizados 0-100 (Cap. 4). Esa normalización garantiza que ningún indicador tenga mayor influencia solo por usar una escala diferente.

> **Corrección adoptada (2026-07-08, ver `marketmind_mos_engine`):** la normalización 0-100 del Cap. 4 usa **límites absolutos fijos por indicador** (auto-ampliables), **no** una población de referencia por percentil ni los países de la consulta. Consecuencia para el MOS: es una escala **absoluta de 0 a 100 por país** (0 = pésimo, 100 = perfecto para ese producto), calculada de forma **independiente por país — sin comparar un país contra otro**. Correr `/analyze` para 1 país o para 20 da exactamente el mismo MOS para cada país. El TOPSIS (5.6) usa ideal=100 / anti-ideal=0 **fijos**, no derivados de los países de la consulta, coherente con esto. Además, OECD Inflation Level/Volatility fueron **eliminados** como indicadores (World Bank es la única fuente de inflación) — ver Cap. 4 §4.4.

## 5.5 Entropy Weight

Método elegido (junto con TOPSIS) porque calcula automáticamente la importancia de cada indicador sin intervención humana — sin pesos manuales, sin influencia humana.

**Principio:** los indicadores que diferencian poco a los mercados comparados (ej. todos los países con Internet ~95%) reciben peso bajo; los indicadores que diferencian mucho (ej. Growth variando de 18 a 77) reciben peso alto. Al final, cada indicador recibe un peso entre 0 y 1, suma total = 1.

> **Corrección adoptada (ver `marketmind_mos_engine`):** el texto literal calcula los pesos **por consulta**, contra los países comparados en la ejecución actual — eso es exactamente la relatividad que el usuario rechazó (ver 5.8). En la implementación real:
> 1. **Agregación en 2 etapas:** los ~16 indicadores atómicos se agrupan en 5 dimensiones (Demand, Digital Readiness, Economic Readiness, Market Size, Demographic Sustainability) — sub-score por dimensión primero (promedio de los indicadores disponibles de esa dimensión), Entropy Weight corre sobre las 5 dimensiones, no sobre los indicadores crudos. Evita que Demand sea diluido por tener menos indicadores que el bloque macro.
> 2. **Pesos calculados periódicamente contra una población de referencia fija** (no en cada consulta), versionados por `period_key`, inmutables después de que el período cierra — tabla `indicator_weights`.
> 3. **El bloque Demand es por producto** (sus indicadores — Trend/Growth/Stability/Regional/Search Intent — son específicos del producto, no tienen una población de países natural). Decisión del usuario (2026-07-02): al calibrar pesos para un producto, el Core garantiza cobertura de ese producto en los ~27 países de referencia (no solo en los países de la consulta del usuario) y calibra el peso del Demand contra esa base fija — el mismo producto siempre genera el mismo peso de Demand, independientemente de qué países se compararon en la consulta.

> **Corrección posterior (2026-07-03, ver `marketmind_mos_engine`):** Entropy Weight fue abandonado como método de producción — pruebas en vivo mostraron que subvaloraba Demand sistemáticamente (2,6%-5% del peso total) por un problema conceptual (variancia ≠ importancia), no solo falta de dato. Reemplazado por **Rank Order Centroid (ROC)**, que convierte un orden de prioridad explícito en pesos matemáticos.
>
> **Corrección posterior (2026-07-08, ver `marketmind_mos_engine`):** se añadió una 6ª dimensión, **Macro Demand Structure** (identidad `PIB = C + I + G + NX`), y ROC fue reemplazado por **Rank Reciprocal (RR)** — misma familia de "surrogate weighting" (Barron & Barrett 1996), elegida porque el usuario pidió acercar los pesos de Market Size y Economic Readiness sin ajustar números a mano (ver 5.5.1).

> **Corrección posterior (2026-07-10, ver `marketmind_mos_engine`):** se reordenó la prioridad para **reducir el sesgo de "país grande y rico"** (GDP per capita en Economic Readiness y población en Market Size hacían que un país grande/rico ganara sin importar el fit con el producto — fue la razón de que EE.UU. superara a España para jamón). Orden final: **Demand > Macro Demand Structure > Digital Readiness > Economic Readiness > Demographic Sustainability > Market Size**. Pesos RR (por rango, no cambian): 40,82% / 20,41% / 13,61% / 10,20% / 8,16% / 6,80% — pero ahora Economic Readiness y Market Size reciben los dos pesos más bajos. Cambio de *orden*, no de números (sin pesos arbitrarios). Además, el MOS ahora se calibra y calcula **automáticamente** al final de `/analyze` (`analysis_service` llama `WeightCalibrationService.calibrate` antes de `compute_mos` — RR es determinista, no depende de dato de país), sin el paso manual `POST /admin/mos/refresh-weights`.

> **CAP NÃO-COMPENSATÓRIO REMOVIDO (2026-07-15, decisão do usuário):** existia um `trend_score_cap` que travava o MOS (em 39 se `trend_score<20`, em 59 se `<40`) para impedir que o macro compensasse a falta de demanda. Foi **removido** — o MOS é agora o **TOPSIS direto** (`mos = topsis*100`, sem cap). A Demanda é uma dimensão ponderada como as outras.
>
> **✅ PONTO RESOLVIDO — PESO/ESTRUTURA DA DEMANDA (2026-07-16):** o Brasil (maior demanda, 5º no MOS) foi corrigido com duas mudanças fundamentadas (sem chutar pesos):
> 1. **Nível linear** (Cap. 4 §4.2): score proporcional ao volume, não `ln`.
> 2. **Demanda dividida em duas dimensões** — **DEMAND_VOLUME** (tamanho de mercado/potencial: o nível de volume absoluto) e **DEMAND_QUALITY** (crescimento+estabilidade+intenção). A ordem Rank Reciprocal passou a **7 dimensões**: **DEMAND_VOLUME(1) > DEMAND_QUALITY(2) > Macro Demand Structure(3) > Digital Readiness(4) > Economic Readiness(5) > Demographic Sustainability(6) > Market Size(7)**. Pesos RR (só da ordem, sem números à mão): **38,57% / 19,29% / 12,86% / 9,64% / 7,71% / 6,43% / 5,51%**.
> **Por que o split (e não só repesar):** diagnóstico com dado real mostrou que o volume estava **diluído na média** com os sinais de qualidade (independentes de tamanho), então um mercado pequeno de alta qualidade (Japão, crescimento explosivo) empatava um enorme (Brasil); nem linearizar sozinho nem inflar o peso da Demanda-única resolvia (o Japão continuava 1º nos dois). Separar o VOLUME numa dimensão de topo, não-diluída, resolve. **Efeito validado (air fryer): Brasil 5º→1º; Japão 1º→5º.** O "pequeno-bom" não é desprezado — o Japão tem a maior DEMAND_QUALITY, 2ª dimensão mais pesada. Ver `marketmind_mos_engine`.

### 5.5.1 Por qué Rank Reciprocal y no un ajuste manual (2026-07-08)

El usuario pidió explícitamente "dejar los pesos más parejos" entre Market Size y Economic Readiness, incluso
sugiriendo tomar peso de Demand/Macro Demand Structure para redistribuir. Ajustar los números a mano habría
reintroducido exactamente el "peso arbitrario" que motivó abandonar Entropy Weight (Cap. 4: "sin pesos
arbitrarios"). En su lugar se cambió la *fórmula* (no los números): Rank Reciprocal
(`w_j = (1/j) / Σ(1/k, k=1..n)`) mantiene el peso de Demand casi idéntico a ROC (40,82% vs. 40,83%) mientras
reduce principalmente Macro Demand Structure y redistribuye hacia las dimensiones inferiores — resuelve la
necesidad real sin violar el principio metodológico central del proyecto. Se descartó Rank Sum (más "parejo"
todavía, pero le habría costado ~12pp de peso a Demand, reabriendo el problema que motivó la salida de Entropy
Weight).

## 5.6 TOPSIS

Responde: ¿qué país representa la mejor oportunidad de mercado? Imagina un **mercado ideal** (mejores valores posibles en todos los indicadores simultáneamente) y un **mercado pésimo** (peores valores posibles). Cada país se compara con los dos extremos — TOPSIS calcula la distancia a cada uno. El mejor mercado es el más cercano al ideal y más distante del pésimo. Al final, cada país recibe un coeficiente entre 0 y 1. Proceso completamente determinístico, sin IA.

> **Corrección adoptada:** como los indicadores (ahora dimensiones, ver 5.5) ya están en una escala fija 0-100, el vector ideal es siempre **100 en todo** y el anti-ideal siempre **0 en todo** — ya no el mejor/peor observado entre los países de la consulta actual. Esto también elimina el paso clásico de normalización vectorial del TOPSIS (que divide cada valor por la norma euclidiana entre las alternativas comparadas — reintroduciría la relatividad); solo permanece la distancia ponderada, que es lo que de hecho captura la importancia de cada dimensión.

## 5.7 Cálculo del MOS

```
MOS = TOPSIS × 100
```

El MOS no es un promedio de los indicadores — representa la posición relativa de cada país después de considerar simultáneamente todos los indicadores, los pesos del Entropy Weight, y la proximidad al mercado ideal vía TOPSIS.

> **Corrección adoptada (techo no compensatorio, ver `marketmind_mos_engine`):** el Cap. 5 no describe esto — decisión adicional del usuario para impedir que indicadores macro (población, PIB, internet) compensen la ausencia de demanda real:
> ```
> MOS_final = min(MOS_topsis, teto(TrendScore))
> TrendScore < 20   → teto = 39
> 20 ≤ TS < 40       → teto = 59
> TS ≥ 40            → sem teto
> ```
> Cuando el techo reduce el MOS, la respuesta expone `capped_by`/`mos_before_cap` explícitamente.

## 5.8 Interpretación

| MOS | Interpretación |
|---|---|
| 90–100 | Excelente oportunidad |
| 75–89 | Alta oportunidad |
| 60–74 | Buena oportunidad |
| 40–59 | Oportunidad moderada |
| 20–39 | Baja oportunidad |
| 0–19 | Oportunidad muy baja |

**Texto literal (original en portugués):** "O MOS sempre deve ser interpretado dentro do conjunto de países comparados. Não existe um MOS absoluto."

> **DECISIÓN DEL USUARIO QUE REEMPLAZA ESTE TEXTO (ver `marketmind_mos_engine`):** el usuario rechazó explícitamente esa relatividad. El MOS es absoluto e independiente por país — un "70" significa lo mismo en cualquier consulta, sin importar qué otros países se compararon junto. Esto es viabilizado por las correcciones de las secciones 5.5/5.6 arriba (bounds/pesos/ideal-anti-ideal fijos, no por consulta). **Pendiente para la disertación:** esta sección necesita ser reescrita para reflejar la decisión de implementación.

## 5.9 Explicabilidad

Después de calculado, el MOS va al módulo de IA, que recibe: indicadores normalizados, pesos calculados, resultado del TOPSIS, MOS final. Función única: generar explicación en lenguaje natural. La IA nunca modifica indicadores, pesos, cálculos o MOS — garantiza reproducibilidad total (misma entrada → mismo MOS siempre).

> **Adición 2026-07-08 (ver `marketmind_mos_engine`):** el insumo de la Fase 5 (aún no implementada) ahora incluye una capa de contexto determinística por indicador/dimensión/MOS — frase fija de significado de negocio (`business_context`, en inglés) + banda categórica (`interpretation_band`: Very Low/Low/Moderate/High/Very High; `mos_interpretation` para el MOS, las 6 bandas de 5.8) — ver `app/services/mos/interpretation.py`. Sigue siendo 100% determinístico, no generado por LLM; solo cambia qué tan rico es el insumo que el LLM recibe para narrar.

## 5.10 Salida del Core

Cada ejecución devuelve: indicadores normalizados, pesos calculados, resultado TOPSIS, MOS, estado de disponibilidad de los datos. Se mapea a la tabla `mos_results` (ya creada en la Fase 0): `entity_key, country, period_type, period_key, mos, mos_before_cap, capped_by, topsis_score, mos_interpretation, availability_status, excluded_reason, computed_at`. `mos_interpretation` (añadido 2026-07-08) es el rótulo fijo de 5.8 (ej. "Good Opportunity").

## 5.11 Conclusión

El MOS es el resultado final del proceso analítico de MarketMind: indicadores determinísticos (Cap. 4) → Entropy Weight determina la importancia automáticamente → TOPSIS identifica la proximidad al escenario ideal → MOS resume el potencial comercial en una puntuación 0-100. Proceso completamente determinístico, transparente y reproducible — la IA participa solo en la interpretación, nunca en el cálculo.
