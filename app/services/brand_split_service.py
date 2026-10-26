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

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.core.logging import logger
from app.repositories.product_repository import ProductRepository
from app.utils.text import normalize_alias


@dataclass
class SplitReport:
    """O que o reparo moveu. Uma entidade só, porque desde 24/09/2026 a marca
    não cria entidade — o que se separa é o vocabulário."""

    entity_key: uuid.UUID
    generic_title: str
    brand: str
    mercados_movidos: list[str] = field(default_factory=list)
    aliases_criados: int = 0


class BrandSplitService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ProductRepository(session)

    async def split(self, *, entity_key: uuid.UUID, brand: str, generic_title: str | None = None) -> SplitReport:
        """Devolve a entidade à categoria, e o vocabulário à marca.

        Desde 24/09/2026 não há entidade de marca para criar: o catálogo tem um
        produto por identidade e a marca é vínculo. Então o reparo deixou de
        fatiar a entidade em duas e passou a mover só o que estava no lugar
        errado — os termos curados, que são da marca e estavam pendurados no
        nome da categoria.

        A entidade CONTINUA sendo o produto. Não é aposentada, não há alias
        para repontar, e o histórico pendurado nela (indicator_values,
        mos_results) segue de pé sem nenhum ponteiro novo.
        """
        poluida = await self._repo.get_by_entity_key(entity_key)
        if poluida is None:
            raise EntityNotFoundError(f"Entity {entity_key} not found")
        if poluida.merged_into is not None:
            raise MarketMindCoreError(f"Entity {entity_key} já foi aposentada em {poluida.merged_into}")
        if not normalize_alias(brand):
            raise MarketMindCoreError(f"Marca {brand!r} não tem forma normalizada")

        marca = await self._repo.get_or_create_brand(name=brand, source="brand_split")
        titulo_generico = (generic_title or poluida.canonical_title).strip()
        mercados = dict(poluida.localized_markets or {})

        # O vocabulário vai para a marca, chaveado por (produto, marca, país).
        await self._repo.save_brand_markets(poluida.entity_key, marca.brand_key, mercados)

        report = SplitReport(
            entity_key=poluida.entity_key,
            generic_title=titulo_generico,
            brand=marca.name,
            mercados_movidos=sorted(mercados.keys()),
        )

        # O nome volta a ser o da categoria, e o alias dele tem de existir: sem
        # ele a próxima resolução do genérico cria duplicata, que é o erro que
        # este reparo existe para consertar.
        poluida.canonical_title = titulo_generico
        report.aliases_criados += await self._repo.create_aliases_bulk(
            aliases={normalize_alias(titulo_generico): poluida.entity_key}, source="brand_split"
        )

        # Os termos excluídos ficam onde estão: pendurados no produto, eles
        # vetam a fusão de produtos VIZINHOS, que é pergunta do produto e não
        # da marca.

        # O campeão foi eleito com a demanda DA MARCA, então não vale para o
        # produto. Apagado em vez de herdado: um campeão errado é pior que
        # nenhum, porque faz a próxima análise pular a coleta que o corrigiria.
        await self._repo.forget_champion(poluida, None, None)

        poluida.localized_markets = None
        poluida.reference_candidates = None
        await self._session.flush()
        await self._session.commit()

        logger.warning(
            f"Entidade {poluida.entity_key} devolvida à categoria {titulo_generico!r}: "
            f"{len(report.mercados_movidos)} mercados foram para a marca {report.brand!r}"
        )
        return report
