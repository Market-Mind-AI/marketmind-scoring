import math
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, bindparam, delete, func, literal_column, select, text
from sqlalchemy import true as sa_true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import logger
from app.db.models.brand import Brand
from app.db.models.brand_champion import BrandChampion
from app.db.models.brand_market import BrandMarket
from app.db.models.brand_reference_candidate import BrandReferenceCandidate
from app.db.models.product_alias import ProductAlias
from app.db.models.product_catalog import ProductCatalog
from app.db.models.product_excluded_term import ProductExcludedTerm
from app.services.vocabulary_window import apply_candidates, apply_markets, vocabulary_is_fixed
from app.utils.text import as_term_list, categorias_batem, normalize_alias
from app.utils.time import utc_now


@dataclass
class TrigramMatch:
    product: ProductCatalog
    similarity: float


@dataclass
class Polaridade:
    """De que lado de uma oposição a consulta caiu, para UMA entidade."""

    # O termo mais próximo de cada lado: a evidência legível, para o log e
    # para o LLM. A decisão é pelo `escore` (k-NN), não por estes dois.
    termo_positivo: str
    distancia_positivo: float
    termo_antagonico: str
    distancia_antagonico: float
    # sim média dos k termos mais próximos do produto - a dos k antagônicos.
    escore: float

    @property
    def vetada(self) -> bool:
        # Empate veta, como no trigram: na dúvida, quem decide é o LLM.
        return self.escore <= 0

    @property
    def ambigua(self) -> bool:
        """Dentro da banda de abstenção: nenhum dos lados ganhou por margem
        suficiente para o passo 2 decidir por forma. Vai ao LLM."""
        return not self.vetada and self.escore <= settings.catalog_semantic_abstention_margin


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

    async def get_entity_by_alias_exact(
        self, alias_normalized: str, category: str | None = None
    ) -> tuple[ProductAlias, ProductCatalog] | None:
        """O produto deste nome NESTA categoria, quando a resposta é inequívoca.

        A identidade é o par (nome, categoria): `jaguar` em Automotive e
        `jaguar` em Wildlife são produtos diferentes, e até 24/09/2026 caíam na
        mesma entidade pelo caminho O(1) — o mais barato, e por isso o único
        que nada a jusante corrige.

        Devolve None quando o nome existe em mais de uma categoria e o pedido
        não diz qual. Recusar é a resposta certa: quem decide por SIGNIFICADO é
        o passo 3, e chutar aqui criaria um erro permanente pelo caminho que
        ninguém revisa.
        """
        stmt = (
            select(ProductAlias, ProductCatalog)
            .join(ProductCatalog, ProductCatalog.entity_key == ProductAlias.entity_key)
            .where(ProductAlias.alias_normalized == alias_normalized)
        )
        linhas = list((await self._session.execute(stmt)).all())
        if not linhas:
            return None
        if category:
            exatas = [linha for linha in linhas if linha[0].category and categorias_batem(linha[0].category, category)]
            if exatas:
                return exatas[0][0], exatas[0][1]
            # Só as sem categoria declarada podem servir a um pedido com
            # categoria: elas não contradizem nada. Uma categoria concreta e
            # diferente contradiz.
            sem_categoria = [linha for linha in linhas if not linha[0].category]
            if len(sem_categoria) == 1 and len(linhas) == 1:
                return sem_categoria[0][0], sem_categoria[0][1]
            return None
        if len(linhas) == 1:
            return linhas[0][0], linhas[0][1]
        return None

    async def markets_for(self, entity: ProductCatalog, brand_key: uuid.UUID | None) -> dict[str, dict[str, Any]]:
        """O vocabulário de busca deste produto, para ESTA marca (ou sem marca).

        O único lugar que decide de onde os termos vêm. Sem marca, são os do
        produto (`localized_markets`); com marca, as linhas de `brand_markets`.
        O formato é o mesmo dos dois lados de propósito — quem lê não precisa
        saber qual caminho foi.

        A separação não é organização: "juan valdez café" e "café" são buscas
        diferentes no Google Ads e nunca podem dar o mesmo número. Guardar os
        dois no mesmo lugar foi o estrago medido em 27/08/2026, quando duas
        marcas de whisky dividiram a mesma entidade e a segunda herdou a
        demanda da primeira.
        """
        if brand_key is None:
            return dict(entity.localized_markets or {})
        linhas = await self._session.execute(
            select(BrandMarket).where(BrandMarket.entity_key == entity.entity_key, BrandMarket.brand_key == brand_key)
        )
        return {linha.country: dict(linha.market) for linha in linhas.scalars().all()}

    async def save_brand_markets(
        self,
        entity_key: uuid.UUID,
        brand_key: uuid.UUID,
        markets: dict[str, dict[str, Any]],
        *,
        keep_fixed: bool = False,
    ) -> None:
        """Grava (ou atualiza) o vocabulário de uma marca, país a país.

        Mercado vazio é ignorado em vez de gravado: uma linha sem termos não
        diz "não tem", diz "ninguém escreveu ainda", e as duas coisas precisam
        continuar distinguíveis para o enriquecimento saber o que fazer.

        `keep_fixed`: quem traz os termos de uma nova identificação não troca
        os de um país gravado há menos de um semestre (`search_terms_ttl_days`).
        É a mesma regra do vocabulário do produto: lista de termos nova é
        semente nova para as palavras relacionadas, e semente nova é Google Ads
        pago de novo em todos os países.
        """
        for country, market in (markets or {}).items():
            if not market or not as_term_list(market.get("search_terms")):
                continue
            existente = await self._session.get(BrandMarket, (entity_key, brand_key, country))
            if existente is None:
                self._session.add(
                    BrandMarket(entity_key=entity_key, brand_key=brand_key, country=country, market=market)
                )
                continue
            # A mesma regra do produto (ver vocabulary_window): dentro da
            # janela um país com termos fica com eles; fora dela os termos se
            # acumulam. Substituir jogava fora o que já tinha sido curado.
            fixo = keep_fixed and vocabulary_is_fixed(existente.updated_at)
            mesclado, _aplicado = apply_markets({country: existente.market}, {country: market}, fixed=fixo)
            existente.market = mesclado[country]
        await self._session.flush()

    async def reference_candidates_for(
        self, entity: ProductCatalog, brand_key: uuid.UUID | None
    ) -> list[dict[str, Any]]:
        """Os mercados de referência desta análise: do produto, ou DESTA marca.

        O maior volume real entre eles é o teto da régua de Demand Volume. Onde
        o café é mais procurado não é onde a Juan Valdez é mais procurada, e
        usar a régua da categoria para medir a marca desloca todos os scores de
        demanda dela de uma vez.
        """
        if brand_key is None:
            return list(entity.reference_candidates or [])
        linha = await self._session.get(BrandReferenceCandidate, (entity.entity_key, brand_key))
        return list(linha.candidates) if linha is not None else []

    async def save_reference_candidates(
        self, entity: ProductCatalog, brand_key: uuid.UUID | None, candidates: list[dict[str, Any]]
    ) -> None:
        if not candidates:
            return
        if brand_key is None:
            entity.reference_candidates = candidates
            await self._session.flush()
            return
        linha = await self._session.get(BrandReferenceCandidate, (entity.entity_key, brand_key))
        if linha is None:
            self._session.add(
                BrandReferenceCandidate(entity_key=entity.entity_key, brand_key=brand_key, candidates=candidates)
            )
        else:
            linha.candidates = candidates
        await self._session.flush()

    async def merge_identified_reference_candidates(
        self, entity: ProductCatalog, brand_key: uuid.UUID, incoming: list[dict[str, Any]]
    ) -> None:
        """Os candidatos de referência que uma identificação trouxe para uma
        marca, pela regra da janela (ver vocabulary_window). Sem janela, cada
        identificação nova trocava os candidatos da marca, e com eles as
        sementes pagas no Google Ads."""
        linha = await self._session.get(BrandReferenceCandidate, (entity.entity_key, brand_key))
        if linha is None:
            await self.save_reference_candidates(entity, brand_key, incoming)
            return
        linha.candidates = apply_candidates(linha.candidates, incoming, fixed=vocabulary_is_fixed(linha.updated_at))
        await self._session.flush()

    async def get_by_entity_key(self, entity_key: uuid.UUID) -> ProductCatalog | None:
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.entity_key == entity_key)
            .options(selectinload(ProductCatalog.aliases))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def owners_of_search_term(self, termo: str) -> set[tuple[uuid.UUID, uuid.UUID | None]]:
        """Quem declara `termo` entre os seus termos de busca: (produto, marca).

        Os dois lugares onde o vocabulário mora — `brand_markets` para o de uma
        marca, `localized_markets` para o do produto sem marca. Um termo de
        busca é o que o comprador digita para ESTE produto, então o texto igual
        a um deles identifica o produto sem modelo nenhum. Comparado sem
        caixa e com espaços normalizados; quem chama confere a forma exata.
        """
        consulta = text(
            """
            SELECT bm.entity_key, bm.brand_key
              FROM brand_markets bm
              JOIN product_catalog pc ON pc.entity_key = bm.entity_key AND pc.merged_into IS NULL
             CROSS JOIN LATERAL jsonb_array_elements_text(
                   CASE WHEN jsonb_typeof(bm.market->'search_terms') = 'array'
                        THEN bm.market->'search_terms' ELSE '[]'::jsonb END) t
             WHERE lower(regexp_replace(btrim(t), '\\s+', ' ', 'g')) = :termo
            UNION
            SELECT pc.entity_key, NULL::uuid
              FROM product_catalog pc
             CROSS JOIN LATERAL jsonb_each(
                   CASE WHEN jsonb_typeof(pc.localized_markets) = 'object'
                        THEN pc.localized_markets ELSE '{}'::jsonb END) m
             CROSS JOIN LATERAL jsonb_array_elements_text(
                   CASE WHEN jsonb_typeof(m.value->'search_terms') = 'array'
                        THEN m.value->'search_terms' ELSE '[]'::jsonb END) t
             WHERE pc.merged_into IS NULL
               AND lower(regexp_replace(btrim(t), '\\s+', ' ', 'g')) = :termo
            """
        )
        linhas = await self._session.execute(consulta, {"termo": " ".join(termo.lower().split())})
        return {(linha[0], linha[1]) for linha in linhas.all()}

    async def list_incomplete_entities(self, *, after: uuid.UUID | None, limit: int) -> list[ProductCatalog]:
        """Produtos vivos a que falta algum campo da identidade: preço,
        atributos, antagônicos, categoria ou faixa etária. Paginados pela chave."""
        stmt = (
            select(ProductCatalog)
            .where(
                ProductCatalog.merged_into.is_(None),
                (ProductCatalog.estimated_price.is_(None))
                | (ProductCatalog.defining_attributes.is_(None))
                | (ProductCatalog.antagonist_products.is_(None))
                | (ProductCatalog.category.is_(None))
                | (ProductCatalog.target_age_min.is_(None))
                | (ProductCatalog.target_age_max.is_(None)),
            )
            .order_by(ProductCatalog.entity_key)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(ProductCatalog.entity_key > after)
        return list((await self._session.execute(stmt)).scalars().all())

    async def brand_terms(self, entity_key: uuid.UUID) -> dict[str, list[str]]:
        """Cada marca já analisada neste produto, com os termos de busca dela."""
        linhas = await self._session.execute(
            select(Brand.name, BrandMarket.market)
            .join(Brand, Brand.brand_key == BrandMarket.brand_key)
            .where(BrandMarket.entity_key == entity_key)
        )
        marcas: dict[str, list[str]] = {}
        for nome, market in linhas.all():
            termos = marcas.setdefault(nome, [])
            termos.extend(t for t in (market or {}).get("search_terms") or [] if t and t not in termos)
        return marcas

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

        melhor: TrigramMatch | None = None
        vetadas: list[ProductCatalog] = []
        for product, sim, melhor_negativa in result.all():
            # Candidata desqualificada vai para o passo 3 na lista de
            # prioridade, porque é justamente ela que o LLM tem de julgar.
            if float(melhor_negativa) >= float(sim):
                vetadas.append(product)
                continue
            if not categorias_batem(product.category, category):
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
            vocabulary_at=utc_now() if localized_markets else None,
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

    async def create_alias(
        self, *, alias_normalized: str, entity_key: uuid.UUID, source: str, category: str | None = None
    ) -> ProductAlias:
        alias = ProductAlias(
            id=uuid.uuid4(),
            alias_normalized=alias_normalized,
            # Parte da identidade, não decoração: o mesmo nome em outra
            # categoria é outro produto (migration 051).
            category=category,
            entity_key=entity_key,
            source=source,
            hit_count=1,
            first_seen_at=utc_now(),
        )
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def set_alias(
        self, *, alias_normalized: str, entity_key: uuid.UUID, source: str, category: str | None = None
    ) -> None:
        """Faz (nome, categoria) apontar para `entity_key`, exista a linha ou não.

        É o que o serviço grava depois de DECIDIR a identidade de uma consulta.
        Um INSERT puro estourava quando o par já existia: medido em 28/09/2026
        em azure-dev, `air fryer` em Kitchen Appliances tinha alias para a
        entidade guardada como Home appliances; o passo 1 recusava pela
        categoria da entidade, o passo 3 confirmava o mesmo produto, e gravar
        o alias de novo dava UniqueViolation — 500 em toda repetição.

        A decisão que acabou de ser tomada vale sobre a antiga: se o passo 3
        escolheu outra entidade para este par, é para ela que o caminho O(1)
        tem que levar da próxima vez.
        """
        stmt = pg_insert(ProductAlias).values(
            id=uuid.uuid4(),
            alias_normalized=alias_normalized,
            category=category,
            entity_key=entity_key,
            source=source,
            hit_count=1,
            first_seen_at=utc_now(),
        )
        # `''` LITERAL, não parâmetro. Com parâmetro o arbiter é
        # `coalesce(category, $8)`, que o Postgres só casa com o índice da
        # migration 051 enquanto usa plano customizado — o asyncpg prepara o
        # statement e, da 6ª execução na mesma conexão em diante, o plano
        # genérico não infere mais o índice e o INSERT falha com "no unique or
        # exclusion constraint matching the ON CONFLICT specification".
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProductAlias.alias_normalized, func.coalesce(ProductAlias.category, literal_column("''"))],
            set_={"entity_key": stmt.excluded.entity_key, "hit_count": ProductAlias.hit_count + 1},
        )
        await self._session.execute(stmt)

    async def create_aliases_bulk(
        self, *, aliases: dict[str, uuid.UUID], source: str, category: str | None = None
    ) -> int:
        """Um INSERT para todos os aliases, em vez de um por termo.

        Um produto com 27 mercados chega aqui com ~286 termos normalizados; um
        INSERT+flush por termo eram ~286 idas ao banco por resolução, e é o
        caminho quente do backfill do catálogo inteiro.

        `ON CONFLICT DO NOTHING` não é só velocidade: o par (nome, categoria) é
        único, então duas resoluções simultâneas do mesmo termo faziam a segunda
        estourar IntegrityError. Aqui a segunda simplesmente não grava, que é
        exatamente a regra de negócio (o primeiro dono prevalece).

        O conflito é declarado sobre a EXPRESSÃO do índice, não sobre a coluna:
        a unicidade vive em `(alias_normalized, COALESCE(category, ''))` desde a
        migration 051, porque nulo não participa de unicidade no Postgres.
        """
        if not aliases:
            return 0
        linhas = [
            {
                "id": uuid.uuid4(),
                "alias_normalized": alias,
                "category": category,
                "entity_key": entity_key,
                "source": source,
                "hit_count": 1,
                "first_seen_at": utc_now(),
            }
            for alias, entity_key in aliases.items()
        ]
        # `''` literal pelo mesmo motivo de set_alias: parâmetro só casa com o
        # índice enquanto o plano é customizado.
        stmt = (
            pg_insert(ProductAlias)
            .values(linhas)
            .on_conflict_do_nothing(
                index_elements=[
                    ProductAlias.alias_normalized,
                    func.coalesce(ProductAlias.category, literal_column("''")),
                ]
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)

    async def create_excluded_terms_bulk(
        self, *, entity_key: uuid.UUID, termos: dict[str, str], source: str, kind: str = "excluded"
    ) -> int:
        """Mesma razão do bulk de aliases. O conflito aqui é por
        (entity_key, term_normalized): reindexar a mesma entidade não duplica,
        e por isso o indexador não precisa mais ler antes de gravar.

        Um termo que chega como `antagonist` e já existia como `excluded` é
        PROMOVIDO: o antagônico é o que mais diz sobre a identidade, e é o
        único que o veto semântico lê. O contrário nunca rebaixa."""
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
                "kind": kind,
            }
            for normalizado, superficie in termos.items()
        ]
        stmt = pg_insert(ProductExcludedTerm).values(linhas)
        if kind == "antagonist":
            stmt = stmt.on_conflict_do_update(
                constraint="uq_product_excluded_terms_entity_term",
                set_={"kind": "antagonist"},
                where=ProductExcludedTerm.kind != "antagonist",
            )
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="uq_product_excluded_terms_entity_term")
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
            # A mesma restrição dos aliases. Sem ela, os antagônicos que o
            # passo 3 aprende (source "catalog_match", fora de
            # localized_markets) seriam apagados no próximo backfill.
            ProductExcludedTerm.source == "localized_market",
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
        semantic: list[ProductCatalog] | None = None,
        brand_key: uuid.UUID | None = None,
    ) -> list[ProductCatalog]:
        """Entidades que o LLM vai considerar quando alias e trigram falham.

        `semantic` são os vizinhos mais próximos do passo 3a (semantic_search),
        em ordem de distância, e entram logo depois de `priority`. São a única
        parte da lista que olha o SIGNIFICADO da consulta: o resto é categoria
        e data de criação, que só acertam enquanto o catálogo cabe inteiro no
        teto. Vazio quando o passo 3a está desligado ou falhou — a seleção
        então é a de antes.

        Depois deles, prioriza a mesma `category` quando o chamador a conhece (identify-image
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

        for produto in semantic or []:
            if produto.entity_key not in vistos and len(selecionadas) < limite:
                selecionadas.append(produto)
                vistos.add(produto.entity_key)

        if category:
            # As grafias guardadas que são a MESMA categoria pela regra única
            # (categorias_batem). Igualdade exata no SQL perdia 'Eletrodomésticos'
            # contra 'eletrodomesticos'; e as categorias distintas do catálogo
            # são poucas, então a comparação é feita aqui e o SQL só filtra.
            distintas = (
                await self._session.execute(
                    select(ProductCatalog.category).distinct().where(ProductCatalog.category.is_not(None))
                )
            ).scalars()
            mesmas = [c for c in distintas if categorias_batem(c, category)]
            result = await self._session.execute(
                base.where(ProductCatalog.category.in_(mesmas)).order_by(ProductCatalog.created_at.desc()).limit(limite)
            )
            for produto in result.scalars():
                if produto.entity_key in vistos:
                    continue
                if len(selecionadas) >= limite:
                    break
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

    async def semantic_search(
        self, query_vector: str, *, model: str, brand_key: uuid.UUID | None = None, top_k: int
    ) -> list[tuple[ProductCatalog, float]]:
        """Passo 3a: as `top_k` entidades cujos termos estão mais perto da
        consulta, por distância coseno, com a distância da melhor.

        Só RECUPERA. Quem aceita é o LLM; a distância não é comparada com
        limiar nenhum, porque a medição de 14/08/2026 mostrou que nenhum separa
        "mesmo produto" de "produto vizinho" (`freidora de aire` x `freidora de
        aceite` = 0,044). Para recuperar, o que importa é a ORDEM, e nela o
        vizinho certo aparece entre os primeiros.

        DUAS ETAPAS porque o índice ordena ALIASES e a resposta é ENTIDADES.
        Os termos de uma entidade são traduções uns dos outros e ficam juntos
        no espaço: os 10 aliases mais próximos costumam ser de uma entidade só.
        Por isso o HNSW traz um bolsão de `top_k * 20` aliases e o agrupamento
        fica com a melhor distância de cada entidade.

        `hnsw.iterative_scan` porque o filtro da partição de marca roda DEPOIS
        do índice: sem ele, o HNSW devolve `ef_search` vizinhos, o filtro joga
        fora os de outras marcas e a consulta pode voltar com menos do que
        pediu. Com ele (pgvector >= 0.8), a varredura continua até preencher o
        LIMIT. Os dois GUCs valem só nesta transação, como o limiar do trigram.
        """
        await self._session.execute(
            select(func.set_config("hnsw.ef_search", str(settings.catalog_semantic_ef_search), True))
        )
        await self._session.execute(select(func.set_config("hnsw.iterative_scan", "relaxed_order", True)))
        stmt = text(
            """
            WITH vizinhos AS (
                SELECT a.entity_key, a.embedding <=> CAST(:q AS vector) AS distancia
                FROM product_aliases a
                JOIN product_catalog p ON p.entity_key = a.entity_key
                WHERE a.embedding IS NOT NULL
                  AND a.embedding_model = :modelo
                  AND p.merged_into IS NULL
                  AND p.brand_key IS NOT DISTINCT FROM :brand
                ORDER BY a.embedding <=> CAST(:q AS vector)
                LIMIT :bolsao
            )
            SELECT entity_key, min(distancia) AS distancia
            FROM vizinhos
            GROUP BY entity_key
            ORDER BY distancia
            LIMIT :k
            """
        ).bindparams(bindparam("brand", type_=PG_UUID(as_uuid=True)))
        linhas = (
            await self._session.execute(
                stmt, {"q": query_vector, "modelo": model, "brand": brand_key, "bolsao": top_k * 20, "k": top_k}
            )
        ).all()
        if not linhas:
            return []
        entidades = await self._session.execute(
            select(ProductCatalog).where(ProductCatalog.entity_key.in_([chave for chave, _ in linhas]))
        )
        por_chave = {e.entity_key: e for e in entidades.scalars()}
        return [(por_chave[chave], float(dist)) for chave, dist in linhas if chave in por_chave]

    async def pending_embeddings(
        self, *, model: str, limit: int, entity_key: uuid.UUID | None = None
    ) -> list[tuple[str, uuid.UUID, str]]:
        """Textos sem vetor do modelo atual: (tabela, id, texto normalizado).

        Duas fontes. Os aliases, que o passo 3a recupera; e os termos
        ANTAGÔNICOS, que o veto semântico compara com eles. Os demais termos
        excluídos (uso, receita, consulta) ficam sem vetor de propósito — ver
        migration 053. Vetor de outro modelo conta como ausente: não está no
        mesmo espaço."""
        filtro = "AND entity_key = :entidade" if entity_key is not None else ""
        stmt = text(
            f"""
            (SELECT 'product_aliases' AS tabela, id, alias_normalized AS texto FROM product_aliases
              WHERE (embedding IS NULL OR embedding_model IS DISTINCT FROM :modelo) {filtro})
            UNION ALL
            (SELECT 'product_excluded_terms', id, term_normalized FROM product_excluded_terms
              WHERE kind = 'antagonist'
                AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :modelo) {filtro})
            ORDER BY 1, 2
            LIMIT :n
            """
        )
        params: dict[str, Any] = {"modelo": model, "n": limit}
        if entity_key is not None:
            params["entidade"] = entity_key
        return [(t, id_, texto) for t, id_, texto in (await self._session.execute(stmt, params)).all()]

    async def save_embeddings(self, vetores: list[tuple[str, uuid.UUID, str]], *, model: str) -> None:
        """Grava (tabela, id, vetor literal). A tabela vem de pending_embeddings,
        nunca de fora, e mesmo assim é conferida contra a lista fechada antes
        de entrar no SQL."""
        por_tabela: dict[str, list[dict[str, Any]]] = {}
        for tabela, id_, vetor in vetores:
            if tabela not in ("product_aliases", "product_excluded_terms"):
                raise ValueError(f"tabela sem vetor: {tabela!r}")
            por_tabela.setdefault(tabela, []).append({"id": id_, "v": vetor, "modelo": model})
        for tabela, linhas in por_tabela.items():
            await self._session.execute(
                text(f"UPDATE {tabela} SET embedding = CAST(:v AS vector), embedding_model = :modelo WHERE id = :id"),
                linhas,
            )

    async def has_antagonist_vectors(self, entity_key: uuid.UUID, *, model: str) -> bool:
        """Barato, e decide se o passo 2 paga a chamada de embedding: entidade
        sem antagônico vetorizado não tem veto semântico possível."""
        stmt = text(
            "SELECT EXISTS (SELECT 1 FROM product_excluded_terms "
            "WHERE entity_key = :e AND kind = 'antagonist' AND embedding_model = :m)"
        )
        return bool((await self._session.execute(stmt, {"e": entity_key, "m": model})).scalar())

    async def polarity(
        self, query_vector: str, *, model: str, entity_keys: list[uuid.UUID], k: int = 3
    ) -> dict[uuid.UUID, Polaridade]:
        """Para cada entidade que TEM antagônicos vetorizados: de que lado da
        oposição a consulta caiu, e com que margem.

        É a regra do veto do trigram (ver trigram_search) transportada para o
        espaço de embeddings, e pelo mesmo motivo: a distância absoluta não
        separa produtos opostos (`fritadeira a ar` x `fritadeira a óleo` =
        0,065), mas o lado mais próximo, sim. Entidade sem antagônico não
        aparece no resultado — sem evidência negativa não há o que comparar.

        k-NN POR LADO, NÃO 1-NN. O escore é a média das similaridades dos `k`
        termos mais próximos do produto menos a dos `k` antagônicos mais
        próximos. Medido em 14/10/2026 (evaluation/veto_rules.py) contra 1-NN,
        centroide, eixo de Rocchio e LDA: o k-NN foi o melhor nas buscas reais
        (97,9% contra 94,8% do 1-NN, 40/40 antagônicos vetados) e empatou nas
        sintéticas. O 1-NN sofre com a assimetria — ~40 termos positivos contra
        ~15 antagônicos, e o mínimo sobre mais pontos é menor por acaso. Os
        métodos de centroide perderam porque exigem um conjunto de contraste
        limpo; o vizinho mais próximo tolera um antagônico errado na lista.

        O sinal decide (<= 0 veta; empate veta, como no trigram) e o módulo é
        a margem: abaixo de `settings.catalog_semantic_abstention_margin` a
        consulta é AMBÍGUA e vai ao LLM em vez de o passo 2 aceitar por forma.

        Varredura exata, sem índice: são as linhas de poucas entidades já
        escolhidas pelos passos anteriores."""
        if not entity_keys:
            return {}

        async def _lados(tabela: str, coluna: str, filtro: str, chaves: list[uuid.UUID]) -> dict[uuid.UUID, list]:
            stmt = text(
                f"""
                SELECT entity_key, {coluna}, distancia FROM (
                    SELECT entity_key, {coluna}, embedding <=> CAST(:q AS vector) AS distancia,
                           row_number() OVER (
                               PARTITION BY entity_key ORDER BY embedding <=> CAST(:q AS vector)
                           ) AS posicao
                    FROM {tabela}
                    WHERE entity_key = ANY(:chaves) AND embedding_model = :modelo {filtro}
                ) t WHERE posicao <= :k
                ORDER BY entity_key, distancia
                """
            ).bindparams(bindparam("chaves", type_=ARRAY(PG_UUID(as_uuid=True))))
            linhas = await self._session.execute(stmt, {"q": query_vector, "modelo": model, "chaves": chaves, "k": k})
            por_entidade: dict[uuid.UUID, list] = {}
            for chave, termo, distancia in linhas:
                por_entidade.setdefault(chave, []).append((termo, float(distancia)))
            return por_entidade

        antagonicos = await _lados("product_excluded_terms", "term", "AND kind = 'antagonist'", entity_keys)
        if not antagonicos:
            return {}
        positivos = await _lados("product_aliases", "alias_normalized", "", list(antagonicos))
        resultado: dict[uuid.UUID, Polaridade] = {}
        for chave, lado_antagonico in antagonicos.items():
            lado_positivo = positivos.get(chave)
            if not lado_positivo:
                continue
            media_positivo = sum(d for _, d in lado_positivo) / len(lado_positivo)
            media_antagonico = sum(d for _, d in lado_antagonico) / len(lado_antagonico)
            resultado[chave] = Polaridade(
                termo_positivo=lado_positivo[0][0],
                distancia_positivo=lado_positivo[0][1],
                termo_antagonico=lado_antagonico[0][0],
                distancia_antagonico=lado_antagonico[0][1],
                # Distância coseno = 1 - similaridade, então a diferença das
                # médias de distância é a diferença das médias de similaridade.
                escore=media_antagonico - media_positivo,
            )
        return resultado

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

    async def champion_for(
        self, entity: ProductCatalog, brand_key: uuid.UUID | None, period_key: str, *, demand_ceiling: float | None
    ) -> tuple[bool, str | None, Decimal | None]:
        """(já calculado neste período COM esta régua?, país, mos) para ESTA marca.

        Sem marca, o campeão é o do produto. Com marca, o dela — o produto
        passou a ser um só, e compartilhar campeão entre marcas faria o melhor
        mercado de uma virar o da outra.

        Essa contaminação se autoconfirma, e é por isso que ela tem tabela
        própria: campeão conhecido faz a análise PULAR a coleta dos mercados de
        referência. Um campeão herdado nunca seria corrigido, porque existir já
        impede a medição que o desmentiria.

        A régua entra na pergunta pelo mesmo motivo: um campeão calculado
        contra a régua de antes de ela subir tem um MOS que a régua de agora
        não daria, e reaproveitá-lo é coroar um número que não existe mais.
        """
        if brand_key is None:
            periodo, pais, mos, regua = (
                entity.champion_period_key,
                entity.champion_country,
                entity.champion_mos,
                entity.champion_demand_ceiling,
            )
        else:
            linha = await self._session.get(BrandChampion, (entity.entity_key, brand_key, period_key))
            if linha is None:
                return False, None, None
            periodo, pais, mos, regua = period_key, linha.country, linha.mos, linha.demand_ceiling
        if periodo != period_key or not _mesma_regua(regua, demand_ceiling):
            return False, None, None
        return True, pais, mos

    async def forget_champion(
        self, entity: ProductCatalog, brand_key: uuid.UUID | None, period_key: str | None
    ) -> None:
        """Esquece o campeão: o que o produto ou a marca eram mudou.

        `period_key` nulo esquece o de qualquer período. Inteiro, inclusive
        quando foi calculado: um campeão sem período mas com data seria um
        campeão pela metade, e quem lê a data acharia que houve cálculo."""
        if brand_key is None:
            if period_key is None or entity.champion_period_key == period_key:
                entity.champion_period_key = None
                entity.champion_country = None
                entity.champion_mos = None
                entity.champion_computed_at = None
                entity.champion_demand_ceiling = None
            return
        condicoes = [BrandChampion.entity_key == entity.entity_key, BrandChampion.brand_key == brand_key]
        if period_key is not None:
            condicoes.append(BrandChampion.period_key == period_key)
        await self._session.execute(delete(BrandChampion).where(*condicoes))

    async def set_champion(
        self,
        entity: ProductCatalog,
        *,
        country: str | None,
        mos: Decimal | None,
        period_key: str,
        demand_ceiling: float | None,
        brand_key: uuid.UUID | None = None,
    ) -> None:
        """Record the product's standout reference market for a period, and
        the demand ruler it was computed against.

        Called with country=None when no candidate could be scored, and that is
        deliberately still a write: period_key is what marks the work as done, so
        "nothing scorable this month" is remembered rather than re-attempted on
        every analysis. Distinguishing it from "never computed" is the whole
        reason champion_period_key exists separately from champion_country.
        """
        regua = None if demand_ceiling is None else Decimal(str(demand_ceiling))
        if brand_key is not None:
            linha = await self._session.get(BrandChampion, (entity.entity_key, brand_key, period_key))
            if linha is None:
                self._session.add(
                    BrandChampion(
                        entity_key=entity.entity_key,
                        brand_key=brand_key,
                        period_key=period_key,
                        country=country,
                        mos=mos,
                        demand_ceiling=regua,
                    )
                )
            else:
                linha.country, linha.mos, linha.demand_ceiling = country, mos, regua
                linha.computed_at = utc_now()
            await self._session.flush()
            return
        entity.champion_country = country
        entity.champion_mos = mos
        entity.champion_period_key = period_key
        entity.champion_computed_at = utc_now()
        entity.champion_demand_ceiling = regua
        await self._session.flush()


def _mesma_regua(guardada: Decimal | None, atual: float | None) -> bool:
    """Se o campeão guardado foi calculado contra a régua de agora. Teto
    nulo (régua fixa) só bate com teto nulo."""
    if guardada is None or atual is None:
        return guardada is None and atual is None
    return math.isclose(float(guardada), float(atual), rel_tol=1e-9)
