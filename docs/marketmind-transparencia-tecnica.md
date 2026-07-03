# Transparencia técnica — decisiones y limitaciones que no pueden quedar ocultas

Este documento reúne, en un solo lugar, los mecanismos y decisiones del MarketMind Core que necesitan ser explicables en la
defensa del TFM — cosas que un lector escéptico (o un miembro del tribunal) va a preguntar "¿cómo funciona esto de verdad?"
o "¿pensaron en este caso?". Cada sección dice **qué es**, **por qué existe** y **cuál es la limitación conocida**.

## 1. Unificación de nombres de producto — pipeline de resolución del catálogo

**El problema:** un usuario puede describir el mismo producto de formas completamente diferentes — "air fryer",
"freidora de aire", "airfryer", "fritadeira sem óleo" — y el sistema necesita saber que es la misma entidad, sin
crear un registro duplicado cada vez que cambie la redacción.

**Cómo funciona — 4 pasos, del más barato al más caro, deteniéndose en el primero que coincida:**

1. **Alias exacto:** normaliza el texto (minúsculas, sin acento/puntuación) y busca un alias ya registrado
   idéntico. Más barato, más preciso.
2. **Trigram (`pg_trgm`):** similitud de subcadenas de 3 caracteres contra los alias ya existentes. Captura
   errores de tipeo y pequeñas variaciones en el mismo idioma (threshold: `CATALOG_TRIGRAM_SIMILARITY_THRESHOLD=0.4`).
3. **Embedding semántico:** si los dos pasos anteriores no coincidieron, genera un vector de embedding del texto (vía
   OpenAI, `text-embedding-3-small`, 1536 dimensiones) y busca por distancia de coseno contra los embeddings ya
   guardados (`pgvector`, índice de similitud). Este es el único paso que captura paráfrasis sin superposición léxica
   (ej. "air fryer" ≈ "fritadeira sem óleo" en portugués) o traducción entre idiomas.
4. **Crear entidad nueva:** si nada coincidió, crea un registro nuevo — `entity_key` siempre es un UUID generado
   localmente, nunca depende de que ninguna API externa responda para existir.

**El threshold de embedding fue ajustado empíricamente, no es un número de manual.** Probado en vivo (2026-07-03):
con el valor inicial de 0,15 (recomendación común en ejemplos de `pgvector`), *ninguna* paráfrasis real coincidió —
"air fryer" vs. "freidora de aire caliente" midió 0,397 de distancia real. Pruebas sucesivas llevaron el valor a
**0,5** — en ese punto, una frase en portugués sin ninguna palabra en común con el catálogo ya existente
("fritadeira sem óleo a ar quente elétrica") encontró correctamente la entidad en italiano equivalente
(`method: "embedding"`, score 0,628). Trade-off consciente: cuanto más alto el threshold, más fácil capturar
paráfrasis distantes, pero mayor el riesgo de fusionar por error dos productos parecidos pero diferentes (ej. "air
fryer" vs. "grill eléctrico"). Vale la pena revisarlo con más datos reales de producción, no es definitivo.

**Enriquecimiento (`google_mid`):** después de creada la entidad, un job best-effort (no bloquea la creación)
le pregunta a PyTrends por sugerencias de entidad del Google Knowledge Graph para ese texto, y guarda el `mid` si
encuentra un match determinístico bueno. Sirve para detectar duplicados más tarde (merge de entidades que solo después
descubrimos que son la misma).

**Por qué OpenAI, y no otra cosa:** Anthropic no tiene endpoint de embeddings propio (la documentación oficial
de ellos recomienda Voyage AI) — OpenAI fue elegido como **stopgap explícito**, aislado detrás de una única clase
(`app/services/embedding_client.py`) específicamente para que ese cambio sea barato después. Plan ya registrado:
migrar a un modelo local (`sentence-transformers`) más adelante, lo que eliminaría el costo por llamada y la
dependencia de una API externa — pero exigiría una nueva migration (la dimensión del vector cambia) y reprocesar embeddings
existentes.

## 2. Normalización absoluta — por qué el MOS no cambia dependiendo de quién más fue consultado

Cada indicador del Capítulo 4 (ej. PIB per cápita) se normaliza en una escala 0-100 usando límites (`min`/`max`)
calculados **una vez, contra una población de referencia fija** (~27 países, tabla `countries`), y no contra los
países de la consulta actual. Esto es una corrección deliberada sobre el texto literal del Capítulo 4/5 (que describe
normalización "sobre el conjunto de alternativas comparadas en la misma ejecución").

**Por qué:** sin esto, el mismo país podría tener scores diferentes dependiendo de qué otros países el usuario
decidió comparar en la misma consulta — lo que rompe cualquier posibilidad de comparar resultados entre ejecuciones
diferentes, o a lo largo del tiempo.

**Cómo se mantiene estable:** los bounds (`normalization_reference`) se recalculan periódicamente (bajo demanda hoy,
`POST /admin/indicators/refresh-reference`), versionados por `period_key`, y **nunca se sobrescriben después de que el
período cierra** (`is_closed`). Lo mismo vale para los pesos de las dimensiones (`indicator_weights`).

**Limitación conocida:** la población de referencia todavía es pequeña (~27 países) comparada con un ideal estadístico
mayor — aceptado como suficiente para el MVP/TFM, no una solución definitiva de largo plazo (ver known gaps).

## 3. Agregación en 2 etapas + pesos por Rank Order Centroid (no Entropy Weight)

17 indicadores atómicos → 5 dimensiones (sub-score = media simple de los indicadores de esa dimensión) → los 5 pesos
de las dimensiones se combinan en un MOS único vía TOPSIS. Diagrama completo en `docs/marketmind-mos-flow-diagram.md`.

**Decisión que merece un destaque específico:** los pesos de las 5 dimensiones vienen de **Rank Order Centroid**, no de
Entropy Weight (el método literalmente descrito en el Capítulo 5). Motivo: probado en vivo con datos reales, Entropy
Weight subvaloraba Demand estructuralmente (2,6%-5% del peso total) por un problema conceptual del método (mide
variación estadística, no importancia causal) — documentado en detalle en
`docs/marketmind-implementation-plan.md` sección 8 ítem 2, y en la memoria de decisiones del proyecto. Esto es una
divergencia activa respecto del texto del capítulo, necesita aparecer en la disertación como corrección metodológica explícita, no
solo como detalle de implementación.

## 4. Reglas de fallback (nunca duplican la misma información)

- **Inflación — OECD primero, World Bank solo si falta:** nunca los dos cuentan al mismo tiempo para el mismo
  país/período (evita inflar artificialmente el peso de la señal de inflación).
- **Granularidad de producto — SKU específico, categoría como fallback:** si el Trend Score del término exacto del
  producto viene `unavailable` (pocas observaciones en Google Trends), el motor intenta de nuevo con la categoría del
  producto (`product_catalog.category`). Cuando esto sucede, los indicadores resultantes quedan marcados
  `availability_status=fallback` con una nota explícita — nunca queda oculto que el número vino de un término más
  genérico que el producto exacto.

## 5. "Sin imputación" — la regla de exclusión nunca es silenciosa

Si un indicador **obligatorio** no está disponible para un país (fallo de recolección, serie inexistente, etc.), el
país entero queda `UNAVAILABLE` — no recibe MOS, y el sistema **nunca inventa un valor medio o neutro** en su lugar.
`mos_results.excluded_reason` siempre explica el motivo en texto (qué indicador faltó). Esto es intencional aun
sabiendo que genera un sesgo estructural contra países con cobertura de datos más débil (normalmente mercados
emergentes) — declarado como limitación de alcance explícita, no oculta.

## 6. Techo anticompensación — decisión del usuario, no está en el Capítulo 5

Los indicadores macro (PIB, internet, población) por sí solos no pueden producir un MOS "excelente" si el Trend Score
(señal de demanda real) es bajo: `MOS_final = min(MOS_topsis, teto(TrendScore))`, con techo 39 si
`TrendScore < 20`, techo 59 si `20 ≤ TrendScore < 40`, sin techo por encima de eso. Necesita convertirse en sección explícita en
la disertación — es una regla de negocio determinística, documentada, pero **no viene del Capítulo 5 original**.

## 7. Acceso directo a la base de datos de los otros 4 servicios — acoplamiento aceptado conscientemente

El Core lee las tablas crudas de los 4 microservicios de recolección **directo vía SQL** (usuario `marketmind_readonly`,
solo lectura), no a través de la API HTTP de ellos. Decisión de simplicidad — evita reimplementar endpoints de
lectura que ya existen como tabla. **El costo real:** si uno de los 4 servicios cambia el nombre de una columna o tabla
sin avisar, el Core se rompe silenciosamente en la próxima lectura, sin ningún contrato formal entre los dos lados.
Mitigación parcial: las pruebas de indicador (`test_indicators_*.py`) fijan el nombre exacto de las columnas esperadas, entonces
un cambio de schema aparece como test en rojo, no como error en producción — pero solo si las pruebas corren después
del cambio en el otro repositorio, lo que no es automático hoy (son repositorios/equipos diferentes).

## 8. Limitación documentada de la OECD — dimensiones SDMX no totalmente filtrables

El Capítulo 4 pide filtrar la serie de inflación de la OECD por `EXPENDITURE=_T`, `TRANSFORMATION=GY`, `ADJUSTMENT=N`,
además de `MEASURE=CPI`. La tabla cruda de `marketmind-oecd` solo persiste `MEASURE`/`FREQ`/`UNIT` como columnas
queryable — las otras 3 dimensiones no se capturan en la ingesta (confirmado leyendo el código de ese servicio, no
asumido). En la práctica, el filtrado del Core hoy es solo `país + measure='CPI'`, confiando en que la recolección ya fue
delimitada correctamente en el momento en que corrió. Documentado en el código (`app/services/indicators/oecd.py`) — no una
suposición oculta.

## 9. Bug real de rendimiento/confiabilidad descubierto y corregido en los 3 microservicios de recolección (2026-07-03)

Al ejecutar una recolección masiva real (backfill de los 27 países de referencia) por primera vez, encontramos y
corregimos 3 bugs reales que ningún test automatizado aislado (cada servicio + mocks para los otros) detectaría:

1. **`marketmind-worldbank`:** cuando la recolección de un indicador específico fallaba (ej. blip de red), el servicio
   registraba la ejecución entera como **éxito** de todas formas — el Core creía que el dato estaba cubierto,
   permanentemente (hasta que expirara el TTL de 30 días), aun con cero datos reales. Corregido: la ejecución solo se marca
   como éxito si al menos un indicador realmente fue recolectado.
2. **`marketmind-oecd`:** el endpoint de recolección por indicador nunca delimitaba la consulta por país — siempre descargaba
   el dataflow entero de la OCDE (todos los países, todas las series) y filtraba después, en Python. Una consulta que
   debería tardar segundos podía tardar más de una hora o fallar (conexión caída en medio de la descarga). Corregido
   construyendo la clave SDMX posicional correcta (`REF_AREA` + `MEASURE` fijos, el resto libre) — de "más de 1 hora o
   fallo" a ~5 segundos por país.
3. **`marketmind-core`:** el parsing de la respuesta de recolección de UN Data siempre miraba `metadata.ingestion_run_id`,
   pero ese endpoint específico nunca completa ese campo (puede generar más de una ejecución por llamada) — el
   ID real viene en `data.ingestion_run_ids` (lista). Resultado: toda recolección de UN Data por el Core **siempre fallaba**
   hasta ese día, silenciosamente, sin que ningún test lo detectara porque el mock nunca reproducía la respuesta real del
   servicio.

Los 3 solo aparecieron probando los 5 servicios corriendo juntos, de verdad, con volumen real de llamadas — refuerza que
los tests unitarios con mock no sustituyen al menos un test de integración real por fase.

## 10. Validación cruzada contra biblioteca de mercado (no decide nada, solo compara)

Cada cálculo de MOS también corre, en paralelo, TOPSIS + Entropy Weight **clásicos** vía `pymcdm`
(`app/services/mos/library_reference.py`), guardados en `mos_results.library_topsis_score`/`library_mos` solo para
comparación manual — nunca influye en el MOS real del producto. Existe específicamente para la defensa: muestra que la
implementación propia fue comparada contra el método estándar de mercado, y documenta por qué diverge (ideal/anti-ideal
fijos vs. relativos a la población comparada — ver sección 2 arriba).
