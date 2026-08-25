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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
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
        category: str | None = None,
        localized_markets: dict[str, dict[str, Any]] | None = None,
        reference_candidates: list[dict[str, Any]] | None = None,
        target_age_min: int | None = None,
        target_age_max: int | None = None,
    ) -> CatalogResolveResponse:
        alias_normalized = normalize_alias(text)

        # Sem forma normalizada não há o que casar por string: alias exato e
        # trigram são pulados e a decisão vai direto para o LLM, que lê o texto
        # original. Antes, `get_entity_by_alias_exact("")` casava com o alias
        # vazio de uma entidade anterior e fundia produtos sem relação.
        existing = await self._repo.get_entity_by_alias_exact(alias_normalized) if alias_normalized else None
        if existing is not None:
            existing_alias, entity = existing
            await self._repo.bump_alias_hit(existing_alias)
            # Re-analysis refreshes the entity with any fields the caller brought
            # (identify-image / analyze pass-through), so a product's markets and
            # reference candidates stay current instead of being frozen at creation.
            self._refresh_entity_fields(
                entity,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._index_terms(entity, localized_markets)
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="alias"
            )

        trigram = (
            await self._repo.trigram_search(alias_normalized)
            if alias_normalized
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
        if trigram.match is not None:
            await self._gravar_alias(alias_normalized, trigram.match.product.entity_key, source)
            self._refresh_entity_fields(
                trigram.match.product,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._index_terms(trigram.match.product, localized_markets)
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=trigram.match.product.entity_key,
                canonical_title=trigram.match.product.canonical_title,
                method="trigram",
                match_score=trigram.match.similarity,
            )

        # Passo 3: o LLM decide. Só chega aqui o que alias e trigram não
        # resolveram — medido contra o catálogo real, ~20% das consultas, e
        # sempre pelo mesmo motivo: o termo não tem parentesco de escrita com
        # nada indexado (`lockencreme`, `odżywka bez spłukiwania`) ou é
        # sinônimo de domínio (`quadcopter` para drone). Substituiu o passo de
        # embedding, que não decidia esses casos: ver catalog_matcher.py.
        candidatos = await self._repo.list_match_candidates(category=category, priority=trigram.vetoed)
        correspondente = await self._matcher.pick(text=text, candidates=candidatos)
        if correspondente is not None:
            await self._gravar_alias(alias_normalized, correspondente.entity_key, source)
            self._refresh_entity_fields(
                correspondente,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._index_terms(correspondente, localized_markets)
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=correspondente.entity_key,
                canonical_title=correspondente.canonical_title,
                method="llm",
            )

        entity = await self._repo.create_entity(
            canonical_title=text,
            category=category,
            localized_markets=localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
        )
        await self._gravar_alias(alias_normalized, entity.entity_key, source)
        await self._index_terms(entity, localized_markets)
        await self._session.commit()
        return CatalogResolveResponse(
            entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="created"
        )

    async def _gravar_alias(self, alias_normalized: str, entity_key: uuid.UUID, source: str) -> None:
        """Grava o alias da consulta, a menos que ela não tenha forma normalizada.

        Texto só de pontuação ou emoji normaliza para vazio. Gravar isso como
        alias é a corrupção que motivou a migration 028: o alias vazio vira um
        ímã, e toda consulta sem forma normalizada passa a casar com ele pelo
        passo 1 — o caminho O(1), onde o veto do passo 2 nem roda.
        """
        if not alias_normalized:
            logger.info("Consulta sem forma normalizada (só pontuação/emoji); nenhum alias gravado")
            return
        await self._repo.create_alias(
            alias_normalized=alias_normalized, entity_key=entity_key, source=source
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

        Em lotes, com commit por lote e paginação por chave. Uma transação
        única sobre o catálogo inteiro seguraria locks em `product_aliases`
        durante todo o backfill — e é a tabela do caminho quente de QUALQUER
        resolução, então uma análise concorrente ficaria esperando. Com lotes,
        uma interrupção também não perde o que já foi gravado: reexecutar é
        idempotente (`ON CONFLICT DO NOTHING`).
        """
        relatorio = {"entidades": 0, "aliases": 0, "excluidos": 0}
        ultima: uuid.UUID | None = None
        while True:
            entidades = await self._repo.list_entities_with_markets(after=ultima, limit=batch_size)
            if not entidades:
                break
            for entidade in entidades:
                relatorio["aliases"] += await self._index_localized_terms(entidade, entidade.localized_markets)
                relatorio["excluidos"] += await self._index_excluded_terms(entidade, entidade.localized_markets)
                relatorio["entidades"] += 1
            ultima = entidades[-1].entity_key
            await self._session.commit()
            logger.info(f"Backfill de termos: {relatorio['entidades']} entidades processadas")
        logger.info(f"Backfill de termos do catálogo concluído: {relatorio}")
        return relatorio

    async def _index_terms(
        self, entity: ProductCatalog, localized_markets: dict[str, dict[str, Any]] | None
    ) -> None:
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

