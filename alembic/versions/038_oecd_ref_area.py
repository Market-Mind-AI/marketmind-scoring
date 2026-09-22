"""Preenche o código OECD de cada país, e deixa nulo quem a fonte não cobre.

2026-09-22. A coluna `oecd_ref_area` existe desde o começo e estava NULA para os
24 países — nunca foi preenchida. Como cada coluna de identificação em
`countries` é o código do país NAQUELA fonte, a ausência dela é a resposta à
pergunta "esta fonte conhece este país?".

Sem isso, o pré-aquecimento pedia o índice de preços imobiliários (RHP) da OCDE
para Argentina e Peru todas as noites, e todas as noites recebia "não tenho".
Duas tarefas falhando para sempre — e uma falha que nunca vai deixar de
acontecer ensina quem lê o log a ignorar falhas, que é o oposto do que um
registro de falha serve.

A lista não foi inferida de quem é membro da OCDE, que não é a mesma coisa: a
OCDE publica RHP para vários não-membros. Foi medida no armazém do próprio
serviço em 2026-09-22 — `select country, count(*) from oecd_observations where
indicator_code='RHP' group by country` devolveu 22 dos 24 países suportados,
faltando exatamente ARG e PER.

Não confundir com "o indicador vale zero". Argentina não tem crescimento
imobiliário zero; ela não tem o dado, e continua marcada como indisponível pelo
motor de indicadores. O que vai a zero é a contagem de FALHAS de coleta, porque
deixa de haver uma coleta impossível para falhar.

REF_AREA da OCDE é o ISO3 para todos os países desta lista, então o código é o
próprio iso3. O downgrade devolve a coluna a nulo, que é o estado anterior.
"""

from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None

# Medido, não inferido. Ver o docstring.
COBERTOS_POR_RHP = (
    "AUS", "BRA", "CAN", "CHL", "COL", "DEU", "ESP", "FRA", "GBR", "IDN",
    "IND", "ITA", "JPN", "MEX", "NLD", "POL", "PRT", "SAU", "SWE", "TUR",
    "USA", "ZAF",
)


def upgrade() -> None:
    lista = ", ".join(f"'{iso3}'" for iso3 in COBERTOS_POR_RHP)
    op.execute(f"UPDATE countries SET oecd_ref_area = iso3 WHERE iso3 IN ({lista})")
    # Explícito, e não só "o que sobrou": se um país for acrescentado à tabela
    # depois desta migration, ele nasce nulo e é tratado como não coberto até
    # que alguém verifique — o lado seguro do erro.
    op.execute(f"UPDATE countries SET oecd_ref_area = NULL WHERE iso3 NOT IN ({lista})")


def downgrade() -> None:
    op.execute("UPDATE countries SET oecd_ref_area = NULL")
