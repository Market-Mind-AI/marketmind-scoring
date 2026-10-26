"""Fase 5 — final LLM inference. Assembles a deterministic "evidence packet"
from the computed MOS + indicator values and asks the resolved `explanation`
LLM (provider/model from .env — default ollama / gemma4:12b, see
app/services/llm/resolver.py) to turn it into a business-facing narrative of
the market-opportunity ranking ("where to sell"). The prompt lives HERE (in the
core) so it is provider-agnostic — switching to claude/gpt asks the exact same
thing.

Best-effort: any failure (provider down, timeout, LLM error) returns None and is
logged — the analysis still completes with the numbers, just without prose. The
LLM only EXPLAINS these deterministic values; it never computes or alters a
score (Cap. 4 §4.0, memory marketmind_mos_engine).
"""

import json
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.models.mos_result import MosResult
from app.domain.enums import IndicatorCode, IndicatorDimension
from app.prompts import prompt_text
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.metric_explanation_repository import MetricExplanationRepository
from app.repositories.mos_repository import MosRepository
from app.services.llm import resolve_explanation_llm
from app.services.llm.base import joined_system
from app.services.mos.dimension_mapping import DIMENSION_BY_INDICATOR, compute_dimension_subscores
from app.services.mos.explanation_builder import ExplanationCatalog
from app.services.mos.interpretation import score_band

# Every indicator the packet may carry, in a stable narrative order: the four
# scored demand signals and the macro/readiness block (DIMENSION_BY_INDICATOR),
# then the two context-only ones (self-scaled / direction-neutral, never scored
# into the MOS but useful colour for the narrative).
_PACKET_INDICATORS: list[IndicatorCode] = [
    *DIMENSION_BY_INDICATOR,
    IndicatorCode.REGIONAL_DEMAND_SCORE,
    IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
    IndicatorCode.CATEGORY_VOLUME_SCORE,
]

# --- demand-confidence guard (automated stand-in for human term curation) ---
# The one failure the Google-volume net can't catch is a MISSING native search
# term (e.g. Japan without ノンフライヤー): a real term simply isn't proposed, so
# volume is undercounted with no signal. Its tell-tale is a demand penetration
# (searches per 100k inhabitants) that is a gross LOW outlier vs the peer
# markets. We read the penetration the indicator engine already wrote into the
# Demand Volume signal's context and flag such countries as low-confidence.
_PENETRATION_RE = re.compile(r"penetration\s+([\d.]+)\s+searches per 100k", re.IGNORECASE)
_DEMAND_OUTLIER_RATIO = 0.2  # < 20% of the peer median penetration = suspicious
_DEMAND_OUTLIER_MIN_PEERS = 3  # need enough peers for a meaningful median
# A low penetration is only SUSPICIOUS (likely a missing native term) in a
# developed, connected market — a rich country with near-zero demand is wrong.
# In a low-income market, low per-capita demand is expected, not an artifact, so
# don't false-flag it. gdp_per_capita_score >= 15 ≈ GDP/cap above ~$18.8k on the
# [1000, 120000] ruler (see migration 021).
_DEVELOPED_GDP_MIN = 15.0


def _signal_score(country: dict[str, Any], code: str) -> float | None:
    for signal in country.get("signals") or []:
        if signal.get("code") == code:
            return signal.get("score")
    return None


def _penetration_of(country: dict[str, Any]) -> float | None:
    context = None
    for signal in country.get("signals") or []:
        if signal.get("code") == IndicatorCode.TREND_SCORE.value and signal.get("context"):
            context = str(signal["context"])
            break
    if not context:
        return None
    match = _PENETRATION_RE.search(context)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _flag_demand_outliers(countries: list[dict[str, Any]]) -> None:
    """Set `demand_confidence` ('ok'|'low') on each country. 'low' means the
    demand volume is likely UNDERCOUNTED (a missing native search term) — flagged
    when a DEVELOPED market's penetration is a gross low outlier vs the peer
    median. Mutates in place. Purely a warning; never changes any score."""
    for country in countries:
        country["demand_confidence"] = "ok"
    pens = {c["country"]: _penetration_of(c) for c in countries}
    valid = sorted(p for p in pens.values() if p is not None and p > 0)
    if len(valid) < _DEMAND_OUTLIER_MIN_PEERS:
        return
    mid = len(valid) // 2
    median = valid[mid] if len(valid) % 2 else (valid[mid - 1] + valid[mid]) / 2
    if median <= 0:
        return
    for country in countries:
        pen = pens.get(country["country"])
        if pen is None or pen >= median * _DEMAND_OUTLIER_RATIO:
            continue
        gdp = _signal_score(country, IndicatorCode.GDP_PER_CAPITA_SCORE.value)
        if gdp is not None and gdp < _DEVELOPED_GDP_MIN:
            continue  # low-income market → low demand is expected, not an artifact
        country["demand_confidence"] = "low"
        country["demand_confidence_note"] = (
            f"Penetração de {pen:.0f} buscas/100k é um outlier baixo frente à mediana dos mercados "
            f"({median:.0f}/100k) num mercado desenvolvido — possível subcontagem por termo de busca "
            "nativo faltante aqui."
        )

_SYSTEM_PROMPT_PT = """Você é um analista de entrada em mercados (market-entry) que ajuda um negócio \
a {{scope_intro}}, a partir de um "pacote de evidências" já calculado \
pelo motor determinístico de MOS (Market Opportunity Score) da MarketMind.

REGRAS INVIOLÁVEIS:
- Use SOMENTE os números fornecidos no pacote. NUNCA invente, estime, arredonde de forma nova \
ou recalcule qualquer valor. Não escreva nenhum número que não esteja no input.
- Você EXPLICA os resultados determinísticos; você não os recalcula nem os contesta.
- {{country_rule}}

O PACOTE traz, para CADA país: o MOS (com banda e um campo `explanation` em texto), a lista de \
`dimensions` e a lista de `signals` (indicadores). CADA item — MOS, dimensão e sinal — tem um campo \
`explanation`: uma leitura em linguagem natural do NÚMERO daquele score (o que aquele nível significa). \
Cada país traz também `context_signals`: números SEM score, que não são bons nem ruins por si — use-os para \
dar escala, nunca como elogio ou crítica. O mais importante deles é a demanda da CATEGORIA (termos \
GENÉRICOS, não a demanda total do mercado), comparada com a da marca: uma fração pequena de uma categoria \
grande significa que o mercado existe e a marca não está nele, o que é diferente de "não há mercado aqui". \
A marca PODE ser maior que os termos genéricos, e aí a leitura é que ela é procurada por nome — nunca \
apresente isso como "share" acima de 100%. \
Há ainda um `glossary` no topo definindo o que cada métrica mede. USE o `explanation` de cada score e o \
`glossary` para construir uma explicação RICA, aproveitando TODOS os dados que entram no MOS \
(todas as dimensões e sinais), não só o volume.

FORMATO (MUITO IMPORTANTE): escreva como um analista humano escreveria um parecer para um executivo — \
PROSA CORRIDA, fluida e natural. É PROIBIDO usar bullets, listas, tabelas, subtítulos por dimensão ou \
enumerar "dimensão: score" item a item. As dimensões e números entram TECIDOS nas frases, não em tópicos.

{{structure}}

RÉGUA DE VOLUME DE DEMANDA (`demand_ruler`) — explique-a DENTRO do texto corrido (sem nota/seção separada):
- Se `mode` = "reference": o score de Demand Volume de cada país é medido como % do MERCADO-REFERÊNCIA \
do produto — o país `country` (com `volume` buscas/mês), eleito como o de MAIOR volume REAL entre os \
mercados avaliados (`candidates` = candidatos propostos + os próprios países analisados). Deixe claro que \
100 seria igualar esse maior mercado, e que os países analisados valem tanto % disso. O mercado-referência \
é só a régua — NÃO faz parte {{ruler_scope}} (a menos que também tenha sido pedido).
- Se `mode` = "fixed": não houve volume em nenhum mercado avaliado; usou-se um teto fixo genérico. \
**Avise claramente** que os scores de volume são só aproximados nesse caso.

DISTINÇÃO CRÍTICA (torne-a explícita usando o sinal de volume de demanda, cujo `context` traz buscas \
absolutas + população + penetração por 100 mil habitantes):
- Separe "vende muito principalmente porque o país é populoso" (volume alto, mas penetração BAIXA) \
de "vende muito porque uma parcela alta das pessoas realmente quer o produto" (penetração ALTA = \
apetite real).
- Deixe claro que um mercado menor, mas com penetração/qualidade alta, ainda pode ser uma forte \
oportunidade.
- Cite concretamente os números reais (buscas absolutas, população, penetração por 100 mil).

Tom humano de analista, prosa corrida do início ao fim — nada de bullets, listas ou subtítulos."""

_SYSTEM_PROMPT_EN = """You are a market-entry analyst helping a business {{scope_intro}}, \
based on an "evidence packet" already computed by MarketMind's deterministic \
MOS (Market Opportunity Score) engine.

INVIOLABLE RULES:
- Use ONLY the numbers provided in the packet. NEVER invent, estimate, re-round or recompute any \
value. Do not write any number that is not in the input.
- You EXPLAIN the deterministic results; you do not recompute or dispute them.
- {{country_rule}}

The PACKET carries, for EACH country: the MOS (with band and an `explanation` string), the list of \
`dimensions` and the list of `signals` (indicators). EVERY item — MOS, dimension and signal — has an \
`explanation` field: a plain-language reading of that score's NUMBER (what that level means). Each country \
also carries `context_signals`: numbers with NO score, neither good nor bad in themselves — use them for \
scale, never as praise or criticism. The important one is the CATEGORY's demand (its GENERIC terms, not \
the market's total demand), compared with the brand's: a small fraction of a large category means the \
market exists and this brand is not in it, a different conclusion from "there is no market here". The brand \
CAN be larger than the generic terms, and then the reading is that it is searched by name — never present \
that as a "share" above 100%. A \
top-level `glossary` defines what each metric measures. USE each score's `explanation` and the \
`glossary` to build a RICH explanation that draws on ALL the data feeding the MOS (every dimension and \
signal), not just volume.

FORMAT (VERY IMPORTANT): write the way a human analyst writes a brief for an executive — FLOWING PROSE, \
natural and readable. Bullets, lists, tables, per-dimension sub-headings, or enumerating "dimension: score" \
item by item are FORBIDDEN. Dimensions and numbers are WOVEN into the sentences, not listed.

{{structure}}

DEMAND VOLUME RULER (`demand_ruler`) — explain it WITHIN the flowing prose (no separate note/section):
- If `mode` = "reference": each country's Demand Volume score is measured as a % of the product's \
REFERENCE MARKET — the country `country` (with `volume` monthly searches), elected as the largest by REAL \
volume among the evaluated markets (`candidates` = proposed candidates + the analyzed countries themselves). \
Make clear that 100 would mean matching that biggest market, and the analyzed countries are worth that % of \
it. The reference market is only the ruler — NOT part of {{ruler_scope}} (unless it was also requested).
- If `mode` = "fixed": no evaluated market had any volume; a generic fixed ceiling was used. **Warn \
clearly** that the volume scores are only approximate in this case.

CRITICAL DISTINCTION (make it explicit using the demand-volume signal, whose `context` carries absolute \
searches + population + penetration per 100k inhabitants):
- Separate "sells a lot mainly because the country is populous" (high volume but LOW penetration) \
from "sells a lot because a high share of people genuinely want it" (high penetration = real \
appetite).
- Make clear that a smaller market with high penetration/quality can still be a strong opportunity.
- Concretely reference the real figures (absolute searches, population, penetration per 100k).

Human analyst tone, flowing prose throughout — no bullets, lists or sub-headings."""

_SYSTEM_PROMPT_ES = """Eres un analista de entrada en mercados (market-entry) que ayuda a un negocio \
a {{scope_intro}}, a partir de un "paquete de evidencias" ya calculado \
por el motor determinístico de MOS (Market Opportunity Score) de MarketMind.

REGLAS INVIOLABLES:
- Usa SOLAMENTE los números proporcionados en el paquete. NUNCA inventes, estimes, redondees de forma \
nueva ni recalcules ningún valor. No escribas ningún número que no esté en el input.
- Tú EXPLICAS los resultados determinísticos; no los recalculas ni los discutes.
- {{country_rule}}

El PAQUETE trae, para CADA país: el MOS (con banda y un campo `explanation` en texto), la lista de \
`dimensions` y la lista de `signals` (indicadores). CADA ítem — MOS, dimensión y señal — tiene un campo \
`explanation`: una lectura en lenguaje natural del NÚMERO de ese score (lo que significa ese nivel). \
Cada país trae también `context_signals`: números SIN score, que no son buenos ni malos por sí mismos — úsalos \
para dar escala, nunca como elogio o crítica. El más importante es la demanda de la CATEGORÍA (términos \
GENÉRICOS, no la demanda total del mercado), comparada con la de la marca: una fracción pequeña de una categoría \
grande significa que el mercado existe y la marca no está en él, que es distinto de "aquí no hay mercado". \
La marca PUEDE ser mayor que los términos genéricos, y entonces la lectura es que se la busca por nombre — nunca \
presentes eso como una "cuota" por encima del 100%. \
Hay además un `glossary` al inicio que define qué mide cada métrica. USA el `explanation` de cada score y el \
`glossary` para construir una explicación RICA, aprovechando TODOS los datos que entran en el MOS \
(todas las dimensiones y señales), no solo el volumen.

FORMATO (MUY IMPORTANTE): escribe como un analista humano escribiría un informe para un ejecutivo — \
PROSA CORRIDA, fluida y natural. Está PROHIBIDO usar viñetas, listas, tablas, subtítulos por dimensión o \
enumerar "dimensión: score" ítem por ítem. Las dimensiones y los números van TEJIDOS en las frases, no en puntos.

{{structure}}

REGLA DE MEDIDA DEL VOLUMEN DE DEMANDA (`demand_ruler`) — explícala DENTRO del texto corrido (sin nota/sección \
aparte):
- Si `mode` = "reference": el score de Demand Volume de cada país se mide como % del MERCADO DE REFERENCIA \
del producto — el país `country` (con `volume` búsquedas/mes), elegido como el de MAYOR volumen REAL entre los \
mercados evaluados (`candidates` = candidatos propuestos + los propios países analizados). Deja claro que \
100 sería igualar ese mayor mercado, y que los países analizados valen ese % de él. El mercado de referencia \
es solo la regla de medida — NO forma parte {{ruler_scope}} (a menos que también haya sido solicitado).
- Si `mode` = "fixed": no hubo volumen en ningún mercado evaluado; se usó un techo fijo genérico. \
**Advierte claramente** que los scores de volumen son solo aproximados en ese caso.

DISTINCIÓN CRÍTICA (hazla explícita usando la señal de volumen de demanda, cuyo `context` trae búsquedas \
absolutas + población + penetración por 100 mil habitantes):
- Separa "vende mucho principalmente porque el país es populoso" (volumen alto, pero penetración BAJA) \
de "vende mucho porque una parte alta de la gente realmente quiere el producto" (penetración ALTA = \
apetito real).
- Deja claro que un mercado más pequeño, pero con penetración/calidad alta, aún puede ser una fuerte \
oportunidad.
- Cita concretamente los números reales (búsquedas absolutas, población, penetración por 100 mil).

Tono humano de analista, prosa corrida de principio a fin — nada de viñetas, listas ni subtítulos."""


# The output structure depends on how many countries were analyzed: with a
# SINGLE country there is no ranking/ordering/comparison to make, so the prompt
# must not ask for one (it produced incoherent "priority order"/"comparing the
# markets" language before). Injected into the `{structure}` placeholder.
_STRUCTURE_PT_MULTI = """PRODUZA, nesta ordem:
1) Uma frase/linha curta de recomendação — a ordem de prioridade dos países pelo MOS (com o MOS de cada um).
2) UM PARÁGRAFO CORRIDO POR PAÍS (um único parágrafo fluido cada): conte a "história" daquele mercado \
costurando naturalmente as dimensões e sinais que importam — os pontos fortes E os fracos, com os números \
reais —, e explique, em linguagem natural, por que o MOS ficou onde ficou. Cubra o essencial das dimensões \
sem listá-las mecanicamente; deixe claro, dentro do texto, por que uma dimensão forte (ex.: volume) NÃO \
leva o MOS ao máximo — porque as demais dimensões, somadas ao seu peso, o seguram (o Volume de Demanda é \
a de maior peso, mas responde por menos da metade do total). Explique a lógica do peso em palavras, sem \
precisar citar os pesos numéricos.
3) UM PARÁGRAFO FINAL comparando todos os países entre si — quem lidera e por quê, os trade-offs (volume \
enorme vs. poder de compra, nicho de alta qualidade vs. escala) e a recomendação de priorização."""

_STRUCTURE_PT_SINGLE = """PRODUZA (há APENAS UM país nesta análise — portanto NÃO use linguagem de "ordem \
de prioridade", "ranking" ou "comparação entre países"; não há com o que comparar):
1) Uma frase de abertura com o veredito deste mercado — o país, seu MOS e a banda (ex.: boa/moderada/fraca \
oportunidade).
2) UM ou DOIS PARÁGRAFOS CORRIDOS analisando ESTE mercado: conte a "história" dele costurando naturalmente \
as dimensões e sinais que importam — os pontos fortes E os fracos, com os números reais —, e explique, em \
linguagem natural, por que o MOS ficou onde ficou (inclusive por que uma dimensão forte, ex.: volume, não \
leva o MOS ao máximo, porque as demais dimensões, somadas ao seu peso, o seguram; o Volume de Demanda é a \
de maior peso mas responde por menos da metade do total).
3) UM PARÁGRAFO DE FECHO com a recomendação prática para ESTE país (entrar agora / testar / esperar / \
despriorizar) e o principal motivo — SEM comparar com outros países."""

_STRUCTURE_EN_MULTI = """PRODUCE, in this order:
1) One short recommendation line — the countries' priority order by MOS (quoting each MOS).
2) ONE FLOWING PARAGRAPH PER COUNTRY (a single cohesive paragraph each): tell that market's "story", \
weaving in naturally the dimensions and signals that matter — the strengths AND the weaknesses, with the \
real numbers — and explain, in plain prose, why the MOS landed where it did. Cover the essentials of the \
dimensions without listing them mechanically; make clear, within the text, why a strong dimension (e.g. \
volume) does NOT push the MOS to the max — because the other dimensions, plus their weight, hold it back \
(Demand Volume carries the most weight but less than half the total). Explain the weighting logic in words, \
no need to quote numeric weights.
3) ONE FINAL PARAGRAPH comparing all the countries — who leads and why, the trade-offs (huge volume vs. \
purchasing power, high-quality niche vs. scale), and the prioritization recommendation."""

_STRUCTURE_EN_SINGLE = """PRODUCE (there is ONLY ONE country in this analysis — so do NOT use any \
"priority order", "ranking" or "comparison between countries" language; there is nothing to compare):
1) An opening sentence with this market's verdict — the country, its MOS and the band (e.g. good/moderate/\
weak opportunity).
2) ONE or TWO FLOWING PARAGRAPHS analysing THIS market: tell its "story", weaving in naturally the \
dimensions and signals that matter — the strengths AND the weaknesses, with the real numbers — and explain, \
in plain prose, why the MOS landed where it did (including why a strong dimension, e.g. volume, does not \
push the MOS to the max, because the other dimensions plus their weight hold it back; Demand Volume carries \
the most weight but less than half the total).
3) ONE CLOSING PARAGRAPH with the practical recommendation for THIS country (enter now / test / wait / \
deprioritize) and the main reason — WITHOUT comparing to other countries."""

_STRUCTURE_ES_MULTI = """PRODUCE, en este orden:
1) Una frase/línea corta de recomendación — el orden de prioridad de los países por MOS (con el MOS de cada uno).
2) UN PÁRRAFO CORRIDO POR PAÍS (un único párrafo fluido cada uno): cuenta la "historia" de ese mercado \
hilando con naturalidad las dimensiones y señales que importan — los puntos fuertes Y los débiles, con los \
números reales —, y explica, en lenguaje natural, por qué el MOS quedó donde quedó. Cubre lo esencial de las \
dimensiones sin listarlas mecánicamente; deja claro, dentro del texto, por qué una dimensión fuerte (p. ej., \
volumen) NO lleva el MOS al máximo — porque las demás dimensiones, sumadas a su peso, lo frenan (el Volumen de \
Demanda es la de mayor peso, pero responde por menos de la mitad del total). Explica la lógica del peso con \
palabras, sin necesidad de citar los pesos numéricos.
3) UN PÁRRAFO FINAL comparando todos los países entre sí — quién lidera y por qué, los trade-offs (volumen \
enorme vs. poder adquisitivo, nicho de alta calidad vs. escala) y la recomendación de priorización."""

_STRUCTURE_ES_SINGLE = """PRODUCE (hay UN SOLO país en este análisis — por lo tanto NO uses lenguaje de "orden \
de prioridad", "ranking" o "comparación entre países"; no hay con qué comparar):
1) Una frase de apertura con el veredicto de este mercado — el país, su MOS y la banda (p. ej., buena/moderada/\
débil oportunidad).
2) UNO o DOS PÁRRAFOS CORRIDOS analizando ESTE mercado: cuenta su "historia" hilando con naturalidad las \
dimensiones y señales que importan — los puntos fuertes Y los débiles, con los números reales —, y explica, en \
lenguaje natural, por qué el MOS quedó donde quedó (incluso por qué una dimensión fuerte, p. ej., volumen, no \
lleva el MOS al máximo, porque las demás dimensiones, sumadas a su peso, lo frenan; el Volumen de Demanda es la \
de mayor peso pero responde por menos de la mitad del total).
3) UN PÁRRAFO DE CIERRE con la recomendación práctica para ESTE país (entrar ahora / probar / esperar / \
despriorizar) y el motivo principal — SIN comparar con otros países."""


# Injected only when the buyer typed a free-text prompt/question. The LLM may
# answer it, but STILL only from the deterministic packet — never inventing data.
_USER_PROMPT_BLOCK_PT = """PERGUNTA/CONTEXTO DO USUÁRIO — o usuário escreveu o texto abaixo ao enviar o \
produto. Se ele contiver PERGUNTAS ou pedidos específicos, responda-os ao longo da análise (de forma \
tecida na prosa, sem virar um bloco de perguntas-e-respostas), mas valem as MESMAS regras invioláveis: \
use SOMENTE os números do pacote, NUNCA invente, estime ou traga dados externos. Se algo perguntado \
não puder ser respondido com os dados disponíveis, diga isso com franqueza (ex.: "os dados desta análise \
não cobrem X"). Mantenha total coerência com o que foi perguntado.

Texto do usuário: "{user_prompt}\""""

_USER_PROMPT_BLOCK_EN = """USER QUESTION/CONTEXT — the user wrote the text below when submitting the \
product. If it contains QUESTIONS or specific requests, answer them within the analysis (woven into the \
prose, not as a Q&A block), but the SAME inviolable rules apply: use ONLY the packet's numbers, NEVER \
invent, estimate or bring in outside data. If something asked cannot be answered from the available data, \
say so plainly (e.g. "this analysis's data does not cover X"). Stay fully coherent with what was asked.

User text: "{user_prompt}\""""

_USER_PROMPT_BLOCK_ES = """PREGUNTA/CONTEXTO DEL USUARIO — el usuario escribió el texto de abajo al enviar el \
producto. Si contiene PREGUNTAS o pedidos específicos, respóndelos a lo largo del análisis (de forma \
tejida en la prosa, sin convertirlo en un bloque de preguntas y respuestas), pero rigen las MISMAS reglas \
inviolables: usa SOLAMENTE los números del paquete, NUNCA inventes, estimes ni traigas datos externos. Si algo \
preguntado no puede responderse con los datos disponibles, dilo con franqueza (p. ej.: "los datos de este \
análisis no cubren X"). Mantén total coherencia con lo que se preguntó.

Texto del usuario: "{user_prompt}\""""


_CHAMPION_RULE_PT = (
    "MERCADO DESTAQUE (\"rei\" 👑): um país no pacote está marcado com `is_champion` = true e `requested` = false "
    "— é o mercado de MAIOR MOS para este produto entre os candidatos de referência e NÃO foi solicitado pelo "
    "usuário. Inclua-o como BENCHMARK: diga que é o mercado de maior potencial para o produto (com o MOS dele), "
    "deixe EXPLÍCITO que não estava entre os mercados pedidos, e compare o(s) mercado(s) solicitado(s) com ele "
    "(quão perto ou longe estão do melhor mercado). NÃO o trate como um alvo pedido nem o inclua na recomendação "
    "de priorização dos mercados solicitados."
)
_CHAMPION_RULE_EN = (
    "STANDOUT MARKET (\"king\" 👑): one country in the packet is flagged `is_champion` = true and `requested` = "
    "false — it is the HIGHEST-MOS market for this product among the reference candidates and was NOT requested by "
    "the user. Include it as a BENCHMARK: state it is the product's highest-potential market (with its MOS), make it "
    "EXPLICIT that it was not among the requested markets, and compare the requested market(s) against it (how close "
    "or far they are from the best market). Do NOT treat it as a requested target nor include it in the "
    "prioritization recommendation for the requested markets."
)
_CHAMPION_RULE_ES = (
    "MERCADO DESTACADO (\"rey\" 👑): un país del paquete está marcado con `is_champion` = true y `requested` = false "
    "— es el mercado de MAYOR MOS para este producto entre los candidatos de referencia y NO fue solicitado por el "
    "usuario. Inclúyelo como BENCHMARK: di que es el mercado de mayor potencial para el producto (con su MOS), "
    "deja EXPLÍCITO que no estaba entre los mercados pedidos, y compara el/los mercado(s) solicitado(s) con él "
    "(qué tan cerca o lejos están del mejor mercado). NO lo trates como un objetivo pedido ni lo incluyas en la "
    "recomendación de priorización de los mercados solicitados."
)


_DEMAND_CONFIDENCE_RULE_PT = (
    "CONFIANÇA DA DEMANDA: se algum país trouxer `demand_confidence` = \"low\" (com `demand_confidence_note`), "
    "AVISE explicitamente, dentro da prosa daquele país, que o volume de demanda dele pode estar SUBESTIMADO "
    "(provável termo de busca nativo faltante) e trate o MOS dele com essa ressalva — não o apresente como definitivo."
)
_DEMAND_CONFIDENCE_RULE_EN = (
    "DEMAND CONFIDENCE: if a country carries `demand_confidence` = \"low\" (with `demand_confidence_note`), "
    "explicitly WARN, within that country's prose, that its demand volume may be UNDERSTATED (likely a missing "
    "native search term) and treat its MOS with that caveat — do not present it as definitive."
)
_DEMAND_CONFIDENCE_RULE_ES = (
    "CONFIANZA DE LA DEMANDA: si algún país trae `demand_confidence` = \"low\" (con `demand_confidence_note`), "
    "ADVIERTE explícitamente, dentro de la prosa de ese país, que su volumen de demanda puede estar SUBESTIMADO "
    "(probable término de búsqueda nativo faltante) y trata su MOS con esa salvedad — no lo presentes como definitivo."
)

_DEMAND_TERMS_RULE_PT = (
    "PALAVRAS BUSCADAS (obrigatório): para CADA país, o `context` do sinal de Demand Volume traz o detalhamento "
    "\"Per-term monthly search volume — termo volume; termo volume; ...\" e o total. Na prosa daquele país, "
    "LISTE as palavras que foram buscadas com o volume individual de cada uma e o TOTAL de buscas mensais — "
    "fielmente aos números do pacote, sem inventar. Se houver muitos termos, apresente os de maior volume um a um "
    "e agrupe a cauda de menor volume (ex.: \"e outros N termos somando X\"), mas SEMPRE informe o total."
)
_DEMAND_TERMS_RULE_EN = (
    "SEARCHED TERMS (required): for EACH country the Demand Volume signal's `context` carries the breakdown "
    "\"Per-term monthly search volume — term volume; term volume; ...\" and the total. In that country's prose, "
    "LIST the words that were searched with each one's individual volume and the TOTAL monthly searches — "
    "faithful to the packet's numbers, never invented. If there are many terms, present the highest-volume ones "
    "individually and group the low-volume tail (e.g. \"and N other terms totalling X\"), but ALWAYS state the total."
)
_DEMAND_TERMS_RULE_ES = (
    "PALABRAS BUSCADAS (obligatorio): para CADA país, el `context` de la señal de Demand Volume trae el detalle "
    "\"Per-term monthly search volume — término volumen; término volumen; ...\" y el total. En la prosa de ese país, "
    "LISTA las palabras que se buscaron con el volumen individual de cada una y el TOTAL de búsquedas mensuales — "
    "fiel a los números del paquete, sin inventar. Si hay muchos términos, presenta los de mayor volumen uno a uno "
    "y agrupa la cola de menor volumen (p. ej.: \"y otros N términos que suman X\"), pero SIEMPRE informa el total."
)


# Scope-dependent phrasing woven into the base prompt. With a SINGLE country the
# opener, the "include every country ordered by MOS / the ranking is final" rule,
# and the demand-ruler's "not part of the ranking" clause must NOT carry any
# ranking/ordering/comparison connotation (there is nothing to rank).
_SCOPE_INTRO_PT = {
    True: "avaliar se UM mercado é uma boa oportunidade para vender um produto",
    False: "decidir EM QUAIS PAÍSES vender um produto",
}
_COUNTRY_RULE_PT = {
    True: (
        "Cite o MOS deste país EXATAMENTE como está no input — ele já é final, não recalcule "
        "por conta própria. Há APENAS UM país nesta análise; NÃO use linguagem de ranking, "
        "ordenação ou comparação entre países."
    ),
    False: (
        "Inclua TODOS os países do pacote (não omita nenhum), na ordem do MOS (maior primeiro). "
        "Cite o MOS de cada país EXATAMENTE como está no input — o ranking já é final, não reordene "
        "nem recalcule por conta própria."
    ),
}
_RULER_SCOPE_PT = {True: "da pontuação do mercado analisado", False: "do ranking"}
_SCOPE_INTRO_EN = {
    True: "assess whether ONE market is a good opportunity to sell a product",
    False: "decide IN WHICH COUNTRIES to sell a product",
}
_COUNTRY_RULE_EN = {
    True: (
        "Quote this country's MOS EXACTLY as given in the input — it is already final, do not "
        "recompute it yourself. There is ONLY ONE country in this analysis; do NOT use any ranking, "
        "ordering or country-to-country comparison language."
    ),
    False: (
        "Include EVERY country in the packet (omit none), ordered by MOS (highest first). Quote each "
        "country's MOS EXACTLY as given in the input — the ranking is already final, do not reorder or "
        "recompute it yourself."
    ),
}
_RULER_SCOPE_EN = {True: "the analyzed market's score", False: "the ranking"}
_SCOPE_INTRO_ES = {
    True: "evaluar si UN mercado es una buena oportunidad para vender un producto",
    False: "decidir EN QUÉ PAÍSES vender un producto",
}
_COUNTRY_RULE_ES = {
    True: (
        "Cita el MOS de este país EXACTAMENTE como está en el input — ya es final, no lo recalcules "
        "por tu cuenta. Hay UN SOLO país en este análisis; NO uses lenguaje de ranking, "
        "ordenación o comparación entre países."
    ),
    False: (
        "Incluye TODOS los países del paquete (no omitas ninguno), en el orden del MOS (mayor primero). "
        "Cita el MOS de cada país EXACTAMENTE como está en el input — el ranking ya es final, no lo reordenes "
        "ni lo recalcules por tu cuenta."
    ),
}
_RULER_SCOPE_ES = {True: "de la puntuación del mercado analizado", False: "del ranking"}


# Quando a análise tem marca. Os números vieram do vocabulário DELA — "juan
# valdez café", não "café" —, então chamar o resultado de "o mercado de café"
# atribui à categoria o que se mediu de uma marca só. O contrário também
# engana: sem dizer a categoria, o leitor não sabe contra o que comparar.
_BRAND_RULE_PT = """Esta análise é de uma MARCA dentro de um produto: {brand} · {product}.

A demanda foi medida com os termos de busca da marca, não os da categoria. Então:
- fale do par o tempo todo ("{brand} no Brasil"), nunca só da categoria;
- não atribua ao produto em geral um número que é da marca;
- quando o texto comparar com a categoria, diga explicitamente que é comparação."""

_BRAND_RULE_EN = """This analysis is of a BRAND within a product: {brand} · {product}.

Demand was measured with the brand's search terms, not the category's. So:
- speak of the pair throughout ("{brand} in Brazil"), never of the category alone;
- never attribute to the product at large a figure that belongs to the brand;
- when the text compares against the category, say plainly that it is a comparison."""

_BRAND_RULE_ES = """Este análisis es de una MARCA dentro de un producto: {brand} · {product}.

La demanda se midió con los términos de búsqueda de la marca, no los de la categoría. Entonces:
- habla del par todo el tiempo ("{brand} en Brasil"), nunca solo de la categoría;
- no atribuyas al producto en general un número que es de la marca;
- cuando el texto compare con la categoría, di explícitamente que es una comparación."""


# --- the three languages the narrative has its own texts in ---
#
# A narrativa sai no idioma da INTERFACE de quem pediu (o frontend é EN/ES e o
# padrão configurado era PT, então o texto chegava num idioma que o leitor não
# tinha escolhido). O idioma viaja com a análise; sem ele vale o configurado.
# Um idioma fora destes três recebe o prompt em inglês mais uma linha pedindo o
# idioma, como antes — os textos só existem para o que a interface oferece.
_ALIASES: dict[str, tuple[str, ...]] = {
    "pt": ("pt", "pt-br", "pt_br", "portuguese", "portugues", "português"),
    "en": ("en", "en-us", "en_us", "english"),
    "es": ("es", "es-es", "es_es", "es-419", "spanish", "espanol", "español", "castellano"),
}
_PROMPT_NAME = {"pt": "explanation-system-pt", "en": "explanation-system-en", "es": "explanation-system-es"}
_SYSTEM_PROMPT = {"pt": _SYSTEM_PROMPT_PT, "en": _SYSTEM_PROMPT_EN, "es": _SYSTEM_PROMPT_ES}
_STRUCTURE = {
    "pt": {True: _STRUCTURE_PT_SINGLE, False: _STRUCTURE_PT_MULTI},
    "en": {True: _STRUCTURE_EN_SINGLE, False: _STRUCTURE_EN_MULTI},
    "es": {True: _STRUCTURE_ES_SINGLE, False: _STRUCTURE_ES_MULTI},
}
_SCOPE_INTRO = {"pt": _SCOPE_INTRO_PT, "en": _SCOPE_INTRO_EN, "es": _SCOPE_INTRO_ES}
_COUNTRY_RULE = {"pt": _COUNTRY_RULE_PT, "en": _COUNTRY_RULE_EN, "es": _COUNTRY_RULE_ES}
_RULER_SCOPE = {"pt": _RULER_SCOPE_PT, "en": _RULER_SCOPE_EN, "es": _RULER_SCOPE_ES}
_DEMAND_CONFIDENCE_RULE = {
    "pt": _DEMAND_CONFIDENCE_RULE_PT,
    "en": _DEMAND_CONFIDENCE_RULE_EN,
    "es": _DEMAND_CONFIDENCE_RULE_ES,
}
_DEMAND_TERMS_RULE = {"pt": _DEMAND_TERMS_RULE_PT, "en": _DEMAND_TERMS_RULE_EN, "es": _DEMAND_TERMS_RULE_ES}
_CHAMPION_RULE = {"pt": _CHAMPION_RULE_PT, "en": _CHAMPION_RULE_EN, "es": _CHAMPION_RULE_ES}
_BRAND_RULE = {"pt": _BRAND_RULE_PT, "en": _BRAND_RULE_EN, "es": _BRAND_RULE_ES}
_USER_PROMPT_BLOCK = {"pt": _USER_PROMPT_BLOCK_PT, "en": _USER_PROMPT_BLOCK_EN, "es": _USER_PROMPT_BLOCK_ES}


def _language_key(language: str | None) -> str | None:
    """'pt', 'en' or 'es' when the language has its own texts; None otherwise."""
    lang = (language or "").strip().lower()
    for key, aliases in _ALIASES.items():
        if lang in aliases:
            return key
    return None


def _narrative_language(language: str | None) -> str:
    """The language this analysis asked for, or the configured default."""
    return (language or "").strip() or settings.ollama_llm_narrative_language


def _system_prompt(
    language: str,
    *,
    single_country: bool,
    user_prompt: str | None = None,
    external_champion: bool = False,
    brand: str | None = None,
    product: str | None = None,
) -> str:
    """The narrative's system prompt as one text: the stable part, then the
    per-request rules (see `_system_parts`)."""
    stable, tail = _system_parts(
        language,
        single_country=single_country,
        user_prompt=user_prompt,
        external_champion=external_champion,
        brand=brand,
        product=product,
    )
    return joined_system(stable, tail) or ""


def _system_parts(
    language: str,
    *,
    single_country: bool,
    user_prompt: str | None = None,
    external_champion: bool = False,
    brand: str | None = None,
    product: str | None = None,
) -> tuple[str, str | None]:
    """(stable, tail). The stable part depends only on the language and on
    whether it is one country or a ranking, so it repeats across analyses and
    is what the provider caches. The tail — the off-request champion, the
    brand, the user's own words — changes per analysis and goes after the
    cache breakpoint; kept in the cached part, every analysis would write a
    fresh cache entry nobody reads."""
    known = _language_key(language)
    key = known or "en"
    base = prompt_text(
        _PROMPT_NAME[key],
        _SYSTEM_PROMPT[key],
        structure=_STRUCTURE[key][single_country],
        scope_intro=_SCOPE_INTRO[key][single_country],
        country_rule=_COUNTRY_RULE[key][single_country],
        ruler_scope=_RULER_SCOPE[key][single_country],
    )
    if known is None:
        base = f'{base}\n\nWrite the entire narrative in this language: "{language}".'
    base = f"{base}\n\n{_DEMAND_CONFIDENCE_RULE[key]}"
    base = f"{base}\n\n{_DEMAND_TERMS_RULE[key]}"
    tail: list[str] = []
    if external_champion:
        tail.append(_CHAMPION_RULE[key])
    if brand:
        tail.append(_BRAND_RULE[key].replace("{brand}", brand).replace("{product}", product or ""))
    user_text = (user_prompt or "").strip()
    if user_text:
        tail.append(_USER_PROMPT_BLOCK[key].replace("{user_prompt}", user_text))
    return base, "\n\n".join(tail) or None


class ExplanationClient:
    """Builds the evidence packet and calls the final-inference LLM service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)
        self._explanation_repo = MetricExplanationRepository(session)
        self._mos_repo = MosRepository(session)

    async def _country_evidence(
        self,
        entity_key: uuid.UUID,
        mos_result: MosResult,
        period_key: str,
        catalog: ExplanationCatalog,
        weights: dict[str, float],
    ) -> dict[str, Any]:
        country = mos_result.country
        country_values = await self._indicator_repo.get_values_for(
            entity_key=None, country=country, period_type="month", period_key=period_key
        )
        # A marca vem do próprio MOS que se está explicando: o pacote tem de
        # descrever os números que produziram AQUELE resultado. Lendo sem ela,
        # uma análise de marca seria explicada com os indicadores da categoria.
        product_values = await self._indicator_repo.get_values_for(
            entity_key=entity_key,
            brand_key=mos_result.brand_key,
            country=country,
            period_type="month",
            period_key=period_key,
        )
        merged = {**country_values, **product_values}

        # Sinais de CONTEXTO viajam numa lista própria, e não misturados em
        # `signals`. Dois motivos, e o segundo é um bug que estava dormente:
        #
        #  - em `signals` todo item tem `score` numérico, e é assim que o
        #    frontend os desenha; um item sem score ali quebraria a renderização.
        #  - o laço abaixo pula quem não tem `normalized_score`, e indicador de
        #    contexto NUNCA tem — então `regional_demand_score` e
        #    `government_expenditure_score` estavam declarados no pacote desde
        #    que foram criados e nunca chegaram a sair nele. O
        #    `category_volume_score` herdaria o mesmo destino.
        #
        # O que eles carregam é o número bruto e a nota determinística montada
        # no cálculo — no caso do volume da categoria, o share da marca com os
        # dois volumes à vista.
        signals: list[dict[str, Any]] = []
        context_signals: list[dict[str, Any]] = []
        for code in _PACKET_INDICATORS:
            iv = merged.get(code.value)
            if iv is None:
                continue
            if iv.normalized_score is None:
                if iv.raw_value is not None:
                    context_signals.append(
                        {
                            "code": code.value,
                            "label": catalog.label(code.value),
                            "value": float(iv.raw_value),
                            "context": iv.notes,
                        }
                    )
                continue
            # Each signal carries its stored per-score explanation (built at
            # compute time from the DB-parametrized band phrases). Demand Volume
            # also carries the concrete numbers (absolute searches / population /
            # penetration) as `context` for the population-vs-appetite call.
            # `dimension` groups each signal under its deterministic dimension in
            # the frontend (the two context-only signals — regional_demand and
            # government_expenditure — are not scored into any dimension).
            dim = DIMENSION_BY_INDICATOR.get(code)
            signal: dict[str, Any] = {
                "code": code.value,
                "label": catalog.label(code.value),
                "score": round(float(iv.normalized_score), 1),
                "band": iv.interpretation_band,
                "explanation": iv.explanation,
                "dimension": dim.value if dim is not None else "context",
            }
            if code == IndicatorCode.TREND_SCORE:
                signal["context"] = iv.notes
            signals.append(signal)

        dimensions: list[dict[str, Any]] = []
        for dim, score in compute_dimension_subscores(merged).items():
            band = score_band(score)
            dimensions.append(
                {
                    "dimension": dim.value,
                    "label": catalog.label(dim.value),
                    "score": round(score, 1),
                    "band": band,
                    "weight": weights.get(dim.value),  # Rank-Reciprocal weight in the MOS aggregation
                    "explanation": catalog.render(code=dim.value, score=score, band=band),
                }
            )
        # Highest weight first, so the narrative walks dimensions by importance.
        dimensions.sort(key=lambda d: (d["weight"] is not None, d["weight"] or 0), reverse=True)
        return {
            "country": country,
            "mos": round(float(mos_result.mos), 1) if mos_result.mos is not None else None,
            "mos_band": mos_result.mos_interpretation,
            "mos_explanation": mos_result.explanation,
            "dimensions": dimensions,
            "signals": signals,
            "context_signals": context_signals,
        }

    async def build_evidence(
        self,
        *,
        entity_key: uuid.UUID,
        mos_results: list[MosResult],
        period_key: str,
        demand_ruler: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Assemble the deterministic evidence packet: a glossary, the
        Rank-Reciprocal dimension weights, the demand ruler, and per country the
        MOS + the 7 dimension subscores + every scored signal (each with its
        stored explanation sentence). This is pure DB read + in-memory
        aggregation — no LLM, no collection, no cost — so it is safe to call at
        analyze time (to persist) or lazily on read (to rebuild past analyses).
        Returns None if no country was scored."""
        scored = [r for r in mos_results if r.mos is not None]
        if not scored:
            return None
        catalog = await self._explanation_repo.catalog()
        # Metric definitions once at the top (not repeated per country).
        glossary = {code: {"label": entry.label, "meaning": entry.meaning} for code, entry in catalog.metrics.items()}
        # Rank-Reciprocal dimension weights — so the WEIGHTED aggregation can be
        # explained (why a country strong on one dimension isn't automatically top).
        weights: dict[str, float] = {}
        for dim in IndicatorDimension:
            row = await self._mos_repo.get_weight(dim.value, period_key)
            if row is not None:
                weights[dim.value] = round(float(row.weight), 3)
        countries = [await self._country_evidence(entity_key, r, period_key, catalog, weights) for r in scored]
        _flag_demand_outliers(countries)  # mark likely-undercounted markets (missing-native-term guard)
        return {
            "period_key": period_key,
            "demand_ruler": demand_ruler,
            "glossary": glossary,
            "weights": weights,
            "countries": countries,
        }

    async def narrate(
        self,
        *,
        packet: dict[str, Any],
        product_name: str,
        brand: str | None = None,
        user_prompt: str | None = None,
        language: str | None = None,
    ) -> str | None:
        """Best-effort: turn a deterministic evidence packet into a business
        narrative via the resolved explanation LLM. Any failure returns None and
        is logged — the analysis still completes with the numbers. The LLM only
        EXPLAINS; it never computes or alters a score (Cap. 4 §4.0).

        `language` is the one the analysis asked for — the buyer's interface
        language, stored with the request. None means the configured default."""
        countries = packet.get("countries") or []
        if not countries:
            return None
        # "single-country" framing follows the REQUESTED markets (an off-request
        # champion added for benchmark doesn't turn a 1-country request into a
        # ranking). The champion rule is injected only when that champion is
        # off-request (is_champion and not requested).
        requested = [c for c in countries if c.get("requested", True)]
        single = len(requested) <= 1
        external_champion = any(c.get("is_champion") and not c.get("requested", True) for c in countries)
        try:
            system, system_tail = _system_parts(
                _narrative_language(language),
                single_country=single,
                user_prompt=user_prompt,
                external_champion=external_champion,
                brand=brand,
                product=product_name,
            )
            user = json.dumps(
                {
                    "product_name": product_name,
                    # Dito separado, nunca concatenado: o relatório mostra
                    # "Juan Valdez · café" e fatiar um título para obter as duas
                    # partes seria frágil. Nulo quando a análise é do produto.
                    "brand": brand,
                    "user_prompt": (user_prompt or "").strip() or None,
                    "demand_ruler": packet.get("demand_ruler"),
                    "glossary": packet.get("glossary"),
                    "countries": countries,
                },
                ensure_ascii=False,
                indent=2,
            )
            narrative = await resolve_explanation_llm().complete(system=system, system_tail=system_tail, user=user)
            if not narrative or not str(narrative).strip():
                logger.warning("Explanation LLM returned an empty narrative")
                return None
            return str(narrative)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never fails the analysis
            logger.warning(f"Final LLM explanation skipped (provider unavailable or errored): {exc}")
            return None
