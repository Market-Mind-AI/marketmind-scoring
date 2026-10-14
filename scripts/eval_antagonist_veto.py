"""Avalia o veto por produto antagônico contra o catálogo gerado pelo pipeline real.

ant pergunta: dado um produto e o antagônico dele (`air fryer` x `fritadeira a
óleo`), o sistema separa os dois para consultas que NUNCA viu, em idiomas que o
catálogo nem tem?

ETAPAS

  1. GERAÇÃO. O EnrichmentClient de produção (o mesmo de /analyze) gera os
     `localized_markets` de cada produto de evaluation/antagonist_cases.py —
     vocabulário, termos excluídos e antagônicos — para BRA, ESP, DEU e USA.
     Fica em cache no JSON de `--cache`; só a primeira execução paga.
  2. CATÁLOGO. TRUNCA o catálogo do banco de teste e cria cada produto pelo
     `CatalogService.resolve` real, com vetores da Voyage.
  3. MEDIDAS SEM LLM, por consulta, contra a entidade-alvo:
       - veto relativo: dist(antagônico mais próximo) <= dist(termo mais próximo)?
       - o melhor LIMIAR ABSOLUTO possível sobre dist(termo mais próximo),
         escolhido olhando o gabarito — o teto do que um limiar consegue;
       - recall@1/@5 da busca semântica (passo 3a) para as consultas `same`;
       - o que o passo 2 (trigram + veto por forma) teria aceitado.
  4. PONTA ant PONTA (`--e2e`). `resolve` de cada consulta com o LLM real do
     passo 3, com e sem o veto semântico, sobre um catálogo recriado para cada
     modo. Conta fusões erradas (antagônico resolvido como a entidade) e
     acertos perdidos.

ESCREVE NO BANCO: só roda contra um banco cujo nome contém "test".

Uso:
    DATABASE_NAME=marketmind_test python scripts/eval_antagonist_veto.py
    DATABASE_NAME=marketmind_test python scripts/eval_antagonist_veto.py --e2e
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.repositories.product_repository import ProductRepository  # noqa: E402
from app.services.catalog_service import CatalogService  # noqa: E402
from app.services.embedding_client import EmbeddingClient, vector_literal  # noqa: E402
from app.services.enrichment_client import EnrichmentClient  # noqa: E402
from app.services.product_identification_service import _market_to_dict  # noqa: E402
from app.utils.text import normalize_alias  # noqa: E402
from evaluation.antagonist_cases import Produto  # noqa: E402
from evaluation.veto_rules import REGRAS, classificacao_seletiva, normalizar, pureza_leave_one_out  # noqa: E402

_AVALIACAO = Path(__file__).resolve().parents[1] / "evaluation"
# `sinteticos`: consultas escritas à mão (evaluation/antagonist_cases.py).
# `reais`: buscas reais da keyword_related, rotuladas (antagonist_real_cases.py).
_CASOS = {"sinteticos": "evaluation.antagonist_cases", "reais": "evaluation.antagonist_real_cases"}
PRODUTOS: list[Produto] = []
IDIOMAS: list[str] | None = None
MERCADOS: list[tuple[str, str]] = []


# --- 1. geração -----------------------------------------------------------------------------------


async def gerar_mercados(cache: Path) -> dict[str, dict[str, Any]]:
    existentes: dict[str, dict[str, Any]] = json.loads(cache.read_text()) if cache.exists() else {}
    faltam = [p for p in PRODUTOS if p.nome not in existentes]
    if faltam:
        print(f"Gerando vocabulário de {len(faltam)} produtos com {settings.enrichment_llm_model}...")
        cliente = EnrichmentClient()
        for produto in faltam:
            mercados = await cliente.localized_markets(
                product_name=produto.nome, brand=None, category=produto.categoria, countries=MERCADOS
            )
            existentes[produto.nome] = {m.country: _market_to_dict(m) for m in mercados}
            cache.write_text(json.dumps(existentes, ensure_ascii=False, indent=1))
            antag = sorted({t for m in mercados for t in m.antagonist_terms})
            print(f"  {produto.nome:<22} antagônicos: {antag}")
    return existentes


# --- 2. catálogo ----------------------------------------------------------------------------------


async def montar_catalogo(sessoes: async_sessionmaker[AsyncSession], mercados: dict[str, Any]) -> dict[str, Any]:
    async with sessoes() as s:
        await s.execute(
            text("TRUNCATE product_catalog CASCADE")
        )
        await s.commit()
    chaves: dict[str, Any] = {}
    for produto in PRODUTOS:
        async with sessoes() as s:
            # Sem LLM na criação: o catálogo está vazio do produto, e o passo 3
            # só serviria para escolher entre os já criados — o que é o teste.
            servico = CatalogService(s, matcher=_SemMatch())
            resposta = await servico.resolve(
                produto.nome, source="eval", category=produto.categoria, localized_markets=mercados[produto.nome]
            )
            chaves[produto.nome] = resposta.entity_key
    async with sessoes() as s:
        await CatalogService(s).backfill_embeddings()
        contagem = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM product_aliases), "
                    "(SELECT count(*) FROM product_excluded_terms WHERE kind = 'antagonist'), "
                    "(SELECT count(*) FROM product_excluded_terms WHERE kind = 'excluded')"
                )
            )
        ).one()
    print(f"Catálogo: {len(chaves)} produtos, {contagem[0]} aliases, {contagem[1]} antagônicos, "
          f"{contagem[2]} excluídos de uso")
    return chaves


class _SemMatch:
    async def pick(self, **_: Any) -> None:
        return None


# --- 3. medidas sem LLM ---------------------------------------------------------------------------


def _consultas() -> list[tuple[str, str, str, str]]:
    """(produto, consulta, esperado, idioma)."""
    return [
        (p.nome, q, tipo, IDIOMAS[i] if IDIOMAS else "real")
        for p in PRODUTOS
        for tipo, lista in (("same", p.same), ("antagonist", p.antagonist))
        for i, q in enumerate(lista)
    ]


async def medir(sessoes: async_sessionmaker[AsyncSession], chaves: dict[str, Any]) -> list[dict[str, Any]]:
    consultas = _consultas()
    embedder = EmbeddingClient()
    vetores = await embedder.embed([normalize_alias(q) for _, q, _, _ in consultas], input_type="query")
    linhas: list[dict[str, Any]] = []
    async with sessoes() as s:
        repo = ProductRepository(s)
        indexados = {
            r[0]
            for r in await s.execute(
                text(
                    "SELECT alias_normalized FROM product_aliases "
                    "UNION SELECT term_normalized FROM product_excluded_terms"
                )
            )
        }
        for (produto, consulta, esperado, idioma), vetor in zip(consultas, vetores, strict=True):
            alvo = chaves[produto]
            literal = vector_literal(vetor)
            polaridade = (await repo.polarity(literal, model=embedder.model, entity_keys=[alvo])).get(alvo)
            vizinhos = await repo.semantic_search(literal, model=embedder.model, top_k=5)
            trigram = await repo.trigram_search(normalize_alias(consulta), category=None)
            rank = next((i + 1 for i, (e, _) in enumerate(vizinhos) if e.entity_key == alvo), None)
            trigram_aceita = trigram.match is not None and trigram.match.product.entity_key == alvo
            await s.rollback()  # os SET LOCAL de cada busca não vazam para a próxima
            linhas.append(
                {
                    "produto": produto,
                    "consulta": consulta,
                    "esperado": esperado,
                    "idioma": idioma,
                    "inedita": normalize_alias(consulta) not in indexados,
                    "d_pos": polaridade.distancia_positivo if polaridade else None,
                    "d_ant": polaridade.distancia_antagonico if polaridade else None,
                    "termo_pos": polaridade.termo_positivo if polaridade else None,
                    "termo_ant": polaridade.termo_antagonico if polaridade else None,
                    "vetada": polaridade.vetada if polaridade else None,
                    "rank": rank,
                    "trigram_aceita_alvo": trigram_aceita,
                    "vetor": vetor,
                }
            )
    return linhas


def relatorio(linhas: list[dict[str, Any]]) -> None:
    validas = [r for r in linhas if r["vetada"] is not None]
    sem_antag = sorted({r["produto"] for r in linhas if r["vetada"] is None})
    if sem_antag:
        print(f"\n!! Produtos sem antagônico vetorizado (fora da medida): {sem_antag}")

    def acerto(r: dict[str, Any]) -> bool:
        return r["vetada"] == (r["esperado"] == "antagonist")

    print("\n== Veto relativo (vetada se dist. ao antagônico <= dist. ao produto) ==")
    for nome, grupo in (("todas", validas), ("só inéditas", [r for r in validas if r["inedita"]])):
        same = [r for r in grupo if r["esperado"] == "same"]
        ant = [r for r in grupo if r["esperado"] == "antagonist"]
        print(
            f"  {nome:<12} acurácia {sum(map(acerto, grupo))}/{len(grupo)} "
            f"({sum(map(acerto, grupo)) / max(len(grupo), 1):.1%}) | "
            f"antagônico vetado {sum(r['vetada'] for r in ant)}/{len(ant)} | "
            f"produto preservado {sum(not r['vetada'] for r in same)}/{len(same)}"
        )

    if IDIOMAS:
        print("\n  Por idioma (inéditas):")
        por_idioma: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in validas:
            if r["inedita"]:
                por_idioma[r["idioma"]].append(r)
        for idioma in IDIOMAS:
            g = por_idioma[idioma]
            print(f"    {idioma}  {sum(map(acerto, g))}/{len(g)}")

    print("\n  Por produto:")
    por_produto: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in validas:
        por_produto[r["produto"]].append(r)
    for produto, g in por_produto.items():
        print(f"    {produto:<22} {sum(map(acerto, g))}/{len(g)}")

    erros = [r for r in validas if not acerto(r)]
    if erros:
        print("\n  Erros:")
        for r in erros:
            print(
                f"    [{r['esperado']:<10}] {r['consulta']!r} -> {r['produto']}: produto {r['d_pos']:.3f} "
                f"({r['termo_pos']}) | antagônico {r['d_ant']:.3f} ({r['termo_ant']})"
            )

    def margem(tipo: str) -> float:
        return statistics.median(r["d_ant"] - r["d_pos"] for r in validas if r["esperado"] == tipo)

    print(f"\n  Margem d_ant - d_pos (mediana): same {margem('same'):+.3f}, antagonist {margem('antagonist'):+.3f}")

    print("\n== Limiar ABSOLUTO em dist. ao produto, o melhor possível (escolhido olhando o gabarito) ==")
    melhor = (0.0, 0.0)
    for limiar in sorted({r["d_pos"] for r in validas}):
        ac = sum((r["d_pos"] <= limiar) == (r["esperado"] == "same") for r in validas) / len(validas)
        melhor = max(melhor, (ac, limiar))
    print(f"  acurácia {melhor[0]:.1%} com limiar {melhor[1]:.3f} — teto, com vazamento do gabarito")

    same = [r for r in linhas if r["esperado"] == "same"]
    print("\n== Recuperação (passo 3a), consultas `same` ==")
    print(f"  recall@1 {sum(r['rank'] == 1 for r in same)}/{len(same)} | "
          f"recall@5 {sum(r['rank'] is not None for r in same)}/{len(same)}")
    falhas = [r for r in same if r["rank"] != 1]
    for r in falhas:
        print(f"    {r['consulta']!r} -> {r['produto']}: rank {r['rank']}")

    ant = [r for r in linhas if r["esperado"] == "antagonist"]
    aceitos = [r for r in ant if r["trigram_aceita_alvo"]]
    print("\n== Passo 2 sozinho (trigram + veto por forma), consultas `antagonist` ==")
    print(f"  aceitaria a fusão errada em {len(aceitos)}/{len(ant)}")
    for r in aceitos:
        veto = "vetada pelo significado" if r["vetada"] else "NÃO vetada pelo significado"
        print(f"    {r['consulta']!r} -> {r['produto']} ({veto})")


# --- 3b. ablação das regras de decisão ------------------------------------------------------------


async def _vetores_por_entidade(
    sessoes: async_sessionmaker[AsyncSession],
) -> tuple[dict[Any, np.ndarray], dict[Any, np.ndarray]]:
    def parse(texto: str) -> np.ndarray:
        return np.fromstring(texto[1:-1], sep=",")

    positivos: dict[Any, list[np.ndarray]] = defaultdict(list)
    antagonicos: dict[Any, list[np.ndarray]] = defaultdict(list)
    async with sessoes() as s:
        for chave, v in await s.execute(
            text("SELECT entity_key, embedding::text FROM product_aliases WHERE embedding_model = :m"),
            {"m": settings.voyage_embedding_model},
        ):
            positivos[chave].append(parse(v))
        for chave, v in await s.execute(
            text(
                "SELECT entity_key, embedding::text FROM product_excluded_terms "
                "WHERE kind = 'antagonist' AND embedding_model = :m"
            ),
            {"m": settings.voyage_embedding_model},
        ):
            antagonicos[chave].append(parse(v))
    return (
        {k: normalizar(np.vstack(v)) for k, v in positivos.items()},
        {k: normalizar(np.vstack(v)) for k, v in antagonicos.items()},
    )


async def ablacao(
    sessoes: async_sessionmaker[AsyncSession],
    chaves: dict[str, Any],
    linhas: list[dict[str, Any]],
    *,
    banda: float | None,
) -> None:
    """As regras de evaluation/veto_rules.py sobre os MESMOS vetores das medidas
    acima. Nada aqui chama a Voyage de novo: os vetores da consulta vieram de
    `medir` e os das entidades, do banco."""
    pos, ant = await _vetores_por_entidade(sessoes)
    validas = [r for r in linhas if chaves[r["produto"]] in ant and chaves[r["produto"]] in pos]
    gabarito = np.array([1 if r["esperado"] == "same" else -1 for r in validas])
    inedita = np.array([r["inedita"] for r in validas])
    escores: dict[str, np.ndarray] = {}
    print(f"\n== Ablação das regras de decisão ({len(validas)} consultas, {inedita.sum()} inéditas) ==")
    print(f"  {'regra':<10}{'acurácia':>10}{'inéditas':>10}{'antag. vetado':>15}{'produto preservado':>20}")
    for nome, regra in REGRAS.items():
        e = np.array(
            [
                regra(normalizar(np.array(r["vetor"])), pos[chaves[r["produto"]]], ant[chaves[r["produto"]]])
                for r in validas
            ]
        )
        escores[nome] = e
        ok = np.sign(e) == gabarito
        print(
            f"  {nome:<10}{ok.mean():>10.1%}{ok[inedita].mean():>10.1%}"
            f"{(e[gabarito < 0] < 0).sum():>8}/{(gabarito < 0).sum():<6}"
            f"{(e[gabarito > 0] > 0).sum():>10}/{(gabarito > 0).sum():<6}"
        )
    for nome in ("nn", "axis"):
        erros = [r for r, e, g in zip(validas, escores[nome], gabarito, strict=True) if np.sign(e) != g]
        if erros:
            print(f"\n  Erros de `{nome}`:")
            for r, e in [(r, escores[nome][validas.index(r)]) for r in erros]:
                print(f"    [{r['esperado']:<10}] {r['consulta']!r} -> {r['produto']}: escore {e:+.3f}")

    print("\n== Pureza do conjunto de contraste (leave-one-out no eixo) ==")
    for produto, chave in chaves.items():
        if chave in ant and chave in pos:
            n = pureza_leave_one_out(pos[chave], ant[chave])
            print(f"  {produto:<26} {n:>2}/{len(ant[chave]):<3} antagônicos projetam no lado do produto")

    print("\n== Classificação seletiva: abster-se (mandar ao LLM) quando |escore| <= t ==")
    for nome in ("nn", "knn", "lda"):
        curva = classificacao_seletiva(escores[nome], gabarito)
        pontos = []
        for cobertura_alvo in (0.95, 0.9, 0.8, 0.7):
            ponto = min(curva, key=lambda c: abs(c[1] - cobertura_alvo))
            pontos.append(f"{ponto[1]:.0%}->{ponto[2]:.1%}")
        perfeitos = [c for c in curva if c[2] == 1.0]
        perfeito = (
            f"100% a partir de t={perfeitos[0][0]:.3f} (cobre {perfeitos[0][1]:.0%})" if perfeitos else "nunca 100%"
        )
        print(f"  {nome:<5} cobertura->acurácia: {'  '.join(pontos)} | {perfeito}")
    regra_banda = "knn"
    curva = classificacao_seletiva(escores[regra_banda], gabarito)
    print(f"\n  Curva fina de `{regra_banda}` (t -> cobertura -> acurácia dos decididos):")
    print("   " + "  ".join(f"{c[0]:.3f}->{c[1]:.0%}->{c[2]:.1%}" for c in curva[:: max(1, len(curva) // 16)]))
    if banda is not None:
        decididos = np.abs(escores[regra_banda]) > banda
        ok = (np.sign(escores[regra_banda]) == gabarito)[decididos]
        print(
            f"  Banda FIXA t = {banda:.3f} (calibrada noutro conjunto): cobre {decididos.mean():.1%}, "
            f"acerta {ok.mean():.1%} dos decididos ({(~ok).sum()} erros)"
        )
        for r, e, d, g in zip(validas, escores[regra_banda], decididos, gabarito, strict=True):
            if d and np.sign(e) != g:
                print(f"    erro fora da banda: [{r['esperado']}] {r['consulta']!r} escore {e:+.3f}")


# --- 4. ponta a ponta -----------------------------------------------------------------------------


async def ponta_a_ponta(
    sessoes: async_sessionmaker[AsyncSession], mercados: dict[str, Any], com_veto: bool
) -> dict[str, Any]:
    chaves = await montar_catalogo(sessoes, mercados)
    contagem: dict[str, int] = defaultdict(int)
    erros: list[str] = []
    latencias: list[float] = []
    # ant mediana sozinha engana: com ~metade das consultas indo ao LLM, cinco a
    # mais bastam para ela pular do caminho do trigram (~0,7 s) para o do LLM
    # (~4 s). Contar as chamadas é o que mostra o custo de verdade.
    chamadas = {"llm": 0, "embedding_consulta": 0}
    for produto, consulta, esperado, _ in _consultas():
        async with sessoes() as s:
            servico = CatalogService(s)
            servico._matcher = _ContaLLM(servico._matcher, chamadas)  # type: ignore[assignment]
            servico._embedder = _ContaEmbedding(chamadas)
            if not com_veto:
                servico._veto_semantico = _nunca  # type: ignore[method-assign]
                servico._evidencias_de_polaridade = _nenhuma  # type: ignore[method-assign]
            inicio = time.perf_counter()
            resposta = await servico.resolve(consulta, source="eval")
            latencias.append(time.perf_counter() - inicio)
        caiu_no_alvo = resposta.entity_key == chaves[produto]
        if esperado == "antagonist":
            chave = "fusao_errada" if caiu_no_alvo else "antagonico_separado"
        else:
            chave = "acerto" if caiu_no_alvo else "acerto_perdido"
        contagem[chave] += 1
        if chave in ("fusao_errada", "acerto_perdido"):
            erros.append(f"[{chave}] {consulta!r} -> {resposta.canonical_title!r} via {resposta.method}")
    latencias.sort()
    return {
        "contagem": dict(contagem),
        "erros": erros,
        "chamadas": chamadas,
        "latencia_s": {
            "media": statistics.mean(latencias),
            "mediana": statistics.median(latencias),
            "p90": latencias[int(len(latencias) * 0.9)],
        },
    }


class _ContaLLM:
    def __init__(self, matcher: Any, chamadas: dict[str, int]) -> None:
        self._matcher, self._chamadas = matcher, chamadas

    async def pick(self, **kwargs: Any) -> Any:
        if kwargs.get("candidates"):
            self._chamadas["llm"] += 1
        return await self._matcher.pick(**kwargs)


class _ContaEmbedding(EmbeddingClient):
    def __init__(self, chamadas: dict[str, int]) -> None:
        super().__init__()
        self._chamadas = chamadas

    async def embed(self, texts: list[str], *, input_type: Any) -> list[list[float]]:
        if input_type == "query":
            self._chamadas["embedding_consulta"] += 1
        return await super().embed(texts, input_type=input_type)


async def _nunca(*_: Any, **__: Any) -> bool:
    return False


async def _nenhuma(*_: Any, **__: Any) -> dict:
    return {}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--casos", choices=sorted(_CASOS), default="sinteticos")
    parser.add_argument("--cache", type=Path, help="JSON do vocabulário gerado (padrão: um por conjunto de casos)")
    parser.add_argument("--e2e", action="store_true", help="resolve com o LLM real, com e sem veto (custa)")
    parser.add_argument("--json", type=Path, help="grava as medidas por consulta")
    parser.add_argument(
        "--banda", type=float, default=None,
        help="limiar de abstenção FIXO para a regra `knn` (calibrado noutro conjunto); sem ele, só a curva",
    )
    args = parser.parse_args()

    if "test" not in settings.database_name.lower():
        raise SystemExit(f"Recusado: {settings.database_name!r} não é banco de teste (este script TRUNCA o catálogo)")

    global PRODUTOS, IDIOMAS, MERCADOS
    modulo = importlib.import_module(_CASOS[args.casos])
    PRODUTOS, IDIOMAS, MERCADOS = modulo.PRODUTOS, modulo.IDIOMAS, modulo.MERCADOS
    cache = args.cache or _AVALIACAO / f"antagonist_markets_{args.casos}.json"
    mercados = await gerar_mercados(cache)
    engine = create_async_engine(settings.database_url)
    sessoes = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    chaves = await montar_catalogo(sessoes, mercados)
    linhas = await medir(sessoes, chaves)
    relatorio(linhas)
    await ablacao(sessoes, chaves, linhas, banda=args.banda)
    if args.json:
        for r in linhas:
            r.pop("vetor", None)
        args.json.write_text(json.dumps(linhas, ensure_ascii=False, indent=1, default=str))

    if args.e2e:
        for com_veto in (False, True):
            resultado = await ponta_a_ponta(sessoes, mercados, com_veto)
            print(f"\n== Ponta a ponta, {'COM' if com_veto else 'SEM'} veto semântico ==")
            lat = resultado["latencia_s"]
            print(
                f"  {resultado['contagem']}\n  chamadas {resultado['chamadas']} | latência média {lat['media']:.2f}s, "
                f"mediana {lat['mediana']:.2f}s, p90 {lat['p90']:.2f}s"
            )
            for erro in resultado["erros"]:
                print(f"    {erro}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
