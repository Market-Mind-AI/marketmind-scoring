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

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.core.logging import logger
from app.prompts import prompt_text
from app.services.llm.base import LLMError
from app.services.llm.resolver import resolve_catalog_match_llm

if TYPE_CHECKING:
    from app.db.models.product_catalog import ProductCatalog


class CatalogMatchDecision(BaseModel):
    """Veredito estruturado, com TRÊS saídas.

    Duas não bastavam, e o caso que mostrou isso foi `instant coffee` contra
    `coffee`, em 24/09/2026. Café instantâneo não é café em geral — tem outra
    demanda, outro preço, outro comprador — mas também não é um produto sem
    relação com café. Forçado a escolher entre "o mesmo" e "outro", o modelo
    respondia "o mesmo", e com razão: a entidade `coffee` já curava `café
    solúvel` entre seus termos.

    A terceira saída é a que descreve a realidade: SUB-PRODUTO. Entidade
    própria, vocabulário próprio, demanda própria — e ligada ao produto mais
    amplo, que passa a ser o contexto de categoria contra o qual ela se lê.
    """

    relationship: str = Field(
        default="none",
        description=(
            "How the query relates to the chosen entry: 'same' (the same product), "
            "'subtype' (a NARROWER product inside it), or 'none' (unrelated to every entry). "
            "'subtype' still requires entity_key: it names the broader product."
        ),
    )
    entity_key: str = Field(
        default="",
        description=(
            "The entity_key of the chosen candidate, copied exactly from the list. Empty string "
            "when relationship is 'none'."
        ),
    )
    reasoning: str = Field(
        default="",
        description="One sentence naming the evidence that decided it.",
    )
    antagonist_terms: list[str] = Field(
        default_factory=list,
        description=(
            "Only when relationship is 'same': the 1-4 OTHER products a buyer searching in the QUERY'S "
            "language could confuse with this one — same category, almost the same name, but one defining "
            "attribute opposite or different (air vs oil, wired vs wireless, with vs without caffeine). "
            "Written in the query's language, as the full names people search for those products. Empty "
            "when there is no real confusable or the relationship is not 'same'."
        ),
    )


_SYSTEM = (
    "You decide product IDENTITY for a catalog. Given a product name and a list of existing "
    "catalog entries, you say which entry (if any) denotes THE SAME PRODUCT.\n\n"
    "Same product means a buyer searching either name wants the same thing. Different language, "
    "different wording, brand vs generic, technical synonym, singular vs plural, and "
    "abbreviations are all the SAME product. A different function, a different category, or a "
    "different operating principle is a DIFFERENT product, even when the names look almost "
    "identical.\n\n"
    "THREE OUTCOMES, not two. 'same' when a buyer searching either name wants the same thing. "
    "'subtype' when the query is a NARROWER product inside one of the entries — it has its own "
    "demand, its own price, its own buyer, and belongs under that entry: 'instant coffee' under "
    "'coffee', 'extra virgin olive oil' under 'olive oil', 'hiking socks' under 'socks'. 'none' "
    "when no entry relates to it at all.\n\n"
    "Do NOT answer 'same' just because the broader entry happens to curate a term for the narrower "
    "one. A category's vocabulary naturally contains its subtypes; that is what makes it the "
    "category, and it is not evidence that the two are one product.\n\n"
    "The relationship only ever points ONE WAY: 'subtype' means the QUERY is narrower than the "
    "entry. When the query is BROADER than every entry — 'olive oil' against an entry for 'extra "
    "virgin olive oil', 'coffee' against 'instant coffee' — the answer is 'none'. The broad "
    "product is its own product, with its own demand, and the narrow entries belong under IT; "
    "answering 'same' there would file all of a category's demand under one of its subtypes.\n\n"
    "WHICH EXTRA WORDS MAKE A NARROWER PRODUCT, and which are noise. The test is whether a buyer "
    "searching the extra word is after a DIFFERENT thing to buy, at a different price:\n"
    "  NARROWER ('subtype') — a trim, version, grade, tier or model line: 'ST', 'ST-Line', "
    "'ST-Line X', 'Pro', 'Max', 'Plus', 'extra virgin', 'instant'. These are other products: other "
    "price, other buyer, other search.\n"
    "  NOISE ('same') — a colour, a model year, a storage size, a unit count or a package size on "
    "its own: 'verde', 'preto', '2023', '128gb', 'pack of 3'. These describe one unit of the SAME "
    "product, and splitting on them would cut one product's demand into arbitrary pieces.\n"
    "Both at once keeps the narrower verdict: 'ford focus ST 2023 verde' is a subtype of 'ford "
    "focus' because of ST, not despite the colour — and it is the SAME product as 'ford focus ST "
    "2023 preto'.\n\n"
    "When the query states a category and an entry names a DIFFERENT one, they are different "
    "products — however identical the names look. A name shared across categories is common "
    "('jaguar' the car and the animal, 'apple' the fruit and the brand, 'galaxy' the phone and "
    "the chocolate) and is exactly the case a name comparison cannot settle.\n\n"
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


class EditVerdict(BaseModel):
    field: str = Field(description="The field, exactly as listed.")
    accept: bool = Field(description="True when the new value fits this product; false when it contradicts it.")
    reason: str = Field(description="One short sentence saying why.")


class EditReview(BaseModel):
    verdicts: list[EditVerdict] = Field(default_factory=list)


# A conferência de uma edição feita na tela de revisão (2026-10-26). O catálogo
# é compartilhado, então uma edição aceita muda a nota de quem analisar o
# produto depois; uma recusada não muda nada. Aceitar o plausível e recusar só o
# que contradiz o produto: o usuário costuma saber coisas sobre o próprio
# mercado que o modelo não sabe.
_EDIT_REVIEW_SYSTEM = (
    "A user reviewing a product's identification changed some of its fields before running a market "
    "analysis. The product is in a shared catalog, so an accepted change affects every later analysis of it. "
    "For each change, decide whether the new value is plausible for THIS product as described. Accept "
    "anything a reasonable seller of this product could hold — the user may know their market better than "
    "you. Reject only a value that contradicts what the product is (an audience, category or price that "
    "cannot belong to this product). Return one verdict per listed field."
)


def _describe(product: "ProductCatalog", max_terms: int = 12, evidencia: str | None = None) -> str:
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
        # Antagônicos primeiro: são os que mais dizem sobre a fronteira do
        # produto, e a amostra é cortada em `max_terms`.
        for term in [*(market.get("antagonist_terms") or []), *(market.get("excluded_terms") or [])]:
            if term and term not in excluidos:
                excluidos.append(term)
    amostra = ", ".join(termos[:max_terms]) if termos else "(sem termos curados)"
    linhas = [
        f"- entity_key: {product.entity_key}",
        f"  name: {product.canonical_title}",
        f"  category: {product.category or 'unknown'}",
        f"  known terms: {amostra}",
    ]
    # Os atributos que separam o produto dos seus antagônicos, quando a
    # curadoria os nomeou ('cooking medium: hot air vs oil'). É a pergunta que
    # o modelo tem de responder sobre a consulta: de que lado deles ela está?
    atributos: list[str] = []
    for m in (product.localized_markets or {}).values():
        if isinstance(m, dict):
            atributos.extend(a for a in (m.get("contrast_attributes") or []) if a and a not in atributos)
    if atributos:
        linhas.append(f"  defining attributes vs. its confusables: {'; '.join(atributos[:6])}")
    # O que a identificação disse do produto (migration 058): o que o faz ser
    # ele e os produtos que se confundem com ele.
    if getattr(product, "defining_attributes", None):
        linhas.append(f"  what makes it this product: {'; '.join(product.defining_attributes)}")
    if getattr(product, "antagonist_products", None):
        linhas.append(f"  NOT these products: {'; '.join(product.antagonist_products)}")
    if excluidos:
        linhas.append(f"  NOT this product: {', '.join(excluidos[:max_terms])}")
    if evidencia:
        # O veto semântico do passo 3a (CatalogService._evidencias_de_polaridade):
        # medido, não decidido. Quem decide continua sendo o modelo.
        linhas.append(f"  semantic check: {evidencia}")
    return "\n".join(linhas)


class CatalogMatcher:
    def __init__(self, provider=None) -> None:
        # Injetado nos testes; resolvido preguiçosamente em produção para que
        # importar este módulo nunca exija configuração de LLM.
        self._provider = provider

    async def pick(
        self,
        *,
        text: str,
        candidates: list["ProductCatalog"],
        motivo: dict[str, Any] | None = None,
        category: str | None = None,
        evidencias: dict[uuid.UUID, str] | None = None,
    ) -> "ProductCatalog | None":
        """`motivo`, quando dado, recebe em `motivo["reasoning"]` o porquê da
        escolha — a evidência da coincidência SEMÂNTICA, que é o que este passo
        decide e os anteriores não.

        Out-param em vez de mudar o retorno porque quem chama só quer a
        entidade; o motivo interessa a quem está explicando a execução depois,
        e é o mesmo idioma de `terms_used` e `reuse_report`."""
        if not candidates:
            return None

        provider = self._provider or resolve_catalog_match_llm()
        listagem = "\n".join(_describe(c, evidencia=(evidencias or {}).get(c.entity_key)) for c in candidates)
        # A categoria do PEDIDO, que antes não era enviada. Sem ela o modelo
        # via a categoria dos candidatos e não tinha contra o que comparar:
        # medido em 24/09/2026, `jaguar` em Wildlife foi fundido com `jaguar`
        # em Automotive porque o texto batia, e a resposta dizia literalmente
        # "exact name match with the existing 'jaguar' entry in Automotive".
        linha_categoria = f"Category stated for this query: {category}\n" if category else ""
        prompt = (
            f"Product name to resolve: {text}\n"
            f"{linha_categoria}\n"
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

        relacao = (decision.relationship or "none").strip().lower()
        if relacao not in ("same", "subtype"):
            return None

        escolhido = decision.entity_key.strip()
        for candidate in candidates:
            if str(candidate.entity_key) == escolhido:
                if motivo is not None:
                    # A RELAÇÃO viaja junto com o motivo: quem chama precisa
                    # saber se reaproveita a entidade ou cria uma nova
                    # pendurada nela, e isso não se deduz da entidade sozinha.
                    motivo["relationship"] = relacao
                    if decision.reasoning:
                        motivo["reasoning"] = decision.reasoning
                    if relacao == "same" and decision.antagonist_terms:
                        motivo["antagonist_terms"] = decision.antagonist_terms
                rotulo = "é o mesmo produto" if relacao == "same" else "é sub-produto de"
                logger.info(
                    f"Match de catálogo: {text!r} {rotulo} {candidate.canonical_title!r} ({decision.reasoning})"
                )
                return candidate

        # Chave alucinada: o modelo devolveu algo que não estava na lista.
        # Tratar como sem match — nunca cair para "o mais parecido", que
        # reintroduziria a decisão por semelhança que este passo substituiu.
        logger.warning(f"LLM devolveu entity_key desconhecida {escolhido!r}; tratando como sem match")
        return None

    async def review_edits(
        self, product: "ProductCatalog", changes: dict[str, tuple[str | None, str]]
    ) -> dict[str, tuple[bool, str]]:
        """Para cada campo editado: (aceita?, porquê). Sem modelo, nada é aceito
        — o catálogo fica como estava, que é o erro que não se espalha."""
        if not changes:
            return {}
        provider = self._provider or resolve_catalog_match_llm()
        linhas = "\n".join(f"- {campo}: {antes or '(empty)'} -> {depois}" for campo, (antes, depois) in changes.items())
        prompt = f"Product:\n{_describe(product)}\n\nChanges the user made:\n{linhas}"
        try:
            review = await provider.complete(
                system=prompt_text("catalog-edit-review-system", _EDIT_REVIEW_SYSTEM),
                user=prompt,
                response_model=EditReview,
            )
        except LLMError as exc:
            logger.warning(f"LLM de conferência de edição indisponível; nenhuma edição aplicada: {exc}")
            return {campo: (False, "the review could not run") for campo in changes}
        vereditos = {v.field: (v.accept, v.reason) for v in getattr(review, "verdicts", []) if v.field in changes}
        return {campo: vereditos.get(campo, (False, "no verdict returned")) for campo in changes}
