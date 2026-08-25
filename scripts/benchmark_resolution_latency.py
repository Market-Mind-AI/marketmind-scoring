"""Mede a LATÊNCIA do passo 2 em catálogo grande, com dado sintético.

Complementa `benchmark_catalog_resolution.py`, que mede ACERTO com o catálogo
real (pequeno). Aqui a pergunta é outra: a resolução continua barata quando o
catálogo cresce?

POR QUE ISTO EXISTE. Até 2026-08-25 o passo 2 filtrava com
`similarity(alias, texto) >= limiar`, que nenhum índice atende — o Postgres
calculava a similaridade para CADA alias, em toda consulta. Com 9 produtos isso
é invisível; a forma é O(aliases) e só aparece em escala. Medido aqui com
2.000 produtos e 500.000 aliases: 268 ms por resolução. Trocando para o
operador `%`, que usa o índice GIN trigram, a mesma resolução (já incluindo o
veto por evidência negativa, que antes nem existia) leva ~2 ms.

ESCREVE DADO SINTÉTICO. Só roda contra um banco cujo nome contém "test": ele
TRUNCA o catálogo antes de semear.

Uso:
    DATABASE_NAME=marketmind_test python scripts/benchmark_resolution_latency.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.repositories.product_repository import ProductRepository  # noqa: E402

PRODUTOS = 2_000
ALIASES_POR_PRODUTO = 250
EXCLUIDOS_POR_PRODUTO = 8

_SEED = """
TRUNCATE product_excluded_terms, product_aliases, product_catalog CASCADE;

INSERT INTO product_catalog (entity_key, canonical_title, category, created_at)
SELECT gen_random_uuid(), 'produto ' || i, 'cat ' || (i % 40), now()
FROM generate_series(1, :produtos) i;

-- Vocabulário diverso de propósito: um catálogo real é de produtos distintos,
-- e é aí que o índice tem valor (descartar o que não parece).
INSERT INTO product_aliases (id, alias_normalized, entity_key, source, hit_count, first_seen_at)
SELECT gen_random_uuid(),
       substr(md5(pc.entity_key::text || v), 1, 9) || ' ' || substr(md5(v::text || pc.entity_key::text), 1, 7),
       pc.entity_key, 'seed', 1, now()
FROM product_catalog pc, generate_series(1, :aliases) v;

INSERT INTO product_excluded_terms (id, entity_key, term_normalized, term, source, first_seen_at)
SELECT gen_random_uuid(), pc.entity_key,
       substr(md5('x' || pc.entity_key::text || v), 1, 9) || ' excluido', 'excluido ' || v, 'seed', now()
FROM product_catalog pc, generate_series(1, :excluidos) v;

-- Uma entidade com vocabulário realista e a vizinha oposta dela, para exercitar
-- os três desfechos: aceite, veto e nada.
INSERT INTO product_aliases (id, alias_normalized, entity_key, source, hit_count, first_seen_at)
SELECT gen_random_uuid(), t, pc.entity_key, 'seed', 1, now()
FROM (SELECT entity_key FROM product_catalog LIMIT 1) pc,
     unnest(ARRAY['air fryer','fritadeira sem oleo','freidora sin aceite','fritadeira de ar quente']) t;
INSERT INTO product_excluded_terms (id, entity_key, term_normalized, term, source, first_seen_at)
SELECT gen_random_uuid(), pc.entity_key, t, t, 'seed', now()
FROM (SELECT entity_key FROM product_catalog LIMIT 1) pc,
     unnest(ARRAY['fritadeira de oleo','freidora de aceite']) t;

ANALYZE product_catalog;
ANALYZE product_aliases;
ANALYZE product_excluded_terms;
"""

CONSULTAS = [
    ("fritadeira sem oleo quente", "deve casar"),
    ("freidora de aceite", "deve ser vetada"),
    ("produto que nao existe xyz", "não deve casar"),
]


async def main() -> None:
    if "test" not in settings.database_name.lower():
        raise SystemExit(
            f"Recusando rodar contra '{settings.database_name}': este script TRUNCA o catálogo. "
            "Use DATABASE_NAME=marketmind_test."
        )

    engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        print(f"semeando {PRODUTOS} produtos x {ALIASES_POR_PRODUTO} aliases...")
        for comando in _SEED.strip().split(";\n"):
            if comando.strip():
                await session.execute(
                    text(comando),
                    {"produtos": PRODUTOS, "aliases": ALIASES_POR_PRODUTO, "excluidos": EXCLUIDOS_POR_PRODUTO},
                )
        await session.commit()

        totais = (await session.execute(text("SELECT (SELECT count(*) FROM product_aliases)"))).scalar_one()
        print(f"{totais} aliases indexados\n")

        repo = ProductRepository(session)
        await repo.trigram_search("aquecimento")  # descarta a primeira, que paga o plano
        await session.rollback()
        for consulta, esperado in CONSULTAS:
            inicio = time.perf_counter()
            resultado = None
            for _ in range(10):
                resultado = await repo.trigram_search(consulta)
                await session.rollback()
            ms = (time.perf_counter() - inicio) * 100
            estado = "match" if resultado.match else ("vetado" if resultado.vetoed else "nada")
            print(f"  {ms:6.1f} ms/consulta   {consulta!r:30} -> {estado:8} ({esperado})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
