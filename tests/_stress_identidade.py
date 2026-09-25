"""Stress da IDENTIDADE do catálogo — sem custo de DataForSEO.

NÃO roda na suíte (nome começa com `_`): cada caso que chega ao passo 3 é uma
chamada de LLM, e a suíte tem de ser gratuita e instantânea. Roda à mão:

    DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test \
        .venv/bin/python -m tests._stress_identidade


A resolução é onde todos os defeitos de hoje nasceram, e ela não gasta nada na
DataForSEO: só banco e uma chamada barata de LLM quando alias e trigram falham.
Então dá para exercitá-la à vontade antes de gastar com análise de verdade.

Cada caso declara o que TEM de ser verdade, e a saída diz o que falhou.
"""

import asyncio
from collections import defaultdict

from app.db.session import async_session_factory
from app.services.catalog_service import CatalogService


def s(nome: str) -> str:
    """Sem sufixo, de propósito.

    A primeira versão punha um sufixo por execução para o catálogo persistente
    não resolver tudo pelo alias da rodada anterior. Só que o sufixo é uma
    substring longa COMPARTILHADA por todos os casos, e o passo 2 mede
    semelhança de texto: `air purifier X` x `air fryer X` casou por causa do
    X, não do produto. O teste acusava um defeito que era dele.

    A resposta certa é catálogo limpo a cada rodada — ver `_limpar`.
    """
    return nome


async def _limpar(sessao) -> None:  # noqa: ANN001
    """Catálogo vazio antes de medir. Sem isto a rodada seguinte mede o cache
    da anterior em vez da decisão."""
    from sqlalchemy import text

    for tabela in ("product_excluded_terms", "product_aliases", "brand_markets",
                   "brand_reference_candidates", "brand_champions", "product_catalog", "brands"):
        await sessao.execute(text(f"DELETE FROM {tabela}"))  # noqa: S608 — lista literal
    await sessao.commit()


# (rótulo, texto, marca, categoria)
CASOS = [
    # A — mesma coisa escrita de jeitos diferentes: UM produto
    ("A1", s("air fryer"), None, "Home appliances"),
    ("A2", s("airfryer"), None, "Home appliances"),
    ("A3", s("air-fryer"), None, "Home appliances"),
    # B — marca sobre o mesmo genérico: UM produto, marcas ao lado
    ("B1", s("air fryer"), "Philips", "Home appliances"),
    ("B2", s("air fryer"), "Ninja", "Home appliances"),
    # C — produtos VIZINHOS da mesma marca: produtos DIFERENTES
    ("C1", s("air purifier"), "Philips", "Home appliances"),
    # D — sub-produto x categoria: entidades DIFERENTES, uma pendurada na outra
    ("D1", s("coffee"), None, "Food and drink"),
    ("D2", s("instant coffee"), None, "Food and drink"),
    ("D3", s("espresso machine"), None, "Home appliances"),
    # E — mesmo produto em outro idioma: UM produto (decisão do LLM)
    ("E1", s("fritadeira elétrica"), None, "Home appliances"),
    ("E2", s("freidora de aire"), None, "Home appliances"),
    # F — palavras que colidem mas são outra coisa
    ("F1", s("jaguar"), None, "Automotive"),
    ("F2", s("jaguar"), None, "Wildlife"),
    # G — homônimos entre categorias (o caso `jaguar`, generalizado)
    ("G1", s("apple"), None, "Food and drink"),
    ("G2", s("apple"), None, "Consumer electronics"),
    ("G3", s("galaxy"), None, "Consumer electronics"),
    ("G4", s("galaxy"), None, "Food and drink"),
    ("G5", s("java"), None, "Food and drink"),
    ("G6", s("java"), None, "Software"),
    # H — mesma categoria, produtos vizinhos que a forma quase funde
    ("H1", s("olive oil"), None, "Food and drink"),
    ("H2", s("extra virgin olive oil"), None, "Food and drink"),
    ("H3", s("palm oil"), None, "Food and drink"),
    ("H4", s("hair dryer"), None, "Personal care"),
    ("H5", s("hair straightener"), None, "Personal care"),
    # I — outros idiomas e alfabetos do MESMO produto
    ("I1", s("ノンフライヤー"), None, "Home appliances"),
    ("I2", s("مقلاة هوائية"), None, "Home appliances"),
    # Ucraniano: фритюр é fritura EM ÓLEO, então isto é a fritadeira de óleo.
    # Air fryer em ucraniano seria `аерофритюрниця`. A primeira versão deste
    # teste esperava que caísse em `air fryer` — a expectativa é que estava
    # errada, não o sistema.
    ("I3", s("фритюрниця"), None, "Home appliances"),
    # J — plural, abreviação, erro de digitação
    ("J1", s("sneakers"), None, "Apparel"),
    ("J2", s("sneaker"), None, "Apparel"),
    ("J3", s("running shoes"), None, "Apparel"),
    # K — marca cujo nome é o produto
    ("K1", s("thermos"), None, "Home appliances"),
    ("K2", s("vacuum flask"), None, "Home appliances"),
    # L — produto composto x componente
    ("L1", s("coffee machine"), None, "Home appliances"),
    ("L2", s("coffee filter"), None, "Home appliances"),
]

# Sub-produto: entidade PRÓPRIA, mas pendurada no produto mais amplo. Nem
# fusão (perderia a demanda própria) nem produto solto (perderia o contexto de
# categoria contra o qual ele se lê).
SUBPRODUTO = [("D2", "D1"), ("H2", "H1")]

# Quem TEM de cair junto, e quem TEM de ficar separado.
MESMO = [
    ("A1", "A2", "A3"),
    ("A1", "B1", "B2"),
    # As três escritas do MESMO eletrodoméstico, em três alfabetos.
    ("A1", "E2", "I1", "I2"),
    ("J1", "J2"),
    ("K1", "K2"),
    ("E1", "I3"),  # os dois são a fritadeira DE ÓLEO, em dois idiomas
]
DIFERENTE = [
    ("A1", "C1"), ("D1", "D2"), ("D1", "D3"), ("A1", "D1"), ("F1", "F2"),
    ("G1", "G2"), ("G3", "G4"), ("G5", "G6"),
    ("H1", "H2"), ("H1", "H3"), ("H4", "H5"),
    ("J1", "J3"), ("L1", "L2"),
    ("A1", "E1"),  # air fryer x fritadeira com óleo: princípios diferentes
    ("A1", "I3"),  # e o ucraniano é a de óleo, não a de ar
]


async def main() -> int:
    chaves: dict[str, str] = {}
    marcas: dict[str, str | None] = {}
    titulos: dict[str, str] = {}
    metodos: dict[str, str] = {}
    pais_de: dict[str, str | None] = {}

    async with async_session_factory() as sessao:
        await _limpar(sessao)
        servico = CatalogService(sessao)
        for rotulo, texto, marca, categoria in CASOS:
            r = await servico.resolve(texto, source="stress", brand=marca, category=categoria)
            chaves[rotulo] = str(r.entity_key)
            marcas[rotulo] = r.brand
            titulos[rotulo] = r.canonical_title
            metodos[rotulo] = r.method
            pais_de[rotulo] = r.generic_title
            print(
                f"  {rotulo:3} {texto[:32]:<34} marca={str(marca or '-'):<9} -> "
                f"{r.canonical_title[:30]:<32} [{r.method}]"
                + (f" sob {r.generic_title!r}" if r.generic_title else "")
            )

    print()
    falhas = []
    for grupo in MESMO:
        distintas = {chaves[g] for g in grupo}
        if len(distintas) != 1:
            por_chave = defaultdict(list)
            for g in grupo:
                por_chave[chaves[g]].append(g)
            falhas.append(f"DEVIAM SER UM: {grupo} -> {dict(por_chave)}")
    for a, b in DIFERENTE:
        if chaves[a] == chaves[b]:
            falhas.append(f"DEVIAM SER DOIS: {a}={titulos[a]!r} e {b}={titulos[b]!r} caíram na mesma entidade")

    # A marca nunca pode entrar no título do catálogo.
    for rotulo, texto, marca, _ in CASOS:
        if marca and marca.lower() in titulos[rotulo].lower():
            falhas.append(f"MARCA NO TÍTULO: {rotulo} -> {titulos[rotulo]!r}")
        if marca and marcas[rotulo] != marca:
            falhas.append(f"MARCA PERDIDA: {rotulo} pediu {marca!r}, voltou {marcas[rotulo]!r}")

    for filho, pai in SUBPRODUTO:
        if chaves[filho] == chaves[pai]:
            falhas.append(f"DEVIA SER SUB-PRODUTO: {filho}={titulos[filho]!r} fundiu com {pai}={titulos[pai]!r}")
        elif pais_de.get(filho) != titulos[pai]:
            falhas.append(
                f"SUB-PRODUTO SOLTO: {filho}={titulos[filho]!r} devia estar sob {titulos[pai]!r}, "
                f"está sob {pais_de.get(filho)!r}"
            )

    print(f"casos: {len(CASOS)} | falhas: {len(falhas)}")
    for f in falhas:
        print("  FALHA:", f)
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
