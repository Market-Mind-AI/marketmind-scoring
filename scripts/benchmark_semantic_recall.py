"""Mede o passo 3a (busca semântica): HNSW contra força bruta, recall e latência.

A pergunta de um índice ANN é sempre a mesma: quanto do resultado EXATO ele
devolve (recall@k) e quanto mais rápido. Aqui as duas respostas saem do mesmo
banco, com a mesma consulta, trocando só o plano:

  exato   `SET LOCAL enable_indexscan = off` — o Postgres compara a consulta com
          TODOS os vetores (varredura sequencial). É a resposta certa por
          definição e serve de gabarito.
  HNSW    o índice da migration 052, com `hnsw.ef_search` variando. Maior
          ef_search = fila de candidatos maior no grafo = recall maior e
          consulta mais lenta. É o compromisso precisão x latência que se
          ajusta em produção (settings.catalog_semantic_ef_search).

DUAS PARTES.

  1. Catálogo real. Os aliases vetorizados viram consultas (o vetor de cada um
     contra todos os outros) e, além deles, um punhado de consultas difíceis
     do histórico do projeto (`quadcopter`, `lockencreme`...) é vetorizado na
     Voyage para mostrar QUEM volta. Com centenas de vetores a força bruta já é
     instantânea — o índice não ganha nada aqui, e o script mostra isso.

  2. Catálogo sintético (`--sintetico N`). Uma tabela TEMPORÁRIA com N vetores
     aleatórios, para ver o índice onde ele faz diferença. Vetor uniforme no
     espaço é o pior caso de um ANN (não há vizinhança de verdade), então o
     recall aqui é um piso. Também imprime o peso em memória por tipo, que é a
     outra estratégia de ANN: reduzir o vetor (`halfvec` float16, `bit`).

Não escreve em tabela nenhuma: a parte 1 só lê, a parte 2 usa TEMP TABLE, que
some com a conexão. Pode rodar contra o banco vivo.

Uso:
    python scripts/benchmark_semantic_recall.py
    python scripts/benchmark_semantic_recall.py --sintetico 100000 --k 10
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.repositories.product_repository import ProductRepository  # noqa: E402
from app.services.embedding_client import EmbeddingClient, vector_literal  # noqa: E402

EF_SEARCH = [10, 20, 40, 80, 200]

# Consultas que o trigram não resolve, tiradas das medições do projeto
# (catalog_matcher.py, docs/marketmind-transparencia-tecnica.md §1.3).
CONSULTAS_DIFICEIS = [
    "quadcopter",
    "lockencreme",
    "krulcrème",
    "odżywka bez spłukiwania",
    "heißluftfritteuse",
    "freidora de aceite",
    "instant coffee",
    "extra virgin olive oil",
]


async def _vizinhos(conn: AsyncConnection, tabela: str, consulta: str, k: int, ef: int | None) -> tuple[list, float]:
    """Top-k ids por distância coseno. `ef=None` = força bruta.

    Uma transação por medida: SET LOCAL vale até o fim da transação, e o
    `enable_indexscan = off` do gabarito não pode vazar para a medida do HNSW."""
    await conn.commit()
    async with conn.begin():
        if ef is None:
            await conn.execute(text("SET LOCAL enable_indexscan = off"))
        else:
            await conn.execute(text("SELECT set_config('hnsw.ef_search', :ef, true)"), {"ef": str(ef)})
        inicio = time.perf_counter()
        linhas = await conn.execute(
            text(
                f"SELECT id FROM {tabela} WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": consulta, "k": k},
        )
        ids = [r[0] for r in linhas]
        return ids, (time.perf_counter() - inicio) * 1000


async def _curva(conn: AsyncConnection, tabela: str, consultas: list[str], k: int) -> None:
    exatos: list[set] = []
    tempos_exato: list[float] = []
    for q in consultas:
        ids, ms = await _vizinhos(conn, tabela, q, k, None)
        exatos.append(set(ids))
        tempos_exato.append(ms)
    print(f"  {'plano':<16}{'recall@' + str(k):>10}{'ms (mediana)':>16}")
    print(f"  {'força bruta':<16}{1.0:>10.3f}{statistics.median(tempos_exato):>16.2f}")
    for ef in EF_SEARCH:
        acertos, tempos = 0, []
        for q, gabarito in zip(consultas, exatos, strict=True):
            ids, ms = await _vizinhos(conn, tabela, q, k, ef)
            acertos += len(gabarito & set(ids))
            tempos.append(ms)
        total = sum(len(g) for g in exatos) or 1
        print(f"  {'HNSW ef=' + str(ef):<16}{acertos / total:>10.3f}{statistics.median(tempos):>16.2f}")


def _memoria(n: int, dim: int) -> None:
    print(f"\n  Peso só dos vetores, {n:,} x {dim} dimensões:")
    for nome, bytes_por_dim in (("vector (float32)", 4), ("halfvec (float16)", 2), ("bit (binário)", 1 / 8)):
        total = n * dim * bytes_por_dim
        print(f"    {nome:<20}{total / 1024**2:>12,.1f} MB   ({dim * bytes_por_dim:,.0f} B por vetor)")


async def parte_real(engine: AsyncEngine, conn: AsyncConnection, k: int) -> None:
    total = (
        await conn.execute(
            text("SELECT count(*) FROM product_aliases WHERE embedding_model = :m"),
            {"m": settings.voyage_embedding_model},
        )
    ).scalar_one()
    print(f"\n== Catálogo real: {total} aliases vetorizados ({settings.voyage_embedding_model}) ==")
    if not total:
        print("  Nada vetorizado. Rode POST /admin/catalog/backfill-embeddings antes.")
        return
    amostra = [
        r[0]
        for r in await conn.execute(
            text("SELECT embedding::text FROM product_aliases WHERE embedding IS NOT NULL ORDER BY random() LIMIT 200")
        )
    ]
    await _curva(conn, "product_aliases", amostra, k)
    _memoria(total, settings.voyage_embedding_dimensions)

    embedder = EmbeddingClient()
    if not embedder.enabled:
        return
    print("\n  Consultas difíceis -> entidades mais próximas (distância coseno):")
    vetores = await embedder.embed(CONSULTAS_DIFICEIS, input_type="query")
    async with AsyncSession(engine) as session:
        repo = ProductRepository(session)
        for consulta, vetor in zip(CONSULTAS_DIFICEIS, vetores, strict=True):
            achados = await repo.semantic_search(
                vector_literal(vetor), model=embedder.model, brand_key=None, top_k=3
            )
            descricao = ", ".join(f"{e.canonical_title} ({d:.3f})" for e, d in achados) or "—"
            print(f"    {consulta:<26} {descricao}")
    print(f"  ({embedder.tokens} tokens Voyage)")


async def parte_sintetica(conn: AsyncConnection, n: int, k: int) -> None:
    dim = settings.voyage_embedding_dimensions
    print(f"\n== Catálogo sintético: {n:,} vetores aleatórios de {dim} dimensões (TEMP TABLE) ==")
    await conn.execute(text(f"CREATE TEMP TABLE sintetico (id bigint PRIMARY KEY, embedding vector({dim}))"))
    inicio = time.perf_counter()
    await conn.execute(
        text(
            f"""
            INSERT INTO sintetico
            SELECT i, (SELECT array_agg(random() - 0.5)::vector({dim}) FROM generate_series(1, {dim}) WHERE i > 0)
            FROM generate_series(1, :n) i
            """
        ),
        {"n": n},
    )
    print(f"  carga: {time.perf_counter() - inicio:.1f}s")
    inicio = time.perf_counter()
    await conn.execute(
        text("CREATE INDEX ON sintetico USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
    )
    tamanho = (await conn.execute(text("SELECT pg_size_pretty(pg_relation_size('sintetico_embedding_idx'))"))).scalar()
    print(f"  índice HNSW: {time.perf_counter() - inicio:.1f}s, {tamanho}")
    await conn.execute(text("ANALYZE sintetico"))
    aleatorio = f"(SELECT array_agg(random() - 0.5)::vector({dim}) FROM generate_series(1, {dim}) WHERE g > 0)"
    consultas = [r[0] for r in await conn.execute(text(f"SELECT {aleatorio}::text FROM generate_series(1, 50) g"))]
    await _curva(conn, "sintetico", consultas, k)
    _memoria(n, dim)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--sintetico", type=int, default=0, help="N vetores aleatórios numa tabela temporária")
    args = parser.parse_args()

    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        await parte_real(engine, conn, args.k)
        await conn.commit()
        if args.sintetico:
            # O build do HNSW em memória precisa de espaço; o default (64MB)
            # força o build para disco e o deixa muito mais lento.
            await conn.execute(text("SET maintenance_work_mem = '512MB'"))
            await parte_sintetica(conn, args.sintetico, args.k)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
