"""Fase 2: product catalog resolution pipeline (docs/marketmind-implementation-plan.md
section 6). Cheapest-to-most-expensive match, in order:

1. Exact normalized alias (product_aliases) — O(1) index lookup.
2. Fuzzy trigram (pg_trgm) over existing aliases.
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

O passo 3 era embedding até 2026-08-14 e passou a ser o LLM. Motivo medido: os
~20% que chegam lá não têm parentesco de escrita com nada indexado
(`lockencreme`, `odżywka bez spłukiwania`) ou são sinônimo de domínio
(`quadcopter` para drone) — e nenhum threshold de distância vetorial separa
"mesmo produto" de "produto vizinho" neste catálogo (`freidora sin aceite` e
`freidora con aceite` ficam a 0,033). O passo 3 é best-effort: se o LLM
estiver indisponível, a resolução cai para a criação de entidade nova, que é
o erro recuperável.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.product_catalog import ProductCatalog
from app.repositories.product_repository import ProductRepository
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

        existing = await self._repo.get_entity_by_alias_exact(alias_normalized)
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
            await self._index_localized_terms(entity, localized_markets)
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="alias"
            )

        trigram = await self._repo.trigram_search(alias_normalized)
        if trigram is not None:
            await self._repo.create_alias(
                alias_normalized=alias_normalized, entity_key=trigram.product.entity_key, source=source
            )
            self._refresh_entity_fields(
                trigram.product,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._index_localized_terms(trigram.product, localized_markets)
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=trigram.product.entity_key,
                canonical_title=trigram.product.canonical_title,
                method="trigram",
                match_score=trigram.similarity,
            )

        # Passo 3: o LLM decide. Só chega aqui o que alias e trigram não
        # resolveram — medido contra o catálogo real, ~20% das consultas, e
        # sempre pelo mesmo motivo: o termo não tem parentesco de escrita com
        # nada indexado (`lockencreme`, `odżywka bez spłukiwania`) ou é
        # sinônimo de domínio (`quadcopter` para drone). Substituiu o passo de
        # embedding, que não decidia esses casos: ver catalog_matcher.py.
        candidatos = await self._repo.list_match_candidates(category=category)
        correspondente = await self._matcher.pick(text=text, candidates=candidatos)
        if correspondente is not None:
            await self._repo.create_alias(
                alias_normalized=alias_normalized, entity_key=correspondente.entity_key, source=source
            )
            self._refresh_entity_fields(
                correspondente,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._index_localized_terms(correspondente, localized_markets)
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
        await self._repo.create_alias(alias_normalized=alias_normalized, entity_key=entity.entity_key, source=source)
        await self._index_localized_terms(entity, localized_markets)
        await self._session.commit()
        return CatalogResolveResponse(
            entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="created"
        )

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
        criados = 0
        for key in candidatos:
            dono = donos.get(key)
            if dono is not None:
                if dono != entity.entity_key:
                    logger.info(
                        f"Termo localizado {key!r} já é alias de outra entidade ({dono}); mantido"
                    )
                continue
            await self._repo.create_alias(
                alias_normalized=key, entity_key=entity.entity_key, source="localized_market"
            )
            criados += 1
        return criados

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

