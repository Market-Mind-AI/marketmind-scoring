"""Casos da avaliação do veto por produto antagônico (scripts/eval_antagonist_veto.py).

Cada produto tem um ANTAGÔNICO: mesma categoria, quase o mesmo nome, um atributo
definidor oposto. As consultas foram escritas à mão, sem olhar o que o LLM de
enriquecimento gera, em sete idiomas. Três deles (fr, it, pl) não estão entre os
mercados do catálogo da avaliação (BRA, ESP, DEU, USA): o veto tem de funcionar
para uma língua cujo vocabulário o catálogo nunca viu.

`same` = a consulta É o produto e deve resolver para ele.
`antagonist` = a consulta é o antagônico e NÃO pode ser fundida com ele.
"""

from dataclasses import dataclass, field

MERCADOS = [("BRA", "pt"), ("ESP", "es"), ("DEU", "de"), ("USA", "en")]


@dataclass(frozen=True)
class Produto:
    nome: str
    categoria: str
    same: list[str] = field(default_factory=list)
    antagonist: list[str] = field(default_factory=list)


PRODUTOS: list[Produto] = [
    Produto(
        "air fryer",
        "Kitchen Appliances",
        same=["fritadeira elétrica de ar quente", "freidora sin aceite digital", "heißluft-fritteuse",
              "friggitrice ad aria", "frytkownica beztłuszczowa", "friteuse sans huile"],
        antagonist=["fritadeira elétrica com óleo", "freidora de aceite profesional", "fritteuse mit öl",
                    "friggitrice ad olio", "frytkownica olejowa", "friteuse à huile"],
    ),
    Produto(
        "decaf coffee",
        "Coffee",
        same=["café descafeinado em grãos", "café sin cafeína", "entkoffeinierter kaffee",
              "caffè decaffeinato", "kawa bezkofeinowa", "café décaféiné moulu"],
        antagonist=["café com cafeína", "café normal molido", "koffeinhaltiger kaffee",
                    "caffè con caffeina", "kawa z kofeiną", "café caféiné"],
    ),
    Produto(
        "wireless earbuds",
        "Audio",
        same=["fone de ouvido sem fio bluetooth", "auriculares inalámbricos", "kabellose in-ear kopfhörer",
              "auricolari wireless", "słuchawki bezprzewodowe", "écouteurs sans fil"],
        antagonist=["fone de ouvido com fio", "auriculares con cable", "kabelgebundene kopfhörer",
                    "auricolari con filo", "słuchawki przewodowe", "écouteurs filaires"],
    ),
    Produto(
        "induction cooktop",
        "Kitchen Appliances",
        same=["cooktop de indução 4 bocas", "placa de inducción", "induktionskochfeld",
              "piano cottura a induzione", "płyta indukcyjna", "plaque à induction"],
        antagonist=["cooktop a gás 5 bocas", "placa de gas butano", "gaskochfeld",
                    "piano cottura a gas", "płyta gazowa", "plaque de cuisson gaz"],
    ),
    Produto(
        "electric toothbrush",
        "Personal Care",
        same=["escova de dentes elétrica recarregável", "cepillo de dientes eléctrico", "elektrische zahnbürste",
              "spazzolino elettrico", "szczoteczka elektryczna", "brosse à dents électrique"],
        antagonist=["escova de dentes manual macia", "cepillo de dientes manual", "handzahnbürste",
                    "spazzolino manuale", "szczoteczka manualna", "brosse à dents manuelle"],
    ),
    Produto(
        "lactose-free milk",
        "Dairy",
        same=["leite zero lactose", "leche sin lactosa", "laktosefreie milch",
              "latte senza lattosio", "mleko bez laktozy", "lait sans lactose"],
        antagonist=["leite integral comum com lactose", "leche entera normal", "normale vollmilch",
                    "latte intero normale", "zwykłe mleko krowie", "lait entier classique"],
    ),
    Produto(
        "robot vacuum",
        "Home Appliances",
        same=["aspirador robô inteligente", "robot aspirador", "saugroboter",
              "robot aspirapolvere", "robot sprzątający", "aspirateur robot"],
        antagonist=["aspirador de pó vertical", "aspiradora escoba", "bodenstaubsauger mit beutel",
                    "aspirapolvere a traino", "odkurzacz pionowy", "aspirateur balai"],
    ),
    Produto(
        "alcohol-free beer",
        "Beverages",
        same=["cerveja sem álcool", "cerveza sin alcohol", "alkoholfreies bier",
              "birra analcolica", "piwo bezalkoholowe", "bière sans alcool"],
        antagonist=["cerveja tradicional com álcool", "cerveza con alcohol", "bier mit alkohol",
                    "birra alcolica", "piwo z alkoholem", "bière alcoolisée"],
    ),
    Produto(
        "gas grill",
        "Outdoor Cooking",
        same=["churrasqueira a gás", "parrilla a gas", "gasgrill",
              "barbecue a gas", "grill gazowy", "barbecue au gaz"],
        antagonist=["churrasqueira a carvão", "parrilla de carbón", "holzkohlegrill",
                    "barbecue a carbone", "grill węglowy", "barbecue au charbon"],
    ),
    Produto(
        "mechanical keyboard",
        "Computer Accessories",
        same=["teclado mecânico gamer", "teclado mecánico", "mechanische tastatur",
              "tastiera meccanica", "klawiatura mechaniczna", "clavier mécanique"],
        antagonist=["teclado de membrana", "teclado membrana silencioso", "membrantastatur",
                    "tastiera a membrana", "klawiatura membranowa", "clavier à membrane"],
    ),
    Produto(
        "sugar-free chocolate",
        "Confectionery",
        same=["chocolate sem açúcar", "chocolate sin azúcar", "zuckerfreie schokolade",
              "cioccolato senza zucchero", "czekolada bez cukru", "chocolat sans sucre"],
        antagonist=["chocolate com açúcar", "chocolate azucarado", "gezuckerte schokolade",
                    "cioccolato zuccherato", "słodzona czekolada", "chocolat sucré"],
    ),
    Produto(
        "curl cream",
        "Hair Care",
        same=["creme para cachos", "crema para rizos", "lockencreme",
              "crema per ricci", "krem do loków", "crème boucles"],
        antagonist=["creme alisante", "crema alisadora", "glättungscreme",
                    "crema lisciante", "krem prostujący", "crème lissante"],
    ),
    Produto(
        "inflatable kayak",
        "Water Sports",
        same=["caiaque inflável", "kayak hinchable", "aufblasbares kajak",
              "kayak gonfiabile", "kajak dmuchany", "kayak gonflable"],
        antagonist=["caiaque rígido de polietileno", "kayak rígido", "festrumpfkajak",
                    "kayak rigido", "kajak sztywny", "kayak rigide"],
    ),
    Produto(
        "electric scooter",
        "Personal Mobility",
        same=["patinete elétrico", "patinete eléctrico adulto", "e-scooter",
              "monopattino elettrico", "hulajnoga elektryczna", "trottinette électrique"],
        antagonist=["patinete de impulso sem motor", "patinete sin motor", "tretroller",
                    "monopattino a spinta", "hulajnoga klasyczna", "trottinette classique"],
    ),
    Produto(
        "non-stick frying pan",
        "Cookware",
        same=["frigideira antiaderente", "sartén antiadherente", "antihaftpfanne",
              "padella antiaderente", "patelnia nieprzywierająca", "poêle antiadhésive"],
        antagonist=["frigideira de ferro fundido", "sartén de hierro fundido", "gusseisenpfanne",
                    "padella in ghisa", "patelnia żeliwna", "poêle en fonte"],
    ),
]

# Idioma de cada posição nas listas acima — todas seguem a mesma ordem.
IDIOMAS = ["pt", "es", "de", "it", "pl", "fr"]
