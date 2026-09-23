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



# A segunda passada. Só entra quando o juiz achou afirmações que a evidência não
# sustenta — medido em execuções reais de 23/09/2026: os números estavam certos
# (`hallucination_rate=0.0`, 0 de 134) e mesmo assim o texto afirmava "os EUA
# não fazem parte do ranking" e "a Espanha é uma aposta de exportação mais
# coerente", coisas que o pacote não diz.
#
# É a exposição que sobra depois do invariante do projeto. Nenhum número vem do
# modelo, e é isso que torna a checagem de número exata e gratuita; mas nada
# impedia o modelo de inventar a CONCLUSÃO. Numa defesa, "o modelo recomendou
# exportar com dados que não cobrem custo nem tarifa" é pior de responder que um
# número errado.
#
# Reescrever, e não apagar: a frase ofensiva costuma ter uma versão honesta
# ("a Espanha lidera o MOS" no lugar de "a Espanha é a melhor aposta").
_REVISION_RULE_PT = """REVISÃO. Você escreveu o texto abaixo e um revisor apontou afirmações que a
evidência NÃO sustenta. Reescreva o texto inteiro corrigindo cada uma delas.

Para cada apontamento, escolha uma saída:
  - reformule até a frase dizer apenas o que a evidência mostra (em geral existe
    uma versão honesta: "lidera o MOS" no lugar de "é a melhor aposta");
  - ou remova a frase.

Não acrescente informação nova, não mude nenhum número e mantenha a estrutura, o
tamanho e o idioma do texto original. Devolva SOMENTE o texto revisado.

AFIRMAÇÕES APONTADAS:
{claims}

TEXTO ORIGINAL:
{narrative}"""

_REVISION_RULE_EN = """REVISION. You wrote the text below and a reviewer flagged claims the evidence
does NOT support. Rewrite the whole text, fixing each one.

For each flagged claim, pick one:
  - rephrase it until the sentence says only what the evidence shows (there is
    usually an honest version: "leads on MOS" instead of "is the better bet");
  - or drop the sentence.

Add no new information, change no number, and keep the structure, length and
language of the original. Return ONLY the revised text.

FLAGGED CLAIMS:
{claims}

ORIGINAL TEXT:
{narrative}"""

def _system_prompt(
    language: str, *, single_country: bool, user_prompt: str | None = None, external_champion: bool = False
) -> str:
    lang = (language or "").strip().lower()
    is_pt = lang in ("pt", "pt-br", "pt_br", "portuguese", "portugues", "português")
    if is_pt:
        structure = _STRUCTURE_PT_SINGLE if single_country else _STRUCTURE_PT_MULTI
        base = prompt_text(
            "explanation-system-pt",
            _SYSTEM_PROMPT_PT,
            structure=structure,
            scope_intro=_SCOPE_INTRO_PT[single_country],
            country_rule=_COUNTRY_RULE_PT[single_country],
            ruler_scope=_RULER_SCOPE_PT[single_country],
        )
    else:
        structure = _STRUCTURE_EN_SINGLE if single_country else _STRUCTURE_EN_MULTI
        base = prompt_text(
            "explanation-system-en",
            _SYSTEM_PROMPT_EN,
            structure=structure,
            scope_intro=_SCOPE_INTRO_EN[single_country],
            country_rule=_COUNTRY_RULE_EN[single_country],
            ruler_scope=_RULER_SCOPE_EN[single_country],
        )
        if lang not in ("en", "en-us", "en_us", "english"):
            base = f'{base}\n\nWrite the entire narrative in this language: "{language}".'
    base = f"{base}\n\n{_DEMAND_CONFIDENCE_RULE_PT if is_pt else _DEMAND_CONFIDENCE_RULE_EN}"
    base = f"{base}\n\n{_DEMAND_TERMS_RULE_PT if is_pt else _DEMAND_TERMS_RULE_EN}"
    if external_champion:
        base = f"{base}\n\n{_CHAMPION_RULE_PT if is_pt else _CHAMPION_RULE_EN}"
    user_text = (user_prompt or "").strip()
    if user_text:
        block = (_USER_PROMPT_BLOCK_PT if is_pt else _USER_PROMPT_BLOCK_EN).replace("{user_prompt}", user_text)
        base = f"{base}\n\n{block}"
    return base


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
        product_values = await self._indicator_repo.get_values_for(
            entity_key=entity_key, country=country, period_type="month", period_key=period_key
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
        user_prompt: str | None = None,
    ) -> str | None:
        """Best-effort: turn a deterministic evidence packet into a business
        narrative via the resolved explanation LLM. Any failure returns None and
        is logged — the analysis still completes with the numbers. The LLM only
        EXPLAINS; it never computes or alters a score (Cap. 4 §4.0)."""
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
            system = _system_prompt(
                settings.ollama_llm_narrative_language,
                single_country=single,
                user_prompt=user_prompt,
                external_champion=external_champion,
            )
            user = json.dumps(
                {
                    "product_name": product_name,
                    "user_prompt": (user_prompt or "").strip() or None,
                    "demand_ruler": packet.get("demand_ruler"),
                    "glossary": packet.get("glossary"),
                    "countries": countries,
                },
                ensure_ascii=False,
                indent=2,
            )
            narrative = await resolve_explanation_llm().complete(system=system, user=user)
            if not narrative or not str(narrative).strip():
                logger.warning("Explanation LLM returned an empty narrative")
                return None
            return str(narrative)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never fails the analysis
            logger.warning(f"Final LLM explanation skipped (provider unavailable or errored): {exc}")
            return None

    async def revise(self, *, narrative: str, claims: list[str]) -> str | None:
        """Reescreve o texto tirando as afirmações que a evidência não sustenta.

        Melhor esforço, como a narrativa: se falhar, o texto original continua
        valendo — ele já foi entregue, e uma revisão que não sai é pior só do
        que não ter tentado, nunca pior que apagar a explicação.

        Ver `_REVISION_RULE_PT` para o porquê de reescrever em vez de apagar.
        """
        if not narrative or not claims:
            return None
        lang = (settings.ollama_llm_narrative_language or "").strip().lower()
        is_pt = lang in ("pt", "pt-br", "pt_br", "portuguese", "portugues", "português")
        modelo = _REVISION_RULE_PT if is_pt else _REVISION_RULE_EN
        pedido = modelo.replace("{claims}", "\n".join(f"- {c}" for c in claims)).replace(
            "{narrative}", narrative
        )
        try:
            revisto = await resolve_explanation_llm().complete(
                system="You revise business narratives for factual support. Return only the revised text.",
                user=pedido,
            )
        except Exception as exc:  # noqa: BLE001 — a revisão nunca pode custar o texto que já existe
            logger.warning(f"Narrative revision skipped (provider unavailable or errored): {exc}")
            return None
        if not revisto or not str(revisto).strip():
            logger.warning("Narrative revision returned nothing; keeping the original")
            return None
        return str(revisto)
