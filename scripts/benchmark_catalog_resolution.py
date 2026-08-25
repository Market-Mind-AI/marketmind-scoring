"""Mede o passo 2 da resolução de catálogo contra os DOIS conjuntos.

POR QUE ESTE SCRIPT EXISTE. A calibragem do trigram (limiar 0.4) foi feita em
14/08/2026 contra 89 termos do catálogo — todos POSITIVOS. "Precisão de 100%"
media metade do problema: nada dizia o que acontecia quando a consulta era um
termo que o catálogo declara NÃO ser aquele produto. O conjunto negativo já
existia (`excluded_terms`, curados por mercado desde a Fase 5) e nunca tinha
sido usado nem como guarda nem como teste. Rodá-lo revelou 45 fusões em 103.

SOMENTE LEITURA. Não escreve nada: monta o índice de aliases em memória, do
mesmo jeito que `_index_localized_terms` montaria, e usa o Postgres apenas para
calcular `similarity()` — a função real do pg_trgm, não uma reimplementação.

Uso (o padrão é o banco vivo, em leitura):
    python scripts/benchmark_catalog_resolution.py
"""

import asyncio
import sys
from collections import defaultdict, namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models.product_catalog import ProductCatalog  # noqa: E402
from app.utils.text import as_term_list, normalize_alias  # noqa: E402

LIMIAR = settings.catalog_trigram_similarity_threshold


def _vocabulario(entidades):
    """Reproduz o que a indexação gravaria: positivos globais (primeiro dono
    ganha, como o alias_normalized único) e negativos por entidade."""
    positivos: dict[str, str] = {}
    negativos: dict[str, dict[str, str]] = defaultdict(dict)

    def reivindicar(alias, chave):
        if alias:
            positivos.setdefault(alias, chave)

    for e in entidades:
        reivindicar(normalize_alias(e.canonical_title), str(e.entity_key))
    for e in entidades:
        for mercado in (e.localized_markets or {}).values():
            if not isinstance(mercado, dict):
                continue
            for termo in as_term_list(mercado.get("search_terms")):
                reivindicar(normalize_alias(termo), str(e.entity_key))
            for termo in as_term_list(mercado.get("excluded_terms")):
                chave = normalize_alias(termo)
                if chave:
                    negativos[str(e.entity_key)].setdefault(chave, termo)

    # Mesma regra de `_index_excluded_terms`: termo que é search_term E
    # excluded_term da MESMA entidade é contradição do dado, e o positivo
    # vence. Manter os dois aqui contaria como "fusão" um acerto legítimo.
    for chave, termos in negativos.items():
        for termo in [t for t in termos if positivos.get(t) == chave]:
            del termos[termo]
    return positivos, negativos


def _consultas(entidades, negativos):
    vistos, saida = set(), []
    for e in entidades:
        for mercado in (e.localized_markets or {}).values():
            if not isinstance(mercado, dict):
                continue
            for tipo, campo in (("positivo", "search_terms"), ("negativo", "excluded_terms")):
                for termo in as_term_list(mercado.get(campo)):
                    chave = normalize_alias(termo)
                    if tipo == "negativo" and chave not in negativos.get(str(e.entity_key), {}):
                        continue  # contradição já resolvida a favor do positivo
                    if chave and (chave, str(e.entity_key), tipo) not in vistos:
                        vistos.add((chave, str(e.entity_key), tipo))
                        saida.append((len(saida) + 1, chave, str(e.entity_key), tipo, termo))
    return saida


async def _similaridades(session, termos, consultas, *, inedita=False):
    """Uma ida ao Postgres por lado. VALUES em vez de tabela: nada é gravado.

    `inedita=True` remove o termo IDÊNTICO à consulta dos dois lados — é o que
    simula uma variação que ninguém cadastrou. Sem isso a medição pergunta
    apenas se o índice encontra o que ele mesmo contém, que é a falha de
    método da calibragem original.
    """
    if not termos:
        return defaultdict(dict)
    lado = ",".join(f"('{a}','{k}')" for a, k in termos)
    alvo = ",".join(f"({q[0]},'{q[1]}')" for q in consultas)
    filtro = "AND t.termo <> q.texto " if inedita else ""
    sql = text(
        f"WITH t(termo, ekey) AS (VALUES {lado}), q(qid, texto) AS (VALUES {alvo}) "
        "SELECT q.qid, t.ekey, similarity(t.termo, q.texto) AS sim "
        f"FROM q CROSS JOIN t WHERE similarity(t.termo, q.texto) > 0 {filtro}"
    )
    melhores: dict[int, dict[str, float]] = defaultdict(dict)
    for qid, ekey, sim in (await session.execute(sql)).all():
        if float(sim) > melhores[qid].get(ekey, 0.0):
            melhores[qid][ekey] = float(sim)
    return melhores


def _resolver(qid, positivos, negativos, com_veto, margem=0.0, limiar=None, escopo="global"):
    """Replica ProductRepository.trigram_search. `margem` > 0 daria folga ao
    veto (vetar mesmo quando a evidência negativa é um pouco mais fraca) — a
    produção usa 0, e o varrido no fim do relatório é o porquê."""
    corte = LIMIAR if limiar is None else limiar
    if com_veto and escopo == "global":
        melhor_pos = max((v for v in positivos[qid].values() if v >= corte), default=0.0)
        melhor_neg = max(negativos[qid].values(), default=0.0)
        if melhor_pos and melhor_neg >= melhor_pos:
            return None
    candidatos = []
    for ekey, sim in positivos[qid].items():
        if sim < corte:
            continue
        if com_veto and negativos[qid].get(ekey, 0.0) >= sim - margem:
            continue
        candidatos.append((sim, ekey))
    return max(candidatos) if candidatos else None


async def main() -> None:
    engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        # Só as três colunas de que este script precisa, em vez da entidade
        # inteira: o banco vivo pode estar numa migration anterior à do código
        # (medir não pode depender de estar em dia), e ler menos é ler menos.
        Entidade = namedtuple("Entidade", "entity_key canonical_title localized_markets")
        linhas = await session.execute(
            select(
                ProductCatalog.entity_key,
                ProductCatalog.canonical_title,
                ProductCatalog.localized_markets,
            ).where(ProductCatalog.merged_into.is_(None))
        )
        entidades = [Entidade(*linha) for linha in linhas.all()]
        positivos, negativos = _vocabulario(entidades)
        consultas = _consultas(entidades, negativos)
        termos_pos = list(positivos.items())
        termos_neg = [(t, k) for k, termos in negativos.items() for t in termos]
        pos_sim = await _similaridades(session, termos_pos, consultas)
        neg_sim = await _similaridades(session, termos_neg, consultas)
        pos_ine = await _similaridades(session, termos_pos, consultas, inedita=True)
        neg_ine = await _similaridades(session, termos_neg, consultas, inedita=True)
    await engine.dispose()

    n_pos = sum(1 for c in consultas if c[3] == "positivo")
    n_neg = sum(1 for c in consultas if c[3] == "negativo")
    n_excl = sum(len(v) for v in negativos.values())
    print(f"entidades {len(entidades)} | aliases {len(positivos)} | termos excluídos {n_excl}")
    print(f"consultas: {n_pos} positivas, {n_neg} negativas | limiar {LIMIAR}\n")
    cenarios = [
        ("sem veto (comportamento anterior)", pos_sim, neg_sim, False),
        ("com veto (implementado)", pos_sim, neg_sim, True),
        ("com veto, colisão NÃO curada", pos_sim, neg_ine, True),
        ("sem veto, consulta inédita", pos_ine, neg_sim, False),
        ("com veto, consulta inédita", pos_ine, neg_ine, True),
    ]
    # Variante medida e DESCARTADA: comparar a evidência negativa só da
    # entidade candidata, em vez da melhor do catálogo. Foi o desenho inicial;
    # perde o caso em que quem exclui o termo não é quem casou com ele
    # (`freidora de aceite`, excluída pela air fryer, casando com "olive oil").
    variantes = [
        ("veto só por entidade (descartado)", pos_sim, neg_sim),
        ("  idem, colisão NÃO curada", pos_sim, neg_ine),
        ("  idem, consulta inédita", pos_ine, neg_ine),
    ]
    print(f"{'':36} {'recall positivo':>18} {'fusões':>16} {'-> 3a entidade':>15}")
    # "3a entidade": termo excluído que casou com uma entidade DIFERENTE da que
    # o exclui. Nem sempre é erro — `panela de pressão elétrica` é excluída pela
    # air fryer e é o produto legítimo de outra entidade — mas também não é
    # sempre acerto, então fica numa coluna própria em vez de virar "fusão" ou
    # sumir do relatório. Ver transparencia-tecnica §1.6.
    for rotulo, pos, neg, com_veto in cenarios:
        acertos = fusoes = terceira = 0
        for qid, _, dono, tipo, _s in consultas:
            r = _resolver(qid, pos, neg, com_veto)
            if tipo == "positivo" and r and r[1] == dono:
                acertos += 1
            if tipo == "negativo" and r:
                fusoes += r[1] == dono
                terceira += r[1] != dono
        print(
            f"{rotulo:36} {acertos:8}/{n_pos} ({acertos/n_pos:5.1%}) "
            f"{fusoes:6}/{n_neg} ({fusoes/n_neg:5.1%}) {terceira:10}"
        )

    for rotulo, pos, neg in variantes:
        acertos = fusoes = terceira = 0
        for qid, _, dono, tipo, _s in consultas:
            r = _resolver(qid, pos, neg, True, 0.0, None, "entidade")
            if tipo == "positivo" and r and r[1] == dono:
                acertos += 1
            if tipo == "negativo" and r:
                fusoes += r[1] == dono
                terceira += r[1] != dono
        print(
            f"{rotulo:36} {acertos:8}/{n_pos} ({acertos/n_pos:5.1%}) "
            f"{fusoes:6}/{n_neg} ({fusoes/n_neg:5.1%}) {terceira:10}"
        )

    print("\nlimiar do trigram, com veto, sobre 'consulta inédita' — calibragem com os DOIS conjuntos:")
    for corte in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        acertos = fusoes = 0
        for qid, _, dono, tipo, _s in consultas:
            r = _resolver(qid, pos_ine, neg_ine, True, 0.0, corte)
            if tipo == "positivo" and r and r[1] == dono:
                acertos += 1
            if tipo == "negativo" and r and r[1] == dono:
                fusoes += 1
        marca = "  <- atual" if abs(corte - LIMIAR) < 1e-9 else ""
        print(
            f"  limiar {corte:.2f} -> recall {acertos:3}/{n_pos} ({acertos/n_pos:5.1%})"
            f"   fusões {fusoes:3}/{n_neg} ({fusoes/n_neg:5.1%}){marca}"
        )

    print("\nfolga no veto, sobre 'consulta inédita' — por que a produção usa margem 0:")
    for margem in (0.0, 0.05, 0.10, 0.15, 0.20):
        acertos = fusoes = 0
        for qid, _, dono, tipo, _s in consultas:
            r = _resolver(qid, pos_ine, neg_ine, True, margem)
            if tipo == "positivo" and r and r[1] == dono:
                acertos += 1
            if tipo == "negativo" and r and r[1] == dono:
                fusoes += 1
        print(
            f"  margem {margem:.2f} -> recall {acertos:3}/{n_pos} ({acertos/n_pos:5.1%})"
            f"   fusões {fusoes:3}/{n_neg} ({fusoes/n_neg:5.1%})"
        )

    print("\nfusões que o veto impede (as 20 mais próximas):")
    linhas = []
    for qid, _, dono, tipo, superficie in consultas:
        if tipo != "negativo":
            continue
        r = _resolver(qid, pos_sim, neg_sim, False)
        if r and r[1] == dono:
            titulo = next(e.canonical_title for e in entidades if str(e.entity_key) == dono)
            linhas.append((r[0], superficie, titulo))
    for sim, termo, titulo in sorted(linhas, reverse=True)[:20]:
        print(f"  {sim:.3f}  {termo!r:34} -> {titulo!r}")


if __name__ == "__main__":
    asyncio.run(main())
