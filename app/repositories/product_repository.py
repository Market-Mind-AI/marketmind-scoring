import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy import true as sa_true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import logger
from app.db.models.brand import Brand
from app.db.models.product_alias import ProductAlias
from app.db.models.product_catalog import ProductCatalog
from app.db.models.product_excluded_term import ProductExcludedTerm
from app.utils.text import normalize_alias
from app.utils.time import utc_now


@dataclass
class TrigramMatch:
    product: ProductCatalog
    similarity: float


@dataclass
class TrigramOutcome:
    """As três saídas do passo 2, em vez das duas de antes.

    `match` aceito, `vetoed` desqualificado por evidência negativa, e nenhum
    dos dois quando simplesmente nada passou do limiar. Vetado NÃO vira
    entidade nova direto: vai para o passo 3 (o LLM), que é quem sabe ler as
    palavras. Ver ProductExcludedTerm para os números que motivaram isto.
    """

    match: TrigramMatch | None
    vetoed: list[ProductCatalog]


class ProductRepository:
    """Backs the resolution pipeline in app/services/catalog_service.py:
    exact alias -> trigram fuzzy (com veto por excluded_terms) -> LLM -> nova entidade,
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_alias_exact(self, alias_normalized: str) -> ProductAlias | None:
        stmt = select(ProductAlias).where(ProductAlias.alias_normalized == alias_normalized)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_entity_by_alias_exact(self, alias_normalized: str) -> tuple[ProductAlias, ProductCatalog] | None:
        stmt = (
            select(ProductAlias, ProductCatalog)
            .join(ProductCatalog, ProductCatalog.entity_key == ProductAlias.entity_key)
            .where(ProductAlias.alias_normalized == alias_normalized)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        return None if row is None else (row[0], row[1])

    async def get_by_entity_key(self, entity_key: uuid.UUID) -> ProductCatalog | None:
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.entity_key == entity_key)
            .options(selectinload(ProductCatalog.aliases))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def bump_alias_hit(self, alias: ProductAlias) -> None:
        alias.hit_count += 1
        await self._session.flush()

    async def trigram_search(
        self, alias_normalized: str, *, category: str | None = None, brand_key: uuid.UUID | None = None
    ) -> TrigramOutcome:
        """Passo 2, com a evidência negativa no mesmo julgamento.

        Para cada entidade que tem algum alias acima do limiar, compara a
        MELHOR semelhança positiva (search_terms indexados) com a MELHOR
        semelhança negativa (excluded_terms). A entidade só é elegível se o
        texto estiver mais perto do que ela diz que É do que do que ela diz
        que NÃO É. Sem limiar novo: é uma comparação entre as duas evidências.

        `fritadeira de óleo` bate 0.696 no alias `fritadeira sem óleo` e 1.000
        no termo excluído `fritadeira de óleo` — vetado. `fritadeira sem óleo`
        bate 1.000 no alias e 0.727 no excluído — aceito.

        POR QUE O OPERADOR `%` E NÃO `similarity(...) >= limiar`. Só o `%` usa
        os índices GIN trigram; a chamada direta a `similarity()` obriga o
        Postgres a calculá-la para CADA linha das duas tabelas. Medido em
        catálogo sintético de 2.000 produtos / 500.000 aliases / 16.000 termos
        excluídos: 300 ms com `similarity()` (dois Seq Scans) contra 4,6 ms com
        `%` — e a forma antiga cresce linearmente com o catálogo, porque é
        O(aliases) por consulta.

        O `%` compara contra `pg_trgm.similarity_threshold`, que é um GUC de
        sessão. Ele é fixado por TRANSAÇÃO (`set_config(..., is_local => true)`)
        e nunca por sessão: as conexões vêm de um pool, e um SET de sessão
        vazaria o limiar deste passo para todo o resto da aplicação.

        CATEGORIA DIVERGENTE TAMBÉM DESQUALIFICA. A identificação traz a
        categoria do produto e ela nunca era usada aqui — só para priorizar
        candidatos do passo 3. Mas quatro dos seis falsos positivos que
        sobraram na medição de 2026-08-25 são acessório, serviço ou conteúdo
        DO produto (`air fryer basket`, `air fryer repair`, `air fryer
        cookbook`, `空气炸锅配件`): compartilham o nome dele por construção, e
        por isso nenhuma medida de semelhança os separa. O que os separa é a
        categoria, que o sistema já recebe.

        O caso extremo mostra por que limiar não resolve: `air fryer basket`
        (a cesta) mede 1.000 contra o termo curado `basket air fryer` (o
        produto), porque o pg_trgm compara sacos de trigramas e IGNORA a ordem
        das palavras — para ele as duas strings são idênticas.

        A guarda só age quando as duas categorias são conhecidas: categoria
        ausente de um dos lados não é evidência de divergência. Categorias
        equivalentes escritas de formas diferentes ("Kitchen Appliances" x
        "Small Kitchen Appliances") custam uma ida ao passo 3, que é o erro
        barato de propósito.

        Filtrar o lado negativo pelo mesmo `%` é exato, não uma aproximação:
        vetar exige `sim_negativa >= sim_positiva`, e uma candidata só existe
        com `sim_positiva >= limiar`, logo nenhum termo excluído abaixo do
        limiar poderia vetar coisa alguma.

        A evidência negativa é comparada GLOBALMENTE, não só contra a entidade
        candidata. A razão veio de um caso real: `freidora de aceite` é termo
        excluído da air fryer, mas casava com a entidade "olive oil" a 0.400
        (via `aceite de oliva`), exatamente no limiar — o veto por entidade não
        via, porque quem excluía o termo não era a entidade que casou. Se o
        catálogo já declarou que aquele texto designa outro produto, aceitar
        uma semelhança mais fraca com uma terceira entidade é o erro caro.

        Medido: o veto global zera esses casos (4 -> 0 no catálogo real) e
        custa 1,3 ponto de escalonamento a mais (25,4% -> 26,7% das consultas
        indo ao passo 3). Como o máximo global é sempre >= o máximo da própria
        entidade, a regra global subsume a por-entidade e sobra uma comparação
        só.

        PARTIÇÃO DA MARCA. A busca acontece dentro de UMA partição: `brand_key`
        igual ao pedido, com NULL sendo a partição genérica (daí o
        `IS NOT DISTINCT FROM`, e não `=`, que nunca casa com NULL). O texto
        comparado continua sendo o NOME GENÉRICO — a marca nunca entra na
        medida de semelhança, porque é um token que todos os produtos daquela
        marca compartilham e foi ele que fundiu `philips air purifier` com o
        alias `philips airfryer` a 0.520 (ver 28ca6be). O que a partição muda é
        CONTRA QUEM comparar: dentro da Philips a medida volta a ser
        `air purifier` x `airfryer` = 0.278, abaixo do limiar.
        """
        limiar = settings.catalog_trigram_similarity_threshold
        # Vale só até o fim desta transação (is_local => true).
        await self._session.execute(select(func.set_config("pg_trgm.similarity_threshold", str(limiar), True)))

        pos_sim = func.similarity(ProductAlias.alias_normalized, alias_normalized)
        positivos = (
            select(ProductAlias.entity_key.label("entity_key"), func.max(pos_sim).label("sim"))
            .where(ProductAlias.alias_normalized.bool_op("%")(alias_normalized))
            .group_by(ProductAlias.entity_key)
            .subquery()
        )
        # Um agregado só, não correlacionado: a melhor evidência negativa do
        # catálogo inteiro para este texto. O `%` mantém a leitura no índice
        # GIN, então isto não varre a tabela de termos excluídos.
        sim_negativa = (
            select(func.coalesce(func.max(func.similarity(ProductExcludedTerm.term_normalized, alias_normalized)), 0.0))
            .where(ProductExcludedTerm.term_normalized.bool_op("%")(alias_normalized))
            .scalar_subquery()
        )
        # `merged_into` é filtrado no SELECT externo, e não dentro da agregação:
        # assim a entidade é alcançada uma vez por candidata, em vez de varrer o
        # catálogo inteiro para depois jogar quase tudo fora.
        stmt = (
            select(ProductCatalog, positivos.c.sim, sim_negativa.label("sim_negativa"))
            .join(positivos, positivos.c.entity_key == ProductCatalog.entity_key)
            .where(ProductCatalog.merged_into.is_(None))
            .where(ProductCatalog.brand_key.is_not_distinct_from(brand_key))
            .order_by(positivos.c.sim.desc())
        )
        result = await self._session.execute(stmt)

        categoria_consulta = normalize_alias(category) if category else ""

        melhor: TrigramMatch | None = None
        vetadas: list[ProductCatalog] = []
        for product, sim, melhor_negativa in result.all():
            # Candidata desqualificada vai para o passo 3 na lista de
            # prioridade, porque é justamente ela que o LLM tem de julgar.
            if float(melhor_negativa) >= float(sim):
                vetadas.append(product)
                continue
            categoria_entidade = normalize_alias(product.category) if product.category else ""
            if categoria_consulta and categoria_entidade and categoria_consulta != categoria_entidade:
                logger.info(
                    f"Categoria divergente para {alias_normalized!r}: consulta {category!r} x "
                    f"{product.canonical_title!r} {product.category!r}; escalando para o LLM"
                )
                vetadas.append(product)
                continue
            if melhor is None:
                melhor = TrigramMatch(product=product, similarity=float(sim))
        return TrigramOutcome(match=melhor, vetoed=vetadas)

    async def create_entity(
        self,
        *,
        canonical_title: str,
        category: str | None = None,
        localized_markets: dict[str, dict[str, Any]] | None = None,
        reference_candidates: list[dict[str, Any]] | None = None,
        target_age_min: int | None = None,
        target_age_max: int | None = None,
        brand_key: uuid.UUID | None = None,
        generic_entity_key: uuid.UUID | None = None,
    ) -> ProductCatalog:
        entity = ProductCatalog(
            entity_key=uuid.uuid4(),
            canonical_title=canonical_title,
            category=category,
            localized_markets=localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
            brand_key=brand_key,
            generic_entity_key=generic_entity_key,
            created_at=utc_now(),
        )
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def get_or_create_brand(self, *, name: str, source: str | None = None) -> Brand:
        """A marca do cadastro global, criada na primeira vez que é vista.

        A chave é a forma normalizada, então "Cardhu", "CARDHU" e "cardhu" são
        a mesma marca — sem isso o veto cruzado da demanda teria três listas
        para a mesma empresa. A grafia de superfície guardada é a primeira
        vista, que é a que o usuário lê.

        Marca cuja forma normalizada é vazia (texto só de pontuação ou emoji)
        não entra: é a mesma corrupção da migration 028, e a CHECK da tabela
        recusaria de todo jeito.
        """
        normalizada = normalize_alias(name)
        if not normalizada:
            raise ValueError(f"Marca {name!r} não tem forma normalizada; não pode ser cadastrada")

        existente = (
            await self._session.execute(select(Brand).where(Brand.name_normalized == normalizada))
        ).scalar_one_or_none()
        if existente is not None:
            return existente

        marca = Brand(
            brand_key=uuid.uuid4(),
            name=name.strip(),
            name_normalized=normalizada,
            first_seen_source=source,
            created_at=utc_now(),
        )
        self._session.add(marca)
        await self._session.flush()
        logger.info(f"Marca nova no cadastro: {marca.name!r}")
        return marca

    async def get_brand(self, brand_key: uuid.UUID | None) -> Brand | None:
        """A marca desta entidade, ou None se ela está na partição genérica."""
        if brand_key is None:
            return None
        result = await self._session.execute(select(Brand).where(Brand.brand_key == brand_key))
        return result.scalar_one_or_none()

    async def list_other_brand_names(self, brand_key: uuid.UUID | None) -> list[str]:
        """Formas normalizadas de todas as marcas do cadastro MENOS esta.

        É a lista contra a qual o veto cruzado da demanda compara (ver
        indicators/pytrends._de_outra_marca). Vazia quando o produto não tem
        marca: numa entidade genérica, consulta de marca é demanda legítima da
        categoria — quem procura "cardhu" está no mercado de whisky — e
        descartá-la subestimaria a categoria.
        """
        if brand_key is None:
            return []
        result = await self._session.execute(select(Brand.name_normalized).where(Brand.brand_key != brand_key))
        return [nome for nome in result.scalars().all() if nome]

    async def list_all_brand_names(self) -> list[str]:
        """Formas normalizadas de TODAS as marcas do cadastro.

        Usada só na medição da CATEGORIA: ali o que se quer é a demanda
        genérica, sem marca nenhuma — nem as concorrentes, nem a própria. É o
        denominador contra o qual a demanda da marca é lida, e ele só responde
        "o mercado existe?" se for de fato o mercado em abstrato.
        """
        result = await self._session.execute(select(Brand.name_normalized))
        return [nome for nome in result.scalars().all() if nome]

    async def repoint_alias(self, alias: ProductAlias, entity_key: uuid.UUID) -> None:
        """Troca o dono de um alias.

        Existe para UM caso, e de propósito não é genérico: um alias com marca
        (`marca + nome`) apontando para uma entidade que não é daquela marca. A
        regra antiga (28ca6be até 27/08) gravava exatamente isso — o alias
        `cardhu single malt scotch whisky` apontando para a entidade genérica
        `single malt Scotch whisky`. Como `alias_normalized` é único global,
        deixá-lo onde está impediria a entidade certa de ter alias exato, e
        toda resolução da marca voltaria a cair no passo 2 ou 3 e criaria
        entidade duplicada.

        A regra do novo modelo é clara o suficiente para automatizar: quem
        possui um alias qualificado por marca sem ser a entidade daquela marca
        está errado por construção.
        """
        anterior = alias.entity_key
        alias.entity_key = entity_key
        await self._session.flush()
        logger.warning(
            f"Alias {alias.alias_normalized!r} repontado de {anterior} para {entity_key}: "
            f"era artefato da regra antiga de marca"
        )

    async def create_alias(self, *, alias_normalized: str, entity_key: uuid.UUID, source: str) -> ProductAlias:
        alias = ProductAlias(
            id=uuid.uuid4(),
            alias_normalized=alias_normalized,
            entity_key=entity_key,
            source=source,
            hit_count=1,
            first_seen_at=utc_now(),
        )
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def create_aliases_bulk(self, *, aliases: dict[str, uuid.UUID], source: str) -> int:
        """Um INSERT para todos os aliases, em vez de um por termo.

        Um produto com 27 mercados chega aqui com ~286 termos normalizados; um
        INSERT+flush por termo eram ~286 idas ao banco por resolução, e é o
        caminho quente do backfill do catálogo inteiro.

        `ON CONFLICT DO NOTHING` não é só velocidade: `alias_normalized` é
        único global, então duas resoluções simultâneas do mesmo termo faziam a
        segunda estourar IntegrityError. Aqui a segunda simplesmente não grava,
        que é exatamente a regra de negócio (o primeiro dono prevalece).
        """
        if not aliases:
            return 0
        linhas = [
            {
                "id": uuid.uuid4(),
                "alias_normalized": alias,
                "entity_key": entity_key,
                "source": source,
                "hit_count": 1,
                "first_seen_at": utc_now(),
            }
            for alias, entity_key in aliases.items()
        ]
        stmt = pg_insert(ProductAlias).values(linhas).on_conflict_do_nothing(index_elements=["alias_normalized"])
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)

    async def create_excluded_terms_bulk(self, *, entity_key: uuid.UUID, termos: dict[str, str], source: str) -> int:
        """Mesma razão do bulk de aliases. O conflito aqui é por
        (entity_key, term_normalized): reindexar a mesma entidade não duplica,
        e por isso o indexador não precisa mais ler antes de gravar."""
        if not termos:
            return 0
        linhas = [
            {
                "id": uuid.uuid4(),
                "entity_key": entity_key,
                "term_normalized": normalizado,
                "term": superficie,
                "source": source,
                "first_seen_at": utc_now(),
            }
            for normalizado, superficie in termos.items()
        ]
        stmt = (
            pg_insert(ProductExcludedTerm)
            .values(linhas)
            .on_conflict_do_nothing(constraint="uq_product_excluded_terms_entity_term")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)

    async def list_entities_with_markets(
        self, *, after: uuid.UUID | None = None, limit: int = 100
    ) -> list[ProductCatalog]:
        """Uma página de entidades com vocabulário curado — o que o backfill
        percorre. `_index_terms` só roda dentro do `resolve()`, então entidade
        criada antes dessas features nunca teve seus termos indexados.

        Paginação por chave (`entity_key > after`) e não por OFFSET: OFFSET faz
        o Postgres varrer e descartar as páginas anteriores a cada chamada, o
        que torna o backfill quadrático no tamanho do catálogo. A chave é a PK,
        então cada página é um index scan a partir de onde a anterior parou.
        """
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.merged_into.is_(None))
            .where(ProductCatalog.localized_markets.is_not(None))
            .order_by(ProductCatalog.entity_key)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(ProductCatalog.entity_key > after)
        result = await self._session.execute(stmt)
        return list(result.scalars())

    async def prune_derived_terms(self, *, entity_key: uuid.UUID, aliases: set[str], excluidos: set[str]) -> int:
        """Apaga alias e termo excluído DERIVADOS que a curadoria atual não
        produz mais. Só toca em `source = "localized_market"`.

        Alias vindo de `analyze` ou `text_identification` nunca é apagado: veio
        de uma consulta real de usuário, é evidência, não dado derivado.

        Existe porque o normalizador antigo TRUNCAVA termo de escrita mista em
        vez de preservá-lo — `Ninja エアフライヤー` virava o alias `ninja`,
        `SK매직 에어프라이어` virava `sk`. Dez marcas isoladas ficaram assim
        apontando para a air fryer, e marca isolada é o pior alias possível:
        casa por igualdade exata, no passo 1, onde o veto do passo 2 nem roda.
        Consultar `philips` (qualquer produto Philips) resolvia para air fryer.
        """
        apagados = 0
        stmt_alias = delete(ProductAlias).where(
            ProductAlias.entity_key == entity_key,
            ProductAlias.source == "localized_market",
            ProductAlias.alias_normalized.notin_(aliases) if aliases else sa_true(),
        )
        resultado = cast(CursorResult[Any], await self._session.execute(stmt_alias))
        apagados += int(resultado.rowcount or 0)

        stmt_excl = delete(ProductExcludedTerm).where(
            ProductExcludedTerm.entity_key == entity_key,
            ProductExcludedTerm.term_normalized.notin_(excluidos) if excluidos else sa_true(),
        )
        resultado = cast(CursorResult[Any], await self._session.execute(stmt_excl))
        apagados += int(resultado.rowcount or 0)
        return apagados

    async def list_match_candidates(
        self,
        *,
        category: str | None = None,
        priority: list[ProductCatalog] | None = None,
        brand_key: uuid.UUID | None = None,
    ) -> list[ProductCatalog]:
        """Entidades que o LLM vai considerar quando alias e trigram falham.

        Prioriza a mesma `category` quando o chamador a conhece (identify-image
        a traz): produto novo quase sempre colide com produto do mesmo tipo, e
        entidades de outra categoria só gastariam tokens. Completa com as
        demais até o teto, para que uma categoria errada vinda do LLM de visão
        não esconda o candidato certo.

        `priority` entra sempre, antes de tudo: são as entidades que o passo 2
        vetou. Vetar significa "a semelhança dizia que sim e o catálogo diz que
        não" — exatamente o caso que o LLM existe para arbitrar, então essa
        entidade não pode ficar de fora da lista por causa do teto ou de uma
        categoria divergente.

        Os candidatos vêm da MESMA partição de marca do pedido (`brand_key`
        igual, NULL = genérica). Mostrar ao LLM produtos de outra marca só
        oferece fusão errada: ele decide identidade de produto, e "é o mesmo
        produto de outra marca" é justamente a resposta que a partição existe
        para tornar impossível.
        """
        limite = settings.catalog_match_candidate_limit
        base = select(ProductCatalog).where(
            ProductCatalog.merged_into.is_(None),
            ProductCatalog.brand_key.is_not_distinct_from(brand_key),
        )

        selecionadas: list[ProductCatalog] = []
        vistos: set[uuid.UUID] = set()

        for produto in priority or []:
            if produto.entity_key not in vistos:
                selecionadas.append(produto)
                vistos.add(produto.entity_key)

        if category:
            result = await self._session.execute(
                base.where(ProductCatalog.category == category).order_by(ProductCatalog.created_at.desc()).limit(limite)
            )
            for produto in result.scalars():
                if produto.entity_key in vistos:
                    continue
                selecionadas.append(produto)
                vistos.add(produto.entity_key)

        if len(selecionadas) < limite:
            result = await self._session.execute(base.order_by(ProductCatalog.created_at.desc()).limit(limite * 2))
            for produto in result.scalars():
                if produto.entity_key in vistos:
                    continue
                selecionadas.append(produto)
                if len(selecionadas) >= limite:
                    break

        return selecionadas

    async def existing_alias_owners(self, normalized: set[str]) -> dict[str, uuid.UUID]:
        """Quais destes aliases normalizados já existem, e de quem são.

        Consulta em lote porque o indexador de termos localizados chega com
        dezenas de termos de uma vez (um produto com 23 mercados traz ~115) e
        um SELECT por termo seria o passo mais caro de uma resolução.
        """
        if not normalized:
            return {}
        stmt = select(ProductAlias.alias_normalized, ProductAlias.entity_key).where(
            ProductAlias.alias_normalized.in_(normalized)
        )
        result = await self._session.execute(stmt)
        return {alias: key for alias, key in result.all()}

    async def set_champion(
        self,
        entity: ProductCatalog,
        *,
        country: str | None,
        mos: Decimal | None,
        period_key: str,
    ) -> None:
        """Record the product's standout reference market for a period.

        Called with country=None when no candidate could be scored, and that is
        deliberately still a write: period_key is what marks the work as done, so
        "nothing scorable this month" is remembered rather than re-attempted on
        every analysis. Distinguishing it from "never computed" is the whole
        reason champion_period_key exists separately from champion_country.
        """
        entity.champion_country = country
        entity.champion_mos = mos
        entity.champion_period_key = period_key
        entity.champion_computed_at = utc_now()
        await self._session.flush()

