# Transparencia técnica — decisiones y limitaciones que no pueden quedar ocultas

Este documento reúne, en un solo lugar, los mecanismos y decisiones del MarketMind Core que necesitan ser explicables en la
defensa del TFM — cosas que un lector escéptico (o un miembro del tribunal) va a preguntar "¿cómo funciona esto de verdad?"
o "¿pensaron en este caso?". Cada sección dice **qué es**, **por qué existe** y **cuál es la limitación conocida**.

## 1. Unificación de nombres de producto — pipeline de resolución del catálogo

**El problema:** un usuario puede describir el mismo producto de formas completamente diferentes — "air fryer",
"freidora de aire", "airfryer", "fritadeira sem óleo" — y el sistema necesita saber que es la misma entidad, sin
crear un registro duplicado cada vez que cambie la redacción.

**Cómo funciona — 4 pasos, del más barato al más caro, deteniéndose en el primero que decida:**

1. **Alias exacto:** normaliza el texto (minúsculas, sin acento/puntuación) y busca un alias ya registrado
   idéntico. Más barato, más preciso. La normalización **preserva escrituras no latinas** desde el 25/08/2026
   (§1.6).
2. **Trigram (`pg_trgm`) arbitrado por evidencia negativa:** similitud de subcadenas de 3 caracteres contra los
   alias existentes (threshold `CATALOG_TRIGRAM_SIMILARITY_THRESHOLD=0.4`), pero una entidad sólo es aceptada si el
   texto está **más cerca de lo que ella declara SER que de lo que declara NO SER** (§1.2). Empate o inversión
   descalifica la entidad y el caso sube al paso 3.
3. **Un modelo de lenguaje decide** entre las entidades candidatas del catálogo (§1.3). Sustituyó al paso de
   embedding el 14/08/2026.
4. **Crear entidad nueva:** si nada decidió, crea un registro nuevo — `entity_key` siempre es un UUID generado
   localmente, nunca depende de que ninguna API externa responda para existir.

Los `search_terms` que el LLM de enriquecimiento produce por mercado se indexan como alias (`_index_localized_terms`).
Eso es lo que le da alcance multilingüe al paso 2: el sistema ya poseía el vocabulario traducido, pero vivía sólo en
el JSONB `localized_markets` mientras el trigram busca en `product_aliases`. Medido contra 89 términos reales del
catálogo (14/08/2026): 71 resuelven en el paso 2 al threshold configurado, y los 71 son correctos.

### 1.1 El caso que decide el diseño: freidora de aire vs. freidora de aceite

Dos productos **opuestos** comparten casi toda la forma de la frase — la palabra que los distingue es minúscula
("aire"/"aceite", "sem"/"de"). Ninguna medida de semejanza los separa, ni de caracteres ni de vectores. Medido contra
el catálogo real el 25/08/2026 (9 entidades, 286 alias normalizados, 102 términos excluidos), con el threshold 0.4:

| similitud trigram | consulta | resolvía a | vía el alias |
|---|---|---|---|
| 0.765 | `öl fritteuse` | air fryer | `fritteuse ohne öl` |
| 0.750 | `friteuse huile` | air fryer | `friteuse sans huile` |
| 0.727 | `fritadeira a óleo` | air fryer | `fritadeira sem óleo` |
| 0.696 | `fritadeira de óleo` | air fryer | `fritadeira sem óleo` |
| 0.696 | `freidora de aceite` | air fryer | `freidora sin aceite` |
| 0.667 | `oil fryer` | air fryer | `oil less fryer` |

**44 de los 102 términos que el propio catálogo declara como "esto NO es este producto" (43,1%) resolvían a la
entidad que los excluye**, y grababan un alias exacto permanente. Es el error caro del sistema: fusionar dos
productos mezcla `localized_markets` y la demanda de ambos, de forma silenciosa y sin que ningún paso posterior lo
note.

Por qué la medición anterior no lo vio: los 89 términos evaluados el 14/08 eran **todos positivos**. La "precisión
del 100%" medía sólo la mitad del problema. El conjunto negativo existía desde la Fase 5 — los `excluded_terms`
curados por mercado — y no se usaba ni como guarda ni como test.

### 1.2 La solución: comparar las dos evidencias, no ajustar el umbral

El catálogo ya declara, por mercado, qué términos significan **otro** producto (`excluded_terms`, usados hasta
entonces sólo para filtrar volumen de búsqueda). Esos términos ahora se indexan como evidencia negativa
(`product_excluded_terms`, migration 026) y entran en la misma decisión:

```
'fritadeira de óleo'  ->  mejor search_term  ('fritadeira sem óleo')   0.696
'fritadeira de óleo'  ->  mejor excluded_term('fritadeira de óleo')    1.000   <- gana
```

**No introduce ningún umbral nuevo que calibrar**: es una comparación relativa entre las dos evidencias, y degrada
al comportamiento anterior cuando el catálogo no tiene términos excluidos. Un término excluido **nunca** es puerta de
entrada: no resuelve nada por sí solo, sólo descalifica candidatos.

**La comparación es global, no por entidad.** El primer diseño comparaba el texto sólo contra los términos excluidos
de la entidad que había coincidido. Un caso real lo tumbó: `freidora de aceite` es término excluido de la air fryer,
pero coincidía con la entidad "olive oil" a 0.400 — vía `aceite de oliva`, exactamente en el umbral — y el veto no
lo veía, porque quien excluye el término no era quien coincidió. Si el catálogo ya declaró que ese texto designa
otro producto, aceptar un parecido más débil con una tercera entidad es el error caro. Como el máximo global es
siempre ≥ el de la propia entidad, la regla global subsume a la anterior y queda una sola comparación.

Resultado medido sobre el mismo corpus. La tercera columna cuenta los términos excluidos que coincidieron con una
entidad **distinta** de la que los excluye — no siempre es error (`panela de pressão elétrica` es excluida por la
air fryer y es el producto legítimo de otra entidad), pero es donde vivía el caso del aceite:

| | recall positivo | fusiones | → 3ª entidad |
|---|---|---|---|
| sin veto (comportamiento anterior) | 284/284 (100,0%) | 44/102 (43,1%) | 3 |
| **con veto (implementado)** | 283/284 (99,6%) | **0/102 (0,0%)** | **0** |
| veto sólo por entidad (descartado) | 284/284 (100,0%) | 0/102 (0,0%) | 4 |
| sin veto, consulta inédita (alias idéntico removido) | 269/284 (94,7%) | 44/102 (43,1%) | 3 |
| con veto, consulta inédita | 261/284 (91,9%) | **17/102 (16,7%)** | 2 |

El coste del alcance global es de 1,3 puntos de escalado: 26,7% de las consultas suben al paso 3, contra 25,4% con
la variante por entidad. Se paga con gusto — es la misma lógica de coste asimétrico de §1.4: una llamada extra al
LLM es barata, una fusión silenciosa no.

Se probaron márgenes de holgura en el veto (0.05 a 0.20) y empeoran el trade: sobre la consulta inédita, un margen
de 0.15 baja las fusiones del 17,6% al 9,8% pero hunde el recall del 93,0% al 77,1%. El punto de operación es
margen 0 — la regla simple, sin ningún número que calibrar.

**Estas cifras son reproducibles**, no de una sesión perdida: `python scripts/benchmark_catalog_resolution.py`
las recalcula contra el catálogo vivo, en sólo-lectura, usando el `similarity()` real del pg_trgm. Los 102
términos excluidos son los 103 curados menos uno descartado por contradicción (§1.9). "Consulta inédita" elimina,
por consulta, el término idéntico de los dos lados — sin eso la medición sólo pregunta si el índice encuentra lo
que él mismo contiene, que es exactamente la falla de método de la calibración original.

**Vetar no es decidir.** La entidad vetada no se descarta: el caso sube al paso 3 con esa entidad garantizada en la
lista de candidatos, y con sus términos excluidos incluidos en el prompt. Lo que la forma de la palabra no puede
decidir, lo decide algo que lee las palabras.

### 1.3 Por qué un LLM y no un vector (paso 3)

Los ~20% de consultas que llegan al paso 3 comparten un rasgo: no tienen parentesco de escritura con nada indexado
(`odżywka bez spłukiwania`, `lockencreme`, `krulcrème`) o son sinónimo de dominio (`quadcopter` por drone). El
último es el que decide el diseño: `quadcopter` ≡ `drone` es conocimiento del mundo, no proximidad en ningún
espacio.

El paso de embedding fue medido y retirado el 14/08/2026. Contra los datos del propio catálogo, con cinco modelos
(`multilingual-e5-small`, `-base`, `BGE-M3`, `LaBSE`, `paraphrase-MiniLM`), **ningún threshold de distancia separa
"mismo producto" de "producto vecino"**, porque el coseno los ordena al revés:

```
freidora de aire             <-> freidora de aceite     0.044   DIFERENTES
krem do loków                <-> krem prostujący        0.062   DIFERENTES
crema de tratamiento capilar <-> lockencreme            0.188   IGUALES
```

En el punto de corte necesario para capturar paráfrasis reales entre idiomas, el 100% de los `excluded_terms`
curados también era aceptado. El problema no es el proveedor ni el número: es que el token que decide el producto es
justamente el que el vector no carga. Cambiar OpenAI por un modelo local (`sentence-transformers`) cambia quién
genera el vector, no ese hecho — y la implementación local, probada el 13/08/2026, llevó la imagen de 1,67GB a
más de 20GB por arrastrar torch. Ambas cosas se revirtieron. Si un vector vuelve algún día, es como *recall* (elegir
qué candidatos entran en el prompt cuando el catálogo crezca), nunca como criterio de aceptación, y con runtime ONNX,
no torch.

**El vector volvió el 13/10/2026 (migration 052), exactamente en ese papel.** La lista de candidatas que el LLM
juzga era "misma categoría, más recientes primero", que sólo contiene la entidad correcta mientras el catálogo entero
cabe en el tope de 25. Ahora la encabezan los vecinos semánticos de la consulta (paso 3a): un embedding por alias
(Voyage AI `voyage-4`, 1024 dimensiones, `input_type` distinto para consulta y documento), índice **HNSW** del
pgvector con distancia coseno, y agrupación por entidad con la mejor distancia de sus términos. Ninguna distancia se
compara con umbral: el vector **recupera**, el LLM **decide**. Sin clave o con Voyage caída, la selección vuelve a
la anterior. `scripts/benchmark_semantic_recall.py` mide recall@k y latencia del HNSW contra la búsqueda exacta
(fuerza bruta) para varios `ef_search`, en el catálogo real y en uno sintético mayor.

#### 1.3.1 El veto por producto antagónico: de la distancia absoluta al contraste

**El problema, medido.** Con `voyage-4`, `fritadeira a ar` y `fritadeira a óleo` distan 0,065; `fritadeira a ar` y
`air fryer` distan 0,106. El producto OPUESTO está más cerca que el MISMO producto en otro idioma. Ni un modelo mayor
(`voyage-4-large`) ni un reranker cross-encoder (`rerank-2.5`, con y sin instrucción) invierten ese orden: separan
mejor, pero sigue sin existir un umbral que acepte `freidora de aire` ≈ `heißluftfritteuse` sin aceptar también
`freidora sin aceite` ≈ `freidora con aceite`. Un embedding responde "¿habla de lo mismo?", y la fritadora de aceite
habla de lo mismo. La pregunta del catálogo es otra: "¿es el mismo producto?", y la decide UN atributo.

**La idea.** Si un umbral sobre la distancia al producto no separa, se compara contra algo más: lo que el catálogo
declara que el producto NO es. Cada producto lleva, generado en la misma llamada de enriquecimiento que ya produce su
vocabulario (sin llamada adicional), sus **productos antagónicos**: el mismo producto con UN atributo definidor
invertido — aire × aceite, con × sin cafeína, inalámbrico × con cable. Son *pares mínimos*, en el sentido de los
*contrast sets* de Gardner et al. (2020). La regla es RELATIVA y sin umbral: la consulta se archiva del lado al que
está más cerca. Es la misma regla que ya funcionaba en el paso 2 con trigramas (44/102 fusiones → 0), transportada
al espacio de embeddings. Vetar no decide: manda al LLM con la evidencia escrita ("más cerca de `fritteuse mit öl`,
que esta entrada declara no ser, que de cualquier término propio").

**Dos conjuntos de evaluación** (`evaluation/`): 180 consultas sintéticas escritas a mano para 15 productos en 6
idiomas, tres de los cuales (fr, it, pl) no existen en el catálogo evaluado; y 97 búsquedas REALES de usuarios de la
tabla `keyword_related` del colector Google Ads (6 productos, mercados US/BR/DE/IN/GB/JP), etiquetadas a mano. Cada
consulta es `same` (es el producto) o `antagonist` (es el opuesto). El catálogo se construye con el pipeline real
(`CatalogService.resolve`, vocabulario generado por `claude-opus-5`, vectores de Voyage).

**Ablación de la regla de decisión** (`evaluation/veto_rules.py`, mismos vectores, sin LLM). Cinco reglas: 1-NN
relativo, k-NN relativo (k=3), centroide más cercano (Rocchio como clasificador, Manning et al. IIR §14.2),
proyección sobre el eje de Rocchio μ_producto − μ_antagónico, y LDA de Fisher con *shrinkage*. Y dos generaciones
del conjunto de contraste: v1, lista libre de "confundibles"; v2, pares mínimos sobre UN atributo nombrado.

| Regla | Sint. v1 | Sint. v2 | Real v1 | Real v2 |
|---|---|---|---|---|
| mejor umbral ABSOLUTO posible (con fuga del gabarito) | 87,8 % | — | 80,4 % | 81,4 % |
| 1-NN relativo | 95,6 % | 93,9 % | 94,8 % | 93,8 % |
| k-NN relativo (k=3) | 95,6 % | **95,0 %** | **97,9 %** | 93,8 % |
| centroide | 83,9 % | 95,0 % | 89,7 % | 89,7 % |
| eje de Rocchio | 90,0 % | 95,0 % | 90,7 % | 88,7 % |
| LDA (shrinkage) | 90,0 % | 94,4 % | 95,9 % | **95,9 %** |

Tres lecturas. (1) Cualquier regla relativa supera al mejor umbral absoluto por 8–15 puntos: el contraste aporta lo
que la distancia sola no tiene. (2) Los métodos de centroide EXIGEN un conjunto de contraste limpio: con la lista
libre (v1) el LLM mezclaba vecinos con opuestos (`toaster oven` y `pressure cooker` como antagónicos de la air fryer;
`oat milk` como antagónico de la leche sin lactosa, que también carece de lactosa) y el centroide de los
"antagónicos" caía junto al producto; con pares mínimos (v2) suben 5–11 puntos. El diagnóstico *leave-one-out* sobre
el eje (`pureza_leave_one_out`) lo cuantifica: en v2 casi ningún antagónico proyecta del lado del producto. Los
vecinos más cercanos, en cambio, toleran un antagónico erróneo — uno lejos de la consulta simplemente no cuenta — y
por eso 1-NN/k-NN apenas cambian entre v1 y v2. (3) Forzar UN atributo cuesta cobertura: para `espresso machine` el
LLM eligió "presión × filtro" y dejó fuera `moka pot` y las cápsulas; para `curl cream`, "crema × gel", y dejó fuera
la crema alisadora. Un producto tiene VARIOS atributos definidores, y cada uno genera su familia de pares mínimos.
La formulación que se adopta (v3) es la unión de los pares mínimos sobre cada atributo nombrado
(`contrast_attributes`, 1–3 por producto): la cobertura de v1 con la pureza de v2. Para `espresso machine` el LLM
nombra tres ('pump pressure vs drip filter', 'pump pressure vs stovetop moka', 'machine vs capsule system'); para
`scotch whisky`, el origen contra Irlanda, Japón y EE. UU.

| Regla | Sint. v3 | Real v3 |
|---|---|---|
| 1-NN relativo | 96,7 % | 94,8 % |
| **k-NN relativo (k=3)** | 96,1 % | **96,9 %** |
| centroide | 96,7 % | 90,7 % |
| eje de Rocchio | 94,4 % | 90,7 % |
| LDA (shrinkage) | 96,7 % | 95,9 % |

Con v3 las cinco reglas convergen en el sintético (94–97 %) y k-NN es la mejor en las búsquedas reales. Es la regla
de producción (`ProductRepository.polarity`): robusta a un antagónico erróneo, sin hiperparámetro de distancia, y
O(k) por entidad. LDA la iguala pero exige una matriz 1024×1024 por entidad; el eje de Rocchio, aunque es la idea
más elegante (la resta cancela lo que ambos lados comparten y deja la dirección del atributo), pierde en las
búsquedas reales porque un producto con varios atributos definidores no tiene UN eje.

**Clasificación selectiva.** El módulo del escore es una margen; por debajo de una banda de abstención la consulta
es ambigua y va al LLM en vez de aceptarla el paso 2 por forma (Geifman & El-Yaniv, 2017: precisión × cobertura).
La banda se calibra en el conjunto sintético y se valida en el real, nunca al revés: en el sintético (v3, k-NN) el
codo de la curva está en t = 0,033 — cobertura 82 %, 99,3 % de acierto en lo decidido; a partir de ahí la precisión
se estanca y sólo cae la cobertura. Se fija t = 0,035 (`CATALOG_SEMANTIC_ABSTENTION_MARGIN`).
Aplicada FIJA a las búsquedas reales: cubre el 83,5 % y acierta el 100 % de lo que decide.

**De extremo a extremo**, con el LLM real decidiendo el paso 3 y el catálogo reconstruido para cada modo
(`--e2e`). Lo que importa es la FUSIÓN ERRÓNEA: el antagónico archivado como el producto, el error silencioso y
permanente. "Acierto perdido" es el error barato (duplicado o subtipo), y en las búsquedas reales son cinco casos
que el LLM declara subtipo (`compact espresso machine`), lectura defendible frente a la etiqueta manual.

| | Reales (97) | Sintéticas (180) |
|---|---|---|
| fusiones erróneas sin veto | 7/40 | 9/90 |
| **fusiones erróneas con veto + banda** | **0/40** | **0/90** |
| producto reconocido | 52/57 (igual) | 89/90 (igual) |
| llamadas al LLM | 47 → 53 | 114 → 124 |

El coste es el 5–10 % de llamadas adicionales al LLM — los casos que la banda declara ambiguos — y una llamada de
embedding (~0,2 s) en las consultas que el trigrama aceptaría. Ninguna llamada NUEVA al modelo: los antagónicos
salen de la misma llamada de enriquecimiento, y cuando el paso 3 acepta una consulta en un idioma que el catálogo no
tenía, devuelve en la misma respuesta los antagónicos en ese idioma (sin eso, 3 de las 4 fusiones que quedaban eran
alias aprendidos sin contrapeso: `écouteurs filaires` contra el alias recién aprendido `écouteurs sans fil`).

**Limitaciones, para decirlas antes de que las pregunten.** Las etiquetas de las búsquedas reales son de una
persona; 277 consultas y 21 productos son una muestra pequeña, con intervalos de ±3–5 puntos; los antagónicos
dependen de que el LLM de enriquecimiento nombre bien los atributos (el diagnóstico *leave-one-out* señala las
listas sospechosas, no las corrige); y el prefijo `e-tretroller` × `tretroller` sigue siendo el caso que ni forma ni
significado separan — cae en la banda y lo decide el LLM.

### 1.4 Coste asimétrico del error — lo que el diseño optimiza

Los dos errores posibles no cuestan lo mismo:

- decir "es el mismo" por error **fusiona** dos productos en una entidad. Destructivo y silencioso.
- decir "es diferente" por error crea una entidad duplicada. Recuperable: como mucho se recolecta demanda de un
  producto ya cubierto.

Por eso, en el paso 3, una abstención del modelo, una `entity_key` alucinada y un proveedor caído resuelven todos a
"sin correspondencia" — nunca a "el candidato más parecido". Y por eso el veto del paso 2 prefiere escalar antes que
aceptar.

### 1.5 Escalabilidad del paso 2 — por qué el operador importa más que el índice

El índice GIN trigram sobre `product_aliases` existe desde la migration 004, y hasta el 25/08/2026 **no se usaba
nunca**. El filtro era `similarity(alias, texto) >= threshold`, y ninguna forma de índice atiende a esa expresión:
Postgres calculaba la similitud fila por fila, en cada consulta. Con 9 productos es invisible; la forma es
O(aliases) y sólo aparece cuando el catálogo crece.

Medido con catálogo sintético de 2.000 productos, 500.000 alias y 16.000 términos excluidos
(`scripts/benchmark_resolution_latency.py`):

| | por resolución |
|---|---|
| `similarity(...) >= threshold` (forma anterior, sin veto) | 268 ms |
| lo mismo, ya con el veto de evidencia negativa | 300 ms |
| operador `%` + veto (forma actual) | **~2 ms** |

El cambio es el operador `%`, que sí usa el índice GIN. `%` compara contra `pg_trgm.similarity_threshold`, un GUC
de sesión — se fija **por transacción** (`set_config(..., is_local => true)`), nunca por sesión, porque las
conexiones vienen de un pool y un `SET` de sesión filtraría el umbral de este paso al resto de la aplicación.

Filtrar también el lado negativo con `%` es exacto, no una aproximación: vetar exige `sim_negativa >= sim_positiva`,
y una candidata sólo existe con `sim_positiva >= threshold`, así que ningún término excluido por debajo del umbral
podría vetar nada. El lado negativo además se consulta **correlacionado** — sólo las entidades que ya son candidatas
lo pagan, por index scan — en vez de agregar la tabla entera de términos excluidos en cada resolución.

Dos cosas más del mismo orden: la indexación de vocabulario pasó de un `INSERT` por término (≈286 viajes al banco
para un producto de 27 mercados) a un `INSERT ... ON CONFLICT DO NOTHING` por lado, lo que además elimina el
`IntegrityError` que dos resoluciones simultáneas del mismo alias producían; y el backfill recorre el catálogo
paginado por clave, con commit por lote, para no sostener locks sobre `product_aliases` — la tabla del camino
caliente de cualquier resolución — durante toda la pasada.

### 1.6 La normalización borraba las escrituras no latinas — y eso fusionaba productos

`normalize_alias` eliminaba todo lo que estuviera fuera de `[a-z0-9]`, así que un texto íntegramente en japonés,
chino, coreano, ruso o árabe normalizaba a la cadena **vacía** — y `resolve()` guardaba ese vacío como alias de la
entidad creada. Reproducido:

```
ノンフライヤー   (air fryer, japonés)   -> created  entity=161b52ba
空气炸锅        (air fryer, chino)     -> alias    entity=161b52ba
Пылесос-робот  (aspiradora, ruso)     -> alias    entity=161b52ba   ← producto sin relación
```

Una aspiradora rusa se volvía una air fryer japonesa. Es la misma fusión silenciosa que el veto existe para
impedir, entrando por la puerta que el veto no vigila: `alias` es el paso 1, el camino O(1), donde ni el trigram ni
la evidencia negativa llegan a ejecutarse.

El mismo defecto costaba alcance: **46 términos curados del catálogo, en 5 mercados (JPN, CHN, KOR, RUS, SAU), no
tenían representación y nunca llegaban a ser alias** — el vocabulario existía en `localized_markets` y era
inalcanzable.

La regla pasó a ser `[\W_]`, la misma idea con conciencia de Unicode: sigue quitando puntuación, símbolos y emoji,
y preserva letra y dígito de cualquier escritura. Para texto latino el resultado es idéntico, salvo dos casos que
**antes perdían información**: `ß` y `ł` no tienen descomposición NFKD y se volvían espacio (`heißluftfritteuse` →
`hei luftfritteuse`). Un `NFC` final recompone lo que el `NFKD` separó, que importa para el coreano.

Tres capas, porque el estado corrupto es destructivo y silencioso: la causa corregida en el normalizador; el
servicio que se niega a guardar alias sin forma normalizada (un texto de sólo puntuación o emoji sigue
normalizando a vacío, y ese caso ahora pasa sin alias); y una `CHECK` en las dos tablas (migration 028) que vuelve
el estado imposible en vez de improbable.

### 1.7 La marca no decide identidad

La unidad de identidad del catálogo es el **producto genérico**: `air fryer` es una entidad, y Philips, Ninja y
Cosori son marcas suyas. El prompt de visión se lo dice al modelo con todas las letras — *"Put the brand only in
`brand`"* — y aun así el código volvía a pegar las dos cosas antes de resolver:

```python
resolve_text = f"{brand} {product_name}"   # hasta el 25/08/2026
```

La marca es un token que **todos** los productos de esa marca comparten: no distingue nada dentro de la marca y
acerca artificialmente cosas que sólo tienen la marca en común. Medido contra el catálogo real:

```
'air purifier'          vs 'air fryer'         0.278   (por debajo del umbral)
'philips air purifier'  vs 'philips airfryer'  0.520   ← pasaba
```

Verificado de punta a punta por el endpoint que usa el frontend: describir *"un purificador de aire Philips para
habitación, con filtro HEPA"* devolvía `method: trigram` → **air fryer**, y el daño era completo — 15 alias de
purificador apuntando a la air fryer, los `search_terms` de BRA contaminados con `purificador de ar Philips` y
`filtro HEPA Philips` (que habrían sumado su volumen de búsqueda al Demand Volume del producto equivocado), y la
categoría de la entidad sobrescrita de `Kitchen Appliances` a `Home Appliances`.

En 11 de 12 casos medidos la marca no cambiaba el resultado; en el 12º lo cambiaba a peor. Tampoco hacía falta para
encontrar la entidad: `Philips Airfryer` y `Ninja air fryer` ya son alias indexados, venidos de los `search_terms`
curados.

**La marca no se pierde: se acumula.** `product_catalog.brands` (migration 029) guarda cada marca vista para el
producto — se añade, nunca sustituye, igual que `localized_markets` — y cada marca nueva genera además un alias
`marca + nombre`, para que la siguiente consulta con marca resuelva en el paso 1. **Nunca** un alias con la marca
sola: un alias `philips` haría que cualquier producto Philips resolviera a esa entidad, por igualdad exacta, en el
paso más barato — que es exactamente el destrozo que la migration 028 y la poda del backfill limpiaron.

Es JSONB y no tabla propia, al contrario que `product_excluded_terms`: aquélla tiene que ser **consultable** por
`similarity()` con índice GIN porque participa del veto; la marca se lee junto con la entidad y nunca se busca sola.

### 1.8 La categoría separa lo que el nombre no puede

Accesorio, repuesto, consumible, servicio y contenido **de** un producto comparten el nombre de ese producto por
construcción, así que ninguna medida de parecido los separa. El caso extremo lo deja claro: `air fryer basket` (la
cesta) mide **1.000** contra el término curado `basket air fryer` (el aparato) — el `pg_trgm` compara bolsas de
trigramas e **ignora el orden de las palabras**, de modo que para él las dos cadenas son idénticas. Ningún umbral
alcanza eso, y el veto tampoco, porque nadie curó la cesta como término excluido.

Lo que sí los separa es la categoría, que la identificación ya devolvía y sólo se usaba para priorizar candidatos
del paso 3. Desde el 25/08/2026 una **categoría divergente descalifica** al candidato en el paso 2, exactamente
igual que la evidencia negativa: no decide que sean distintos, escala al paso 3 con esa entidad en la lista.

La guarda sólo actúa cuando **ambas** categorías son conocidas — que falte una no es evidencia de divergencia.

Medido sobre el conjunto adversarial, los seis falsos positivos que quedaban pasaron todos a escalar, sin perder
ni un caso de recuperación legítima (incluidos `air fryers` con y sin categoría, y las seis escrituras de air
fryer). Y de punta a punta, por el endpoint que usa el frontend, la batería pasó de 5/7 a **8/8**.

La guarda depende de que el modelo dé categorías distintas, y al principio no lo hacía: clasificó una cesta de
repuesto como `Kitchen Appliances`, la misma del aparato. El prompt de visión pasó a decirlo explícitamente — un
accesorio, repuesto, consumible, servicio o contenido recibe su **propia** categoría (`Kitchen Accessories`,
`Spare Parts`, `Cookbooks`, `Services`), nunca la del producto al que pertenece. Después de eso, la cesta, el
libro de recetas y el papel de horno se resuelven cada uno como entidad propia.

**Coste conocido:** categorías equivalentes escritas distinto cuestan un viaje al paso 3. Ocurrió en la propia
medición — el modelo devolvió `Kitchen Appliance` en singular una vez, contra `Kitchen Appliances`. Ahí no costó
nada porque el caso resolvió por alias exacto (paso 1, donde la guarda no actúa), pero en el paso 2 habría
escalado sin necesidad. Es el error barato, elegido a propósito.

**Lo que la categoría no resuelve:** `fritadeira` a secas mide 0.647 contra la air fryer y es `Kitchen Appliances`
de verdad — la categoría coincide y la guarda no tiene nada que decir. Es ambigüedad genuina del idioma (en
pt-BR el término solo suele designar la de aceite) y es exactamente el tipo de caso que el paso 3 existe para
juzgar.

### 1.9 Limitaciones conocidas

- **El veto sólo cubre la colisión que alguien curó.** Repetida la medición eliminando también el término excluido
  idéntico (para simular una colisión que nadie anotó), las fusiones caen de 43,1% a 16,7% — una reducción del 61%,
  no a cero. El residuo depende del paso 3. Un vecino no curado como `air fryer` vs. `deep fryer` mide 0.400,
  exactamente sobre el threshold. La vía para cerrarlo no es el umbral (ver abajo) sino la curación: que el
  enriquecimiento produzca `excluded_terms` para los vecinos de categoría, no sólo para los homónimos.
- **El umbral de trigram (0.4) se re-midió con los dos conjuntos** y se mantiene: subirlo a 0.45 baja las fusiones
  de 16,7% a 12,7% pero hunde el recall de 91,9% a 88,4%; bajarlo a 0.35 sube las fusiones a 20,6% sin ganar recall
  apreciable. Es la rodilla de la curva, ahora por medición de los dos lados y no sólo de los positivos.
- **El índice estaba dormido.** `_index_localized_terms` y `_index_excluded_terms` sólo corren dentro de
  `resolve()`, así que una entidad creada antes de esas features no tiene ninguno de los dos: medido en el banco de
  desarrollo el 25/08/2026, `product_aliases` tenía 9 filas (una por `canonical_title`) y ninguno de los 286
  términos localizados del catálogo. `POST /admin/catalog/backfill-terms` llena los dos lados **en la misma pasada**
  — llenar sólo los positivos encendería el alcance multilingüe junto con las 45 fusiones que la evidencia negativa
  existe para impedir.
- **El dato se contradice a sí mismo, al menos una vez.** `fritadeira elétrica` figura como `search_term` en BRA y
  como `excluded_term` en PRT/ESP del mismo producto. En ese empate gana el positivo (es evidencia de que el término
  *recupera* el producto) y la contradicción queda en el log. Falta un chequeo de consistencia en el
  enriquecimiento.
- El threshold de trigram (0.4) fue calibrado sólo contra positivos. Ahora que existe un conjunto negativo
  (`tests/test_catalog_excluded_terms.py` y los `excluded_terms` del catálogo), vale re-medirlo con los dos lados.

**Enriquecimiento (`google_mid`):** después de creada la entidad, un job best-effort (no bloquea la creación)
le pregunta a PyTrends por sugerencias de entidad del Google Knowledge Graph para ese texto, y guarda el `mid` si
encuentra un match determinístico bueno. Sirve para detectar duplicados más tarde (merge de entidades que solo después
descubrimos que son la misma).

## 2. Normalización absoluta — por qué el MOS no cambia dependiendo de quién más fue consultado

Cada indicador del Capítulo 4 (ej. PIB per cápita) se normaliza en una escala 0-100 usando límites (`min`/`max`)
calculados **una vez, contra una población de referencia fija** (~27 países, tabla `countries`), y no contra los
países de la consulta actual. Esto es una corrección deliberada sobre el texto literal del Capítulo 4/5 (que describe
normalización "sobre el conjunto de alternativas comparadas en la misma ejecución").

**Por qué:** sin esto, el mismo país podría tener scores diferentes dependiendo de qué otros países el usuario
decidió comparar en la misma consulta — lo que rompe cualquier posibilidad de comparar resultados entre ejecuciones
diferentes, o a lo largo del tiempo.

**Cómo se mantiene estable:** los bounds (`normalization_reference`) están versionados por `period_key` y **nunca se
sobrescriben después de que el período cierra** (`is_closed`).

> ⚠️ Corrección (25/08/2026): este documento decía que se recalculan bajo demanda vía
> `POST /admin/indicators/refresh-reference`. **Esa ruta ya no existe** en la API, y
> `app/services/reference_calculator_service.py` no tiene hoy ningún llamador en el código. En la práctica los
> bounds vigentes son los sembrados por las migrations (005/010/012/020/021/022/024) y no se recalculan solos ni
> bajo demanda. Es una laguna real, no un detalle de redacción: la cadencia mensual de recálculo sigue pendiente
> (ver `marketmind-proximos-passos.md`). Lo mismo vale para los pesos de las dimensiones (`indicator_weights`).

**Limitación conocida:** la población de referencia todavía es pequeña (24 países, la canasta soportada entera)
comparada con un ideal estadístico mayor — aceptado como suficiente para el MVP/TFM, no una solución definitiva de
largo plazo (ver known gaps).

### 2.1 Recalibración de las reglas de país (27/08/2026, migration 030)

Los bounds sembrados por las migrations eran, según el propio código, *"plausible starting points, not perfectly
researched limits"*: se eligieron cuando casi ningún país tenía datos macro, porque la población de referencia no
existía todavía. Esa premisa venció — los **24 países soportados hoy tienen los 15 indicadores macro completos**, así
que la población de referencia dejó de ser una muestra y pasó a ser la canasta entera.

Medidos contra ella, los bounds ocupaban entre el 32% y el 73% de su propia escala. El espacio muerto no es neutro:
comprime a todos los países en una franja estrecha del 0-100, y un indicador que **sí** separa mercados deja de
separarlos. El caso más claro es Población: un rango real de Polonia encogiendo 0,8%/año a Arabia Saudí creciendo
1,7%/año salía como scores de 27 a 59. Inversión era peor que plano — Sudáfrica (13,9%) caía **debajo** del piso de
15 y puntuaba 0, leyéndose como "no invierte nada".

Los nuevos bounds son el rango observado de la canasta, redondeado hacia afuera al número económicamente legible más
cercano — nunca ajustado apretadamente a los valores de hoy. Efecto medido en la dispersión (desviación estándar de
los scores de los 24 países), recomputando contra Postgres real:

| indicador | regla | dp antes | dp después |
|---|---|---|---|
| population_growth | -3..5 → **-1..2** | 6,9 | 17,5 |
| gdp_growth | -10..10 → **-2..8** | 8,7 | 17,3 |
| net_migration_rate | -15..15 → **-6..9** | 9,3 | 18,4 |
| mobile_connectivity | 0..200 → **70..190** | 13,5 | 22,4 |
| net_exports | -15..15 → **-8..12** | 11,1 | 16,9 |
| consumer_spending | 30..85 → **40..75** | 15,1 | 22,0 |
| gdp_per_capita | 1k..120k → **2k..95k** | 20,1 | 25,9 |
| investment | 15..45 → **12..35** | 19,0 | 21,5 |
| worldbank_inflation | 0..20 → **0..12** | 6,0 * | 9,9 * |

\* sólo los 22 países fuera de crisis. Argentina (142%) y Turquía (49%) siguen **saturando en 0**, que es la decisión
del 24/07/2026: un extremo satura en el peor score en vez de estirar la regla para todos.

**Fuera de esta recalibración, a propósito:** `trend_score`, `growth_score` y `demand_stability_score` (vienen de
Google Ads y dependen del producto, no del país), `population_scale`, `urbanization` e `internet_penetration` (ya
ocupan bien su escala o son passthrough), `trade_openness` (la saturación de Holanda es deliberada, migration 024) y
`real_estate_growth` (las dos colas son extremos reales).

**Lo que esta recalibración NO arregla, y quedó medido:** la franja del MOS casi no se mueve (36-43 sigue siendo
36-43), porque la comprime la dimensión Demand Volume — 38,6% del peso, con sub-score 100 en el país-techo y debajo
de 10 en 16 de 24 países. No es un artefacto de un producto de nicho: pasa igual con productos de masa (impresora
3D: EUA 100, segundo lugar 18,7). Es la regla por-producto del Google Ads, no una regla de país, y sigue abierta
como el ítem 17 de los known gaps.

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
