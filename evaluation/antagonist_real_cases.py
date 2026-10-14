"""Buscas REAIS de usuários, rotuladas à mão, para a avaliação do veto antagônico.

Origem: a tabela `keyword_related` do coletor Google Ads (marketmind-pytrends) no
ambiente azure-dev, lida em 13/10/2026 — as buscas relacionadas que a DataForSEO
devolveu para as sementes de cada produto, nos mercados que o projeto já
analisou (US, BR, DE, IN, GB, JP). Para cada produto foram puxadas as 45 de maior
volume e as 45 de maior volume que contêm o atributo oposto ("wired", "com fio",
"deep", "3d", "bourbon", "moka"...). Duplicatas removidas.

Rótulo, por busca:
  same        a busca é o produto (variante, erro de digitação, outro idioma,
              qualificador que não muda o produto: "best", "price").
  antagonist  a busca é o produto que se confunde com ele e é outro.
  fora        acessório, serviço, marca/modelo, subtipo, mais amplo, ambíguo —
              não entra na avaliação.
Só `same` e `antagonist` estão abaixo. Os rótulos são de uma pessoa só e são a
parte mais frágil da medida: revisar antes de citar.

Casos difíceis de propósito: `real looking christmas tree` é a árvore ARTIFICIAL
(carrega a palavra do antagônico e é o produto); `fritadeira elétrica` ficou de
fora por ser ambígua no Brasil (diz-se das duas).
"""

from evaluation.antagonist_cases import Produto

MERCADOS = [("BRA", "pt"), ("ESP", "es"), ("DEU", "de"), ("USA", "en")]

PRODUTOS: list[Produto] = [
    Produto(
        "air fryer",
        "Kitchen Appliances",
        same=["air fryer", "airfri", "air fri", "air fryers", "airfryert", "airfryer", "heißluftfritteuse",
              "heißluft fritteuse", "ノンフライヤー", "ノン フライヤー", "airefrier", "ir frier", "ait frier",
              "air frier", "portable air fryer", "non toxic air fryer", "air fryer price", "best air fryer"],
        antagonist=["deep fryer", "turkey deep fryer", "turkey fryer"],
    ),
    Produto(
        "wireless mouse",
        "Computer Accessories",
        same=["wireless mouse", "wireless mice", "mouse sem fio", "mousse sem fio"],
        antagonist=["wired mouse", "mouse com fio", "wired mice", "wired computer mouse", "wired computer mice",
                    "wired mouse computer", "mouse wired", "wired mouse for laptop", "best wired mouse",
                    "top rated wired mouse", "best wired mice", "best wired computer mouse"],
    ),
    Produto(
        "artificial christmas tree",
        "Home Decor",
        same=["faux christmas tree", "fake xmas tree", "synthetic christmas tree", "artificial christmas tree",
              "imitation christmas trees", "faux xmas trees", "imitation xmas trees", "künstliche weihnachtsbäume",
              "künstlicher weihnachtsbaum", "realistic artificial christmas tree", "realistic faux christmas tree",
              "real looking xmas trees", "real looking christmas tree", "fake xmas trees that look real",
              "artificial trees that look real"],
        antagonist=["potted real christmas trees"],
    ),
    Produto(
        "printer",
        "Office Equipment",
        same=["impressora", "impressoras", "printers", "printer", "printer price", "cost of printer"],
        antagonist=["3d printer", "3 d printer", "three d printer", "3d drucker", "resin 3d printer",
                    "desktop 3d printer", "metal 3d printer", "best 3d printer"],
    ),
    Produto(
        "scotch whisky",
        "Spirits",
        same=["scotch whisky", "scotch whiskey"],
        antagonist=["bourbon", "bourbon whiskey", "tennessee whiskey", "irish whiskey", "canadian whiskey",
                    "japanese whisky", "rye whiskey", "bourbons", "premium bourbon", "wheated bourbon",
                    "single barrel bourbon"],
    ),
    Produto(
        "espresso machine",
        "Coffee Machines",
        same=["espresso machine", "espresso maker", "espresso maker machine", "cafeteira espresso",
              "cafeteira expresso", "cafeteira espressa", "home espresso machine", "household espresso machine",
              "house espresso maker", "espresso makers for home", "compact espresso machine", "compact espresso maker"],
        antagonist=["moka pot", "italian moka", "moka machine", "percolators", "portable coffee percolator"],
    ),
]

IDIOMAS: list[str] | None = None
