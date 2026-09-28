"""Fase 2: product catalog resolution pipeline (docs/marketmind-implementation-plan.md
section 6). Cheapest-to-most-expensive match, in order:

1. Exact normalized alias (product_aliases) — O(1) index lookup.
2. Fuzzy trigram (pg_trgm) over existing aliases, ARBITRADO pela evidência
   negativa: a entidade só é aceita se o texto estiver mais perto dos termos
   que ela declara SER (search_terms) do que dos que declara NÃO ser
   (excluded_terms). Empate ou inversão veta a entidade e escala para o 3.
3. LLM decides among the catalog's candidate entities — see catalog_matcher.py.
4. Nothing matched — create a new entity locally (entity_key is always a
   freshly generated UUID, never blocked by an external dependency).

Um match nos passos 2 ou 3 grava um alias exato apontando para a entidade, de
modo que o mesmo texto resolve pelo caminho O(1) na próxima vez.

Os quatro passos rodam dentro de UMA PARTIÇÃO DE MARCA (2026-08-27, migration
031): a identidade do catálogo é o par (nome genérico, marca), e `brand_key
NULL` é a partição genérica. O que os passos 2 e 3 medem continua sendo só o
nome genérico — a marca nunca entra na comparação de semelhança —, mas medem
contra as entidades da mesma marca. Um pedido com marca resolve DUAS entidades:
a família genérica e a filha da marca, que é quem recebe o vocabulário curado e
quem a resposta devolve. Ver `resolve` para o caso medido que motivou.

Os `search_terms` de cada mercado também viram aliases (ver
_index_localized_terms). Isso é o que faz o passo 2 alcançar outros idiomas: o
LLM de enrichment já traduziu o produto para cada mercado, e indexar esses
termos põe esse vocabulário ao alcance do trigram. Medido contra os 89 termos
reais do catálogo (2026-08-14): 71 resolvem no passo 2, com precisão de 100%
no limiar 0.4 configurado.

O VETO do passo 2 entrou em 2026-08-25, depois de medir o passo contra os
`excluded_terms` do próprio catálogo — o conjunto negativo que existia desde a
Fase 5 e nunca tinha sido usado nem como guarda nem como teste. Os 89 termos
medidos em 14/08 eram todos positivos, então a "precisão de 100%" media só
metade do problema. Com 9 entidades, 286 aliases e 102 termos excluídos reais:
44 dos 102 (43,1%) resolviam para a entidade que os exclui — `fritadeira de
óleo` -> air fryer a 0.696 — e gravavam alias exato permanente. Com o veto,
0/102, sem perder nenhum positivo (284/284); numa consulta inédita o recall
vai de 94,7% para 93,0%. Ver product_excluded_term.py.

O passo 3 era embedding até 2026-08-14 e passou a ser o LLM. Motivo medido: os
~20% que chegam lá não têm parentesco de escrita com nada indexado
(`lockencreme`, `odżywka bez spłukiwania`) ou são sinônimo de domínio
(`quadcopter` para drone) — e nenhum threshold de distância vetorial separa
"mesmo produto" de "produto vizinho" neste catálogo (`freidora sin aceite` e
`freidora con aceite` ficam a 0,033). O passo 3 é best-effort: se o LLM
estiver indisponível, a resolução cai para a criação de entidade nova, que é
o erro recuperável.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.brand import Brand
from app.db.models.product_alias import ProductAlias
from app.db.models.product_catalog import ProductCatalog
from app.repositories.product_repository import ProductRepository, TrigramOutcome
from app.schemas.catalog import CatalogResolveResponse
from app.services.catalog_matcher import CatalogMatcher
from app.utils.text import as_term_list, normalize_alias


def _union_terms(*lists: list[str] | None) -> list[str]:
    """Case-insensitive union preserving first-seen order (existing terms first).
    Curated keywords are ACCUMULATED, never replaced — a re-identify only appends
    genuinely new terms, keeping the seed order (and thus the demand cache) stable."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for term in lst or []:
            if not term:
                continue
            key = str(term).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(term)
    return out


def _merge_localized_markets(
    existing: dict[str, dict[str, Any]] | None, incoming: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Merge per-country search vocabularies: union search_terms/excluded_terms
    (existing first + new appended), so nothing curated is ever lost and only new
    terms are added to the base."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (existing or {}).items()}
    for iso3, new in (incoming or {}).items():
        cur = merged.get(iso3)
        if not cur:
            merged[iso3] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        merged[iso3] = {
            "primary_name": cur.get("primary_name") or new.get("primary_name"),
            "formal_name": cur.get("formal_name") or new.get("formal_name"),
            "search_terms": _union_terms(cur.get("search_terms"), new.get("search_terms"), primary),
            "excluded_terms": _union_terms(cur.get("excluded_terms"), new.get("excluded_terms")),
            "confidence": max(float(cur.get("confidence") or 0.0), float(new.get("confidence") or 0.0)),
        }
    return merged


def _merge_reference_candidates(
    existing: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Merge reference-market candidates by country (union of terms), same
    accumulate-never-replace rule as localized_markets."""
    by_country: dict[str | None, dict[str, Any]] = {c.get("country"): dict(c) for c in (existing or [])}
    for new in incoming or []:
        cur = by_country.get(new.get("country"))
        if not cur:
            by_country[new.get("country")] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        cur["search_terms"] = _union_terms(cur.get("search_terms"), new.get("search_terms"), primary)
        cur["excluded_terms"] = _union_terms(cur.get("excluded_terms"), new.get("excluded_terms"))
        cur["primary_name"] = cur.get("primary_name") or new.get("primary_name")
    return list(by_country.values())


@dataclass
class _Resolvido:
    """Saída de uma resolução dentro de uma partição: a entidade, por qual dos
    quatro passos ela foi encontrada, e — quando é entidade de marca — a marca e
    a família, que o chamador expõe separadas em vez de fatiar o título."""

    entity: ProductCatalog
    method: str
    match_score: float | None = None
    # Por que o LLM disse que é o mesmo produto. Só existe no passo `llm`: é a
    # evidência da coincidência por SIGNIFICADO, que nenhum número expressa —
    # `quadcopter`~`drone` não tem semelhança de escrita nenhuma.
    match_reasoning: str | None = None
    marca: Brand | None = None
    familia: ProductCatalog | None = None


def _categorias_batem(guardada: str | None, pedida: str | None) -> bool:
    """Se as duas categorias podem ser do mesmo produto.

    Desconhecida de um dos lados não contradiz nada — a categoria é opcional e
    vem de um modelo, então ausência nunca pode virar recusa. O que impede é
    afirmação CONTRA afirmação.
    """
    if not guardada or not pedida:
        return True
    return guardada.strip().casefold() == pedida.strip().casefold()


def _difere_em_qualificacao(consulta: str, titulo_casado: str) -> bool:
    """Se um dos dois nomes é o outro MAIS alguma qualificação, em qualquer
    direção.

    `instant coffee` contra `coffee`: mesmas palavras e mais uma. Semelhança de
    forma diz que são o mesmo; produto costuma dizer que não.

    Palavra a palavra e não substring, senão `airfryer` contra `air fryer`
    cairia aqui — e esses dois são o mesmo produto escrito de dois jeitos, que
    é justamente o que o passo 2 existe para resolver sozinho.

    OS DOIS LADOS passam pelo MESMO normalizador. A primeira versão comparava a
    consulta normalizada com o título cru, e `ford focus ST-Line X` contra
    `ford focus ST-Line` não disparava: de um lado `st-line` era um token, do
    outro eram `st` e `line`. A verificação existia e nunca via o caso que ela
    foi escrita para ver.

    NOS DOIS SENTIDOS desde 27/09/2026. Só olhava a consulta mais específica
    que o casado, e o sentido contrário é pior: medido contra o catálogo de
    dev, `olive oil` casou por trigram com `extra virgin olive oil` — o
    produto AMPLO engolido por um sub-produto seu, e a demanda de todo o azeite
    passaria a ser gravada contra o extravirgem. Forma não decide nenhum dos
    dois sentidos; os dois vão para o passo 3.
    """
    da_consulta = set(normalize_alias(consulta).split())
    do_casado = set(normalize_alias(titulo_casado).split())
    if not do_casado or not da_consulta:
        return False
    return do_casado < da_consulta or da_consulta < do_casado


def _resposta(resolvido: _Resolvido) -> CatalogResolveResponse:
    return CatalogResolveResponse(
        entity_key=resolvido.entity.entity_key,
        canonical_title=resolvido.entity.canonical_title,
        brand=resolvido.marca.name if resolvido.marca is not None else None,
        brand_key=resolvido.marca.brand_key if resolvido.marca is not None else None,
        generic_title=resolvido.familia.canonical_title if resolvido.familia is not None else None,
        method=resolvido.method,
        match_score=resolvido.match_score,
        match_reasoning=resolvido.match_reasoning,
    )


class CatalogService:
    def __init__(self, session: AsyncSession, matcher: CatalogMatcher | None = None) -> None:
        self._session = session
        self._repo = ProductRepository(session)
        self._matcher = matcher or CatalogMatcher()

    async def resolve(
        self,
        text: str,
        *,
        source: str,
        brand: str | None = None,
        category: str | None = None,
        localized_markets: dict[str, dict[str, Any]] | None = None,
        reference_candidates: list[dict[str, Any]] | None = None,
        target_age_min: int | None = None,
        target_age_max: int | None = None,
    ) -> CatalogResolveResponse:
        """`text` é o NOME GENÉRICO do produto, sem marca. A marca vem separada,
        e desde 24/09/2026 ela NÃO cria entidade: o catálogo tem um produto por
        identidade — `coffee`, nunca `Juan Valdez coffee` — e a marca é um
        vínculo de quem pergunta, gravado na análise.

        POR QUE MUDOU. A marca particionava a identidade, e o mesmo produto
        passava a existir em dois lugares. Medido em 24/09/2026: três análises
        do mesmo café geraram coleta na DataForSEO em BOL, BRA e COL DUAS
        VEZES, uma por entidade, porque cada uma comprava o próprio
        vocabulário. E o resolvedor trocava os dois — pedir `coffee` devolvia a
        entidade da marca e pedir `Juan Valdez coffee` devolvia a genérica —,
        cada acerto gravando um alias que tornava o erro permanente.

        O QUE A PARTIÇÃO PROTEGIA CONTINUA PROTEGIDO, em outro lugar. Ela
        nasceu de uma medição de 27/08/2026: sem ela, `single malt Scotch
        whisky` + Cardhu e + Glenfiddich caíam na mesma entidade e a segunda
        marca herdava o vocabulário e a demanda da primeira. Agora o
        vocabulário é que tem dono — (produto, marca, país) em `brand_markets`
        — então as duas marcas dividem o produto e nada mais. "Juan Valdez
        coffee" e "coffee" nunca medem a mesma coisa, porque a busca no Google
        Ads é outra.

        O que volta na resposta é o PRODUTO, com a marca ao lado.
        """
        marca: Brand | None = None
        if brand and brand.strip():
            if normalize_alias(brand):
                marca = await self._repo.get_or_create_brand(name=brand, source=source)
            else:
                # Marca sem forma normalizada (só pontuação/emoji) não pode ser
                # chave de nada — mesma regra dos aliases (migration 028). O
                # produto resolve como genérico em vez de falhar.
                logger.info(f"Marca {brand!r} não tem forma normalizada; resolvendo como produto genérico")

        # UM produto, com ou sem marca. O vocabulário curado é que se separa:
        # sem marca ele é do produto; com marca vai para `brand_markets` e não
        # encosta no produto, senão a marca contamina a categoria — que é
        # exatamente o estrago de 27/08/2026.
        resolvido = await self._resolver_particao(
            text,
            marca=None,
            familia=None,
            source=source,
            category=category,
            localized_markets=None if marca is not None else localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
        )
        if marca is not None:
            resolvido.marca = marca
            if localized_markets:
                await self._repo.save_brand_markets(
                    resolvido.entity.entity_key, marca.brand_key, localized_markets
                )
        # A família vem dos passos que a DECIDEM; quem apenas reencontra a
        # entidade (alias, trigram) não a traz. Carregada aqui para que a
        # segunda resolução de um sub-produto devolva o mesmo contexto de
        # categoria que a primeira — medido em 27/09/2026: `instant coffee`
        # voltava sem pai por alias, tendo `coffee` gravado no banco, e o
        # enriquecimento então lia a categoria sem o produto que a define.
        if resolvido.familia is None and resolvido.entity.generic_entity_key is not None:
            resolvido.familia = await self._repo.get_by_entity_key(resolvido.entity.generic_entity_key)
        await self._session.commit()
        return _resposta(resolvido)

    async def _resolver_particao(
        self,
        text: str,
        *,
        marca: Brand | None,
        familia: ProductCatalog | None,
        source: str,
        category: str | None,
        localized_markets: dict[str, dict[str, Any]] | None,
        reference_candidates: list[dict[str, Any]] | None,
        target_age_min: int | None,
        target_age_max: int | None,
    ) -> _Resolvido:
        """Os quatro passos da resolução, dentro de UMA partição de marca.

        Duas chaves diferentes, e a distinção é o coração do desenho:

          `chave`     identidade exata — `marca + nome` quando há marca. É o
                      que o passo 1 procura e o que fica gravado como alias.
          `generico`  o que MEDE semelhança nos passos 2 e 3. Nunca leva marca.

        Sem forma normalizada do nome genérico não existe chave: `marca + ""`
        degeneraria no alias da marca sozinha, e um alias `philips` resolveria
        qualquer produto Philips para esta entidade, por igualdade exata, no
        passo mais barato. É o estrago que a migration 028 limpou.
        """
        brand_key = marca.brand_key if marca is not None else None
        titulo = f"{marca.name} {text}".strip() if marca is not None else text
        generico = normalize_alias(text)
        chave = normalize_alias(titulo) if generico else ""

        # Alias exato apontando para FORA desta partição: artefato da regra
        # antiga, que gravava `marca + nome` na entidade genérica. Não pode ser
        # ignorado nem aceito — `alias_normalized` é único global, então
        # deixá-lo lá impediria a entidade certa de ter caminho O(1) e toda
        # resolução da marca cairia no passo 2/3, criando duplicata. Ver
        # ProductRepository.repoint_alias.
        alias_orfao: ProductAlias | None = None

        existente = await self._repo.get_entity_by_alias_exact(chave, category) if chave else None
        if existente is not None:
            alias_existente, entity = existente
            # Alias é igualdade de TEXTO, e texto igual não é produto igual.
            # Medido em 24/09/2026: `jaguar` em Automotive e `jaguar` em
            # Wildlife caíam na mesma entidade pelo caminho O(1) — o mais
            # barato, e por isso o que nada a jusante corrige.
            #
            # Categorias que se contradizem não resolvem aqui: a pergunta passa
            # a ser de SIGNIFICADO, e quem responde isso é o passo 3.
            # O alias gravado para ESTA categoria já é uma decisão sobre este
            # par (nome, categoria) — a chave desde a migration 051 —, mesmo que
            # a entidade guarde outra forma de dizer a categoria ('Home
            # appliances' contra 'Kitchen Appliances'). Recusá-lo mandava o
            # mesmo pedido ao LLM toda vez para reconfirmar o que já decidiu.
            # Igualdade estrita: um alias SEM categoria não decidiu nada sobre
            # esta — é o caso `jaguar`, que continua indo ao passo 3.
            alias_desta_categoria = bool(category and alias_existente.category) and (
                alias_existente.category.strip().casefold() == category.strip().casefold()
            )
            if (
                alias_desta_categoria or _categorias_batem(entity.category, category)
            ) and entity.brand_key == brand_key:
                await self._repo.bump_alias_hit(alias_existente)
                await self._atualizar(
                    entity,
                    category=category,
                    localized_markets=localized_markets,
                    reference_candidates=reference_candidates,
                    target_age_min=target_age_min,
                    target_age_max=target_age_max,
                )
                return _Resolvido(entity=entity, method="alias", marca=marca, familia=familia)
            if not _categorias_batem(entity.category, category):
                logger.info(
                    f"Alias {chave!r} aponta para {entity.canonical_title!r} ({entity.category!r}), e este pedido "
                    f"diz {category!r}; escalando para o LLM em vez de decidir por texto igual"
                )
            else:
                logger.warning(
                    f"Alias {chave!r} pertence a {entity.canonical_title!r}, que está em outra partição de marca; "
                    f"resolvendo na partição correta e repontando"
                )
                alias_orfao = alias_existente

        trigram = (
            await self._repo.trigram_search(generico, category=category, brand_key=brand_key)
            if generico
            else TrigramOutcome(match=None, vetoed=[])
        )
        if trigram.vetoed:
            # Semelhança de forma dizia "é o mesmo produto" e o catálogo diz o
            # contrário. Não decide por string aqui: manda para o passo 3 com a
            # entidade vetada garantidamente na lista.
            logger.info(
                f"Passo 2 vetado para {text!r} por excluded_terms de "
                f"{[p.canonical_title for p in trigram.vetoed]}; escalando para o LLM"
            )
        if trigram.match is not None and _difere_em_qualificacao(generico, trigram.match.product.canonical_title):
            # Um dos dois nomes é o outro mais uma qualificação, e qualificação
            # costuma ser outro produto. Medido em 24/09/2026: `instant coffee`
            # casava com `coffee` por semelhança de forma, e café instantâneo
            # não tem a demanda do café. Em 27/09/2026, no sentido contrário:
            # `olive oil` casava com `extra virgin olive oil`.
            #
            # Não decide o contrário aqui — só recusa decidir por FORMA. Quem
            # diz se `extra virgin olive oil` é `olive oil` é o passo 3.
            logger.info(
                f"Passo 2 recusado para {text!r}: difere de {trigram.match.product.canonical_title!r} por uma "
                f"qualificação; escalando para o LLM"
            )
        elif trigram.match is not None:
            entity = trigram.match.product
            await self._registrar_chave(chave, entity, source=source, alias_orfao=alias_orfao, category=category)
            await self._atualizar(
                entity,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            return _Resolvido(
                entity=entity, method="trigram", match_score=trigram.match.similarity, marca=marca, familia=familia
            )

        # Passo 3: o LLM decide, entre os candidatos da MESMA partição. Só
        # chega aqui o que alias e trigram não resolveram — medido contra o
        # catálogo real, ~20% das consultas, e sempre pelo mesmo motivo: o
        # termo não tem parentesco de escrita com nada indexado
        # (`lockencreme`, `odżywka bez spłukiwania`) ou é sinônimo de domínio
        # (`quadcopter` para drone). Ver catalog_matcher.py.
        candidatos = await self._repo.list_match_candidates(
            category=category, priority=trigram.vetoed, brand_key=brand_key
        )
        motivo: dict[str, str] = {}
        correspondente = await self._matcher.pick(
            text=text, candidates=candidatos, motivo=motivo, category=category
        )
        if correspondente is not None and motivo.get("relationship") == "subtype":
            # SUB-PRODUTO: entidade própria, pendurada na mais ampla.
            #
            # A terceira saída da resolução, de 25/09/2026. `instant coffee`
            # não é `coffee` — tem outra demanda, outro preço, outro comprador
            # — e também não é um produto sem relação com café. Forçado a
            # escolher entre os dois, o modelo respondia "o mesmo", e com
            # razão: a categoria cura `café solúvel` entre seus termos, porque
            # é isso que a torna categoria.
            #
            # O vínculo com o pai é o que dá contexto: a demanda do
            # sub-produto passa a ser lida contra a da categoria, do mesmo
            # jeito que a de uma marca.
            entity = await self._repo.create_entity(
                canonical_title=titulo,
                category=category or correspondente.category,
                localized_markets=None if marca is not None else localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
                generic_entity_key=correspondente.entity_key,
            )
            await self._registrar_chave(
                chave, entity, source=source, alias_orfao=alias_orfao, category=category
            )
            await self._index_terms(entity, localized_markets)
            logger.info(
                f"Sub-produto criado: {titulo!r} sob {correspondente.canonical_title!r} "
                f"({motivo.get('reasoning')})"
            )
            return _Resolvido(
                entity=entity,
                method="subtype",
                match_reasoning=motivo.get("reasoning"),
                marca=marca,
                familia=correspondente,
            )

        if correspondente is not None:
            await self._registrar_chave(
                chave, correspondente, source=source, alias_orfao=alias_orfao, category=category
            )
            await self._atualizar(
                correspondente,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            return _Resolvido(
                entity=correspondente,
                method="llm",
                match_reasoning=motivo.get("reasoning"),
                marca=marca,
                familia=familia,
            )

        entity = await self._repo.create_entity(
            canonical_title=titulo,
            category=category,
            localized_markets=localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
            brand_key=brand_key,
            generic_entity_key=familia.entity_key if familia is not None else None,
        )
        await self._registrar_chave(chave, entity, source=source, alias_orfao=alias_orfao, category=category)
        await self._index_terms(entity, localized_markets)
        return _Resolvido(entity=entity, method="created", marca=marca, familia=familia)

    async def _atualizar(
        self,
        entity: ProductCatalog,
        *,
        category: str | None,
        localized_markets: dict[str, dict[str, Any]] | None,
        reference_candidates: list[dict[str, Any]] | None,
        target_age_min: int | None,
        target_age_max: int | None,
    ) -> None:
        """Reanálise refresca a entidade com o que o chamador trouxe e indexa o
        vocabulário novo — os dois sempre juntos, porque indexar sem refrescar
        deixaria alias apontando para termo que a entidade não declara."""
        self._refresh_entity_fields(
            entity,
            category=category,
            localized_markets=localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
        )
        await self._index_terms(entity, localized_markets)

    async def _registrar_chave(
        self,
        chave: str,
        entity: ProductCatalog,
        *,
        source: str,
        alias_orfao: ProductAlias | None,
        category: str | None = None,
    ) -> None:
        """Grava a chave desta partição como alias exato da entidade, para a
        próxima resolução sair pelo caminho O(1).

        A chave é (nome, categoria) desde a migration 051: o mesmo nome em
        outra categoria é outro produto, e cabem os dois."""
        if alias_orfao is not None:
            await self._repo.repoint_alias(alias_orfao, entity.entity_key)
            return
        await self._gravar_alias(chave, entity.entity_key, source, category or entity.category)

    async def _podar_termos_derivados(self, entity: ProductCatalog) -> int:
        """Remove da entidade os termos derivados que a curadoria atual não
        gera mais. As formas válidas são recalculadas do zero a partir de
        `localized_markets` e do `canonical_title`, com o normalizador vigente."""
        aliases_validos = {normalize_alias(entity.canonical_title)}
        excluidos_validos: set[str] = set()
        for market in (entity.localized_markets or {}).values():
            if not isinstance(market, dict):
                continue
            for termo in as_term_list(market.get("search_terms")):
                aliases_validos.add(normalize_alias(termo))
            for termo in as_term_list(market.get("excluded_terms")):
                excluidos_validos.add(normalize_alias(termo))
        aliases_validos.discard("")
        excluidos_validos.discard("")

        apagados = await self._repo.prune_derived_terms(
            entity_key=entity.entity_key, aliases=aliases_validos, excluidos=excluidos_validos
        )
        if apagados:
            logger.info(f"Podados {apagados} termos derivados obsoletos de {entity.canonical_title!r}")
        return apagados

    async def _gravar_alias(
        self, alias_normalized: str, entity_key: uuid.UUID, source: str, category: str | None = None
    ) -> None:
        """Grava o alias da consulta, a menos que ela não tenha forma normalizada.

        Texto só de pontuação ou emoji normaliza para vazio. Gravar isso como
        alias é a corrupção que motivou a migration 028: o alias vazio vira um
        ímã, e toda consulta sem forma normalizada passa a casar com ele pelo
        passo 1 — o caminho O(1), onde o veto do passo 2 nem roda.
        """
        if not alias_normalized:
            logger.info("Consulta sem forma normalizada (só pontuação/emoji); nenhum alias gravado")
            return
        await self._repo.set_alias(
            alias_normalized=alias_normalized, entity_key=entity_key, source=source, category=category
        )

    async def backfill_terms(self, *, batch_size: int = 100) -> dict[str, int]:
        """Indexa o vocabulário curado de TODAS as entidades já existentes.

        `_index_terms` só roda dentro do `resolve()`, então entidade criada
        antes dessas features nunca teve seus termos indexados. Medido no banco
        vivo em 2026-08-25: `product_aliases` tinha 9 linhas, uma por
        `canonical_title`, e nenhum dos 286 termos localizados do catálogo. Ou
        seja, tanto o alcance multilíngue do passo 2 quanto o risco de fusão
        que ele traz estavam dormentes.

        Os dois lados são preenchidos na mesma passada: rodar o backfill só dos
        positivos ligaria as 44 fusões medidas sem a guarda que as impede.

        RECONCILIA, não só acrescenta: o que a curadoria atual não produz mais
        é apagado (ver `_podar_termos_derivados`). Sem isso, uma mudança na
        regra de normalização deixa para trás aliases que ninguém consegue
        explicar — foi o que aconteceu com o normalizador antigo, que truncava
        termo de escrita mista e gravou dez marcas isoladas (`philips`,
        `ninja`, `cosori`) apontando para a air fryer.

        Em lotes, com commit por lote e paginação por chave. Uma transação
        única sobre o catálogo inteiro seguraria locks em `product_aliases`
        durante todo o backfill — e é a tabela do caminho quente de QUALQUER
        resolução, então uma análise concorrente ficaria esperando. Com lotes,
        uma interrupção também não perde o que já foi gravado: reexecutar é
        idempotente (`ON CONFLICT DO NOTHING`).
        """
        relatorio = {"entidades": 0, "aliases": 0, "excluidos": 0, "obsoletos": 0}
        ultima: uuid.UUID | None = None
        while True:
            entidades = await self._repo.list_entities_with_markets(after=ultima, limit=batch_size)
            if not entidades:
                break
            for entidade in entidades:
                relatorio["aliases"] += await self._index_localized_terms(entidade, entidade.localized_markets)
                relatorio["excluidos"] += await self._index_excluded_terms(entidade, entidade.localized_markets)
                relatorio["obsoletos"] += await self._podar_termos_derivados(entidade)
                relatorio["entidades"] += 1
            ultima = entidades[-1].entity_key
            await self._session.commit()
            logger.info(f"Backfill de termos: {relatorio['entidades']} entidades processadas")
        logger.info(f"Backfill de termos do catálogo concluído: {relatorio}")
        return relatorio

    async def _index_terms(self, entity: ProductCatalog, localized_markets: dict[str, dict[str, Any]] | None) -> None:
        """Indexa o vocabulário curado da entidade nos DOIS sinais.

        Positivos primeiro, negativos depois, e a ordem importa: um termo que a
        mesma entidade lista como `search_term` num mercado e como
        `excluded_term` noutro é uma contradição do dado (medida uma vez no
        catálogo real: `fritadeira elétrica`, positiva no BRA e negativa em
        PRT/ESP). Nesse empate o positivo vence — ele é evidência de que o
        termo RECUPERA o produto — e a contradição vai para o log em vez de
        virar um veto que derruba o próprio produto.
        """
        await self._index_localized_terms(entity, localized_markets)
        await self._index_excluded_terms(entity, localized_markets)

    async def _index_localized_terms(
        self, entity: ProductCatalog, localized_markets: dict[str, dict[str, Any]] | None
    ) -> int:
        """Grava os `search_terms` de cada mercado como aliases da entidade.

        POR QUE ISTO EXISTE. O LLM de enrichment já traduz o produto para cada
        mercado — `localized_markets` guarda o termo em espanhol, português,
        alemão e assim por diante. Mas esses termos ficavam só no JSONB, e o
        passo 2 (trigram) busca em `product_aliases`. O resultado é que o
        sistema possuía o vocabulário multilíngue e não conseguia usá-lo:
        resolver "freidora de aire" falhava mesmo com esse termo gravado na
        entidade da air fryer.

        Indexá-los fecha essa lacuna sem nenhuma dependência nova. Medido
        contra os 89 termos reais do catálogo (2026-08-14), com o
        `similarity()` do pg_trgm e o limiar 0.4 já configurado: 71 dos 89
        casos resolvem acima do limiar e TODOS os 71 estão corretos —
        precisão de 100% no ponto de operação atual. Os 18 restantes ficam
        abaixo do limiar e seguem para os passos seguintes.

        O conhecimento de mundo continua necessário uma vez por produto, na
        identificação. Depois disso, recuperar é comparação de string.

        Conflito: `alias_normalized` é único globalmente. Um termo que já
        pertence a OUTRA entidade é deixado como está, nunca reapontado — o
        mapeamento existente veio de uma resolução anterior (possivelmente de
        uma consulta real de usuário, evidência mais forte que um termo
        gerado), e repontá-lo em silêncio trocaria a identidade de um produto
        já resolvido.
        """
        if not localized_markets:
            return 0

        # Deduplica por forma normalizada: 23 mercados repetem a marca
        # ("Skala Mais Crespinhos") em todos, e a normalização os colapsa.
        candidatos: dict[str, str] = {}
        for market in localized_markets.values():
            if not isinstance(market, dict):
                continue
            for term in as_term_list(market.get("search_terms")):
                key = normalize_alias(term)
                # Vazio acontece com escritas não-latinas (katakana, árabe):
                # normalize_alias remove tudo fora de [a-z0-9], então o termo
                # não tem representação e não pode virar alias.
                if key:
                    candidatos.setdefault(key, term)

        if not candidatos:
            return 0

        donos = await self._repo.existing_alias_owners(set(candidatos))
        novos: dict[str, uuid.UUID] = {}
        for key in candidatos:
            dono = donos.get(key)
            if dono is not None:
                if dono != entity.entity_key:
                    logger.info(f"Termo localizado {key!r} já é alias de outra entidade ({dono}); mantido")
                continue
            novos[key] = entity.entity_key
        return await self._repo.create_aliases_bulk(aliases=novos, source="localized_market")

    async def _index_excluded_terms(
        self, entity: ProductCatalog, localized_markets: dict[str, dict[str, Any]] | None
    ) -> int:
        """Grava os `excluded_terms` de cada mercado como evidência NEGATIVA.

        Eles nunca viram alias — não resolvem nada, só desqualificam candidato
        no passo 2 (ver ProductRepository.trigram_search). Sem isto, 44 dos 102
        termos excluídos do catálogo real resolviam para a entidade que os
        exclui: `fritadeira de óleo` casava com o alias `fritadeira sem óleo` a
        0.696, bem acima do limiar 0.4, e virava alias permanente da air fryer.

        Um termo excluído que já é alias DESTA entidade não é indexado: ver
        `_index_terms`. Já um termo excluído que é alias de OUTRA entidade é
        indexado normalmente — `panela de pressão elétrica` é excluída pela air
        fryer e é o produto legítimo da entidade "Electric pressure cooker", e
        as duas coisas são verdade ao mesmo tempo.
        """
        if not localized_markets:
            return 0

        candidatos: dict[str, str] = {}
        for market in localized_markets.values():
            if not isinstance(market, dict):
                continue
            for term in as_term_list(market.get("excluded_terms")):
                key = normalize_alias(term)
                # Vazio em escrita não-latina, como nos aliases: sem
                # representação normalizada, não há o que comparar.
                if key:
                    candidatos.setdefault(key, term)

        if not candidatos:
            return 0

        donos = await self._repo.existing_alias_owners(set(candidatos))
        novos: dict[str, str] = {}
        for key, surface in candidatos.items():
            if donos.get(key) == entity.entity_key:
                logger.warning(
                    f"Termo {surface!r} é search_term e excluded_term da mesma entidade "
                    f"({entity.canonical_title!r}); mantido como positivo e NÃO indexado como exclusão"
                )
                continue
            novos[key] = surface
        # Reindexar a mesma entidade é idempotente pelo ON CONFLICT, então não
        # há mais um SELECT de "o que já está indexado" antes de cada gravação.
        return await self._repo.create_excluded_terms_bulk(
            entity_key=entity.entity_key, termos=novos, source="localized_market"
        )

    @staticmethod
    def _refresh_entity_fields(
        entity: ProductCatalog,
        *,
        category: str | None,
        localized_markets: dict[str, dict[str, Any]] | None,
        reference_candidates: list[dict[str, Any]] | None,
        target_age_min: int | None,
        target_age_max: int | None,
    ) -> None:
        """Update an already-resolved entity with the fields a re-analysis
        brought, so its curated data stays current. Only NON-None values are
        applied — a text-only resolve (no markets) never wipes existing data.
        The entity is session-attached, so the surrounding commit persists it."""
        if category is not None:
            entity.category = category
        # Merge, never overwrite: curated keywords accumulate in the base; a
        # re-identify only adds genuinely new terms (keeps the demand cache stable).
        if localized_markets is not None:
            entity.localized_markets = _merge_localized_markets(entity.localized_markets, localized_markets)
        if reference_candidates is not None:
            entity.reference_candidates = _merge_reference_candidates(entity.reference_candidates, reference_candidates)
        if target_age_min is not None:
            entity.target_age_min = target_age_min
        if target_age_max is not None:
            entity.target_age_max = target_age_max
