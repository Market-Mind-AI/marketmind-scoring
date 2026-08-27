"""Separa uma entidade que virou marca sem admitir que era marca.

O ESTADO QUE ISTO CONSERTA, medido no banco vivo em 27/08/2026: a entidade
`single malt Scotch whisky` (23e1debc) tinha 24 mercados de vocabulário 100%
Cardhu e os aliases `cardhu`, `cardhu whisky`, `cardhu gold reserve`. O rótulo
dizia a categoria e o número media a marca. Pior: resolver o mesmo nome
genérico com `brand=Glenfiddich` devolvia `method: alias` para ESSA entidade,
pelo caminho O(1), e a segunda marca herdaria a demanda da primeira.

A partição da identidade (migration 031) impede o estado novo, e não desfaz o
antigo. É o que esta operação faz, e ela é o único caminho: `resolve()` não
conserta sozinho, porque a resolução da família cairia justamente na entidade
poluída — ela é dona do alias genérico — e a categoria passaria a ser medida
com os termos da marca.

TRÊS ENTIDADES DEPOIS, uma por papel:

  família     `single malt Scotch whisky`, LIMPA, sem vocabulário nenhum. Dona
              do alias genérico. É ela que o enriquecimento vai preencher com
              termos sem marca, e contra ela que o share da marca é lido.
  marca       `Cardhu single malt Scotch whisky`, com TODO o vocabulário
              curado, os termos excluídos, a categoria, a faixa de idade e o
              campeão — porque tudo isso foi produzido a partir da marca.
  aposentada  a original, com `merged_into` apontando para a entidade da marca.
              Não é apagada: `indicator_values` e `mos_results` continuam
              pendurados nela, então os relatórios já entregues seguem
              abrindo. O que ela perde são os aliases.

A REGRA DE QUEM FICA COM CADA ALIAS é o que torna isto automatizável: o alias
igual ao título normalizado da família é da família; todo o resto pertencia ao
vocabulário poluído, que é da marca. Nada é descartado.

O que NÃO é movido, de propósito: `indicator_values` e `mos_results`. Eles são
o histórico do que já foi entregue e continuam legíveis na entidade aposentada;
a próxima análise recalcula os da entidade da marca a partir das linhas de
demanda que o coletor já tem em cache — sem custo novo no DataForSEO.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.core.logging import logger
from app.db.models.product_alias import ProductAlias
from app.db.models.product_excluded_term import ProductExcludedTerm
from app.repositories.product_repository import ProductRepository
from app.utils.text import normalize_alias


@dataclass
class SplitReport:
    brand_entity_key: uuid.UUID
    generic_entity_key: uuid.UUID
    retired_entity_key: uuid.UUID
    brand: str
    brand_title: str
    generic_title: str
    aliases_para_marca: int = 0
    aliases_para_familia: int = 0
    termos_excluidos_movidos: int = 0
    mercados_movidos: list[str] = field(default_factory=list)


class BrandSplitService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ProductRepository(session)

    async def split(self, *, entity_key: uuid.UUID, brand: str, generic_title: str | None = None) -> SplitReport:
        poluida = await self._repo.get_by_entity_key(entity_key)
        if poluida is None:
            raise EntityNotFoundError(f"Entity {entity_key} not found")
        if poluida.brand_key is not None:
            raise MarketMindCoreError(
                f"Entity {entity_key} já está numa partição de marca; separar de novo criaria uma terceira entidade"
            )
        if poluida.merged_into is not None:
            raise MarketMindCoreError(f"Entity {entity_key} já foi aposentada em {poluida.merged_into}")
        if not normalize_alias(brand):
            raise MarketMindCoreError(f"Marca {brand!r} não tem forma normalizada")

        marca = await self._repo.get_or_create_brand(name=brand, source="brand_split")
        titulo_generico = (generic_title or poluida.canonical_title).strip()
        titulo_marca = f"{marca.name} {titulo_generico}".strip()

        # A família nasce limpa: sem localized_markets, sem reference_candidates.
        # O vocabulário que existe hoje é da marca, e é para lá que ele vai.
        familia = await self._repo.create_entity(canonical_title=titulo_generico, category=poluida.category)
        entidade_marca = await self._repo.create_entity(
            canonical_title=titulo_marca,
            category=poluida.category,
            localized_markets=poluida.localized_markets,
            reference_candidates=poluida.reference_candidates,
            target_age_min=poluida.target_age_min,
            target_age_max=poluida.target_age_max,
            brand_key=marca.brand_key,
            generic_entity_key=familia.entity_key,
        )
        # O campeão foi eleito com a demanda da marca, então é dela.
        if poluida.champion_period_key is not None:
            await self._repo.set_champion(
                entidade_marca,
                country=poluida.champion_country,
                mos=poluida.champion_mos,
                period_key=poluida.champion_period_key,
            )

        report = SplitReport(
            brand_entity_key=entidade_marca.entity_key,
            generic_entity_key=familia.entity_key,
            retired_entity_key=poluida.entity_key,
            brand=marca.name,
            brand_title=titulo_marca,
            generic_title=titulo_generico,
            mercados_movidos=sorted((poluida.localized_markets or {}).keys()),
        )

        chave_generica = normalize_alias(titulo_generico)
        for alias in list(poluida.aliases):
            destino = familia.entity_key if alias.alias_normalized == chave_generica else entidade_marca.entity_key
            alias.entity_key = destino
            if destino == familia.entity_key:
                report.aliases_para_familia += 1
            else:
                report.aliases_para_marca += 1

        # As DUAS chaves de identidade têm que existir depois do reparo, senão
        # ele deixa a próxima resolução criar duplicata — o erro que ele existe
        # para consertar. `marca + nome` pode não existir ainda (a regra antiga
        # só a gravava quando a marca chegava pela identificação) e o alias
        # genérico pode ter ficado de fora se a entidade nasceu por outro
        # caminho. ON CONFLICT DO NOTHING, então quem já tem dono é respeitado.
        report.aliases_para_marca += await self._repo.create_aliases_bulk(
            aliases={normalize_alias(titulo_marca): entidade_marca.entity_key}, source="brand_split"
        )
        report.aliases_para_familia += await self._repo.create_aliases_bulk(
            aliases={chave_generica: familia.entity_key}, source="brand_split"
        )

        # Os termos excluídos são o lado negativo do MESMO vocabulário curado.
        movidos = await self._session.execute(
            update(ProductExcludedTerm)
            .where(ProductExcludedTerm.entity_key == poluida.entity_key)
            .values(entity_key=entidade_marca.entity_key)
        )
        report.termos_excluidos_movidos = movidos.rowcount or 0

        # Aposentada, não apagada: o histórico já entregue continua pendurado
        # nela (indicator_values, mos_results) e segue abrindo.
        poluida.merged_into = entidade_marca.entity_key
        poluida.localized_markets = None
        poluida.reference_candidates = None
        await self._session.flush()
        await self._session.commit()

        logger.warning(
            f"Entidade {poluida.entity_key} separada: marca {report.brand!r} -> {entidade_marca.entity_key} "
            f"({report.aliases_para_marca} aliases, {len(report.mercados_movidos)} mercados), "
            f"família {titulo_generico!r} -> {familia.entity_key} ({report.aliases_para_familia} alias)"
        )
        return report

    async def aliases_de(self, entity_key: uuid.UUID) -> list[ProductAlias]:
        """Só para inspeção em teste — a leitura normal é get_by_entity_key."""
        entity = await self._repo.get_by_entity_key(entity_key)
        return list(entity.aliases) if entity is not None else []
