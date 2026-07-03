# Próximos pasos

Estado actual: Fases 0-4 implementadas y probadas (catálogo, cobertura, motor de indicadores del Cap. 4, motor de
MOS del Cap. 5 con ROC+TOPSIS). Este documento organiza lo que falta, en orden de prioridad pensando en la defensa
del TFM — no es solo una lista suelta, es una recomendación de secuencia.

## Prioridad 1 — Necesario para tener una demo funcionando de punta a punta

### Fase 5 — Orquestación `/analyze` e identificación de producto
Hoy el usuario necesita llamar `/catalog/resolve`, `/indicators/compute`, `/mos/compute` manualmente, en secuencia,
por fuera. Falta el endpoint único que una demo de verdad necesita:
- Recibir texto/imagen/CSV describiendo el producto + lista de países.
- Resolver el producto (reaprovechando el catálogo ya listo).
- Verificar cobertura por país y decidir síncrono (todo ya cubierto → responde al instante) vs. asíncrono (falta
  dato → crea job, dispara recolección, SSE de progreso).
- Calcular indicadores + MOS para los países listos.
- Llamar al LLM solo para **explicar** el resultado en lenguaje natural — nunca para calcular nada.

### Enriquecimiento en segundo plano (idea validada en esta sesión, aún sin código)
Al responder una consulta, procesar solo los países pedidos con lo que ya existe (manteniendo el `check_or_collect`
de siempre para los países pedidos). En paralelo, un proceso separado busca datos de otros países para todos los
productos, enriqueciendo la base para consultas futuras. Dos requisitos ya acordados, no opcionales:
1. Cola única con límite de tasa **compartido entre productos** — nunca un job de recolección por consulta
   independiente, ya que reproduciría el mismo problema de rate-limit de Google Trends que ya vimos en vivo.
2. Reaprovechar la protección de condición de carrera que ya existe (`coverage_index` + `SELECT FOR UPDATE SKIP
   LOCKED`, Fase 1) — nunca pedir de nuevo lo que ya está fresco.

### Explicación vía LLM
Reglas de lenguaje ya definidas, falta implementar: nunca traducir "interés de búsqueda" en "demanda
comprobada"; siempre narrar dirección (Growth) junto con el nivel (Trend); narrar explícitamente cuando el techo
anticompensación redujo el MOS (`capped_by`/`mos_before_cap`); versionar modelo/prompt/timestamp de cada
explicación, por la misma razón por la que el resto del pipeline está versionado.

## Prioridad 2 — Necesario para defender el rigor del método

### Backtesting ligero
Mayor retorno posible para la defensa, aún no diseñado: validar el MOS contra 5-10 casos reales conocidos (producto
que tuvo éxito o fracaso comprobado entrando a un mercado específico). El rigor metodológico por sí solo no prueba
que el score correlaciona con algo real — esto sí lo prueba.

### Revisión de los Capítulos 4 y 5 de la disertación
Se hicieron varias correcciones en la implementación respecto al texto literal de los capítulos — necesitan
convertirse en texto revisado en la disertación, no solo en código:
- Cap. 5 §5.8 ("no existe un MOS absoluto") — reescribir para reflejar la decisión de MOS absoluto e independiente
  por país.
- Peso de las dimensiones: Rank Order Centroid, no Entropy Weight (cambio de método, no solo de parámetro).
- Techo anticompensación — no está en el capítulo original, necesita convertirse en sección metodológica propia.
- Normalización contra población de referencia fija, no por consulta.

### Precalentamiento de caché para el día de la defensa
Ejecutar los productos/países de la demo con antelación, garantizando que la presentación en vivo caiga siempre en
el camino síncrono rápido, sin depender de recolección en tiempo real (y sin riesgo de rate-limit en medio de la
defensa).

## Prioridad 3 — Robustez que vale la pena pero no bloquea la defensa

- **"MOS mundial"** (idea nueva de esta sesión, aditiva — no sustituye el MOS por país): una métrica combinando
  varios países, aún sin diseño técnico. Solo tiene sentido después de que el enriquecimiento en segundo plano ya
  esté funcionando (necesita cobertura amplia para ser útil).
- **Multicolinealidad dentro de cada dimensión:** GDP, Internet, Mobile, Urbanización se correlacionan fuertemente
  entre sí — el sub-score de cada dimensión (media simple) no percibe esa covarianza. Dirección: método tipo
  CRITIC/PCA en la agregación de indicador para sub-score (ya no es relevante como alternativa al peso entre
  dimensiones, que ya es ROC).
- **GDP per cápita en US$ corriente es ruidoso para el poder de compra real** (varía según el tipo de cambio).
  World Bank tiene versión PPP (`NY.GDP.PCAP.PP.CD`) — considerar cambiar o usar ambas.
- **El cero de Google Trends es ambiguo** (sin interés real vs. muestra insuficiente). Cruzar con presencia/ausencia
  en `related_queries`/`related_topics` como señal secundaria.
- **Bounds por percentil ya implementados (Cap. 4), Entropy Weight → ROC ya hecho (Cap. 5).** Los dos ítems que
  antes estaban en el roadmap de largo plazo ya fueron resueltos en esta sesión — ya no necesitan más trabajo
  futuro.

## Prioridad 4 — Evolución post-MVP, no relacionada con la defensa

- Webhooks en lugar de polling para el estado de recolección.
- Base de datos vectorial dedicada para reutilización de insights entre consultas.
- Migrar embedding del catálogo de OpenAI (solución provisional) a modelo local (`sentence-transformers`) —
  elimina costo por llamada y dependencia de API externa, pero exige nueva migration (la dimensión del vector
  cambia).
- Automatizar la cadencia mensual de recálculo de `normalization_reference` (hoy es bajo demanda vía endpoint
  admin, no un cron).
- Extender la tabla `countries` más allá de los ~27 actuales para cobertura ISO 3166 completa.

## Pendientes que necesitan decisión del usuario, no son solo trabajo técnico

- **Threshold de embedding del catálogo (hoy 0,5):** ajustado empíricamente con pocos ejemplos sintéticos — vale
  la pena revisar con dato real de producción antes de considerarlo definitivo.
- **Tamaño de la población de referencia (~27 países):** aceptado como suficiente para el MVP, pero es una
  elección consciente de alcance, no una validación estadística formal.
- **Sesgo estructural contra mercados emergentes** (la regla de exclusión penaliza países con cobertura de datos
  más débil): no se puede resolver sin violar "sin imputación" — necesita convertirse en limitación de alcance
  explícita, declarada en la disertación, no solo en la memoria del proyecto.
