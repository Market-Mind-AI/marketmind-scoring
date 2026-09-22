"""Passo 3 da resolução de catálogo: decide se o texto consultado é o mesmo
produto de alguma entidade existente, quando alias exato e trigram falharam.

POR QUE UM LLM E NÃO UM VETOR. Substituiu o passo de embedding em 2026-08-14,
depois de medir os dois. Com os `search_terms` indexados como alias (ver
catalog_service._index_localized_terms), o trigram resolve 71 dos 89 termos
reais do catálogo com precisão de 100%. Os 18 que sobram têm uma coisa em
comum: nenhum parentesco de escrita com o que está indexado —

    odżywka bez spłukiwania   polonês, nada em comum com es/pt/en
    lockencreme / krulcrème   alemão, holandês
    quadcopter                sinônimo técnico de drone

O último é o caso que decide o desenho: `quadcopter` e `drone` são o mesmo
produto por conhecimento de domínio, não por semelhança de string nem por
proximidade no espaço vetorial. Medições contra o catálogo real mostraram que
nenhum threshold de distância separa "mesmo produto" de "produto vizinho" —
`freidora sin aceite` e `freidora con aceite` (sem óleo vs com óleo) ficam a
0,033 de distância nos melhores modelos, mais próximos que parafrases
legítimas. A palavra que decide o produto é justamente a que o vetor não
carrega.

CUSTO ASSIMÉTRICO DO ERRO, que é o que o prompt otimiza:
  - dizer "é o mesmo" errado FUNDE dois produtos numa entidade. Destrutivo e
    silencioso: `localized_markets` e a demanda de ambos se misturam, e nenhum
    passo posterior percebe.
  - dizer "é diferente" errado cria uma entidade duplicada. Recuperável: o
    pior efeito é recoletar demanda de um produto já coberto.
Daí a instrução para abster-se na dúvida — o erro barato é preferido de
propósito.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.logging import logger
from app.prompts import prompt_text
from app.services.llm.base import LLMError
from app.services.llm.resolver import resolve_catalog_match_llm

if TYPE_CHECKING:
    from app.db.models.product_catalog import ProductCatalog


class CatalogMatchDecision(BaseModel):
    """Veredito estruturado. `entity_key` vazio significa que nenhum candidato
    é o mesmo produto — o chamador então cria uma entidade nova."""

    entity_key: str = Field(
        default="",
        description=(
            "The entity_key of the candidate that is THE SAME PRODUCT, copied exactly from "
            "the list. Empty string if none of them is."
        ),
    )
    reasoning: str = Field(
        default="",
        description="One sentence naming the evidence that decided it.",
    )


_SYSTEM = (
    "You decide product IDENTITY for a catalog. Given a product name and a list of existing "
    "catalog entries, you say which entry (if any) denotes THE SAME PRODUCT.\n\n"
    "Same product means a buyer searching either name wants the same thing. Different language, "
    "different wording, brand vs generic, technical synonym, singular vs plural, and "
    "abbreviations are all the SAME product. A different function, a different category, or a "
    "different operating principle is a DIFFERENT product, even when the names look almost "
    "identical.\n\n"
    "SAME product despite looking nothing alike:\n"
    "  'air fryer' / 'freidora de aire' / 'fritadeira sem óleo' / 'ノンフライヤー'\n"
    "  'drone' / 'quadcopter' / 'multi-rotor'\n"
    "  'curl cream' / 'lockencreme' / 'krulcrème' / 'odżywka bez spłukiwania'\n\n"
    "DIFFERENT products despite looking alike — this is the failure you exist to prevent:\n"
    "  'freidora sin aceite' (no oil) vs 'freidora con aceite' (with oil)\n"
    "  'krem do loków' (curl cream) vs 'krem prostujący' (straightening cream)\n"
    "  'drone' vs 'drone software'\n"
    "  'yerba mate' vs 'yerba buena' (mint)\n\n"
    "Each entry lists the search terms already curated for it, across markets. A query that is "
    "a translation or synonym of those terms is the same product; a query that names a "
    "neighbouring product in the same category is not.\n\n"
    "An entry may also list 'NOT this product' terms: terms the catalog has already declared "
    "denote something ELSE. A query that matches one of those is NOT that entry, however much "
    "it looks like its name.\n\n"
    "Calling two different products the same MERGES them permanently and corrupts both. Calling "
    "two identical products different only creates a duplicate, which is easily fixed. When the "
    "evidence does not clearly settle it, return an empty entity_key."
)


def _describe(product: "ProductCatalog", max_terms: int = 12) -> str:
    """Uma entidade como o LLM a vê: nome, categoria e uma amostra dos termos
    curados, nos DOIS sinais. Os termos são a evidência real — o
    `canonical_title` sozinho é o idioma de quem cadastrou primeiro, não o
    vocabulário do produto.

    Os `excluded_terms` entram desde 2026-08-25. Antes só os positivos eram
    enviados, e o modelo tinha que adivinhar a fronteira do produto sem ver o
    que o catálogo já tinha declarado do outro lado dela — justamente nos casos
    que chegam aqui, que são os que a semelhança de forma não decidiu. É também
    o que o passo 2 usa para vetar: quando uma entidade chega aqui vetada, esta
    linha é a razão do veto, e o LLM precisa vê-la para arbitrar."""
    termos: list[str] = []
    for market in (product.localized_markets or {}).values():
        if not isinstance(market, dict):
            continue
        for term in market.get("search_terms") or []:
            if term and term not in termos:
                termos.append(term)
    excluidos: list[str] = []
    for market in (product.localized_markets or {}).values():
        if not isinstance(market, dict):
            continue
        for term in market.get("excluded_terms") or []:
            if term and term not in excluidos:
                excluidos.append(term)
    amostra = ", ".join(termos[:max_terms]) if termos else "(sem termos curados)"
    linhas = [
        f"- entity_key: {product.entity_key}",
        f"  name: {product.canonical_title}",
        f"  category: {product.category or 'unknown'}",
        f"  known terms: {amostra}",
    ]
    if excluidos:
        linhas.append(f"  NOT this product: {', '.join(excluidos[:max_terms])}")
    return "\n".join(linhas)


class CatalogMatcher:
    def __init__(self, provider=None) -> None:
        # Injetado nos testes; resolvido preguiçosamente em produção para que
        # importar este módulo nunca exija configuração de LLM.
        self._provider = provider

    async def pick(self, *, text: str, candidates: list["ProductCatalog"]) -> "ProductCatalog | None":
        if not candidates:
            return None

        provider = self._provider or resolve_catalog_match_llm()
        listagem = "\n".join(_describe(c) for c in candidates)
        prompt = (
            f"Product name to resolve: {text}\n\n"
            f"Existing catalog entries:\n{listagem}\n\n"
            "Which entry, if any, is the same product?"
        )

        try:
            decision = await provider.complete(
                system=prompt_text("catalog-match-system", _SYSTEM), user=prompt, response_model=CatalogMatchDecision
            )
        except LLMError as exc:
            # Falha para "sem correspondência" em vez de propagar: o chamador
            # então cria uma entidade nova, que é o erro recuperável. Um LLM
            # indisponível nunca pode causar uma fusão.
            logger.warning(f"LLM de match de catálogo indisponível, tratando como sem match: {exc}")
            return None

        if not isinstance(decision, CatalogMatchDecision) or not decision.entity_key.strip():
            return None

        escolhido = decision.entity_key.strip()
        for candidate in candidates:
            if str(candidate.entity_key) == escolhido:
                logger.info(
                    f"Match de catálogo: {text!r} -> {candidate.canonical_title!r} "
                    f"({decision.reasoning})"
                )
                return candidate

        # Chave alucinada: o modelo devolveu algo que não estava na lista.
        # Tratar como sem match — nunca cair para "o mais parecido", que
        # reintroduziria a decisão por semelhança que este passo substituiu.
        logger.warning(f"LLM devolveu entity_key desconhecida {escolhido!r}; tratando como sem match")
        return None
