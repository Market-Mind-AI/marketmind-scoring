"""Tira do seletor os países que o coletor de demanda recusa de propósito.

2026-10-14. Uma análise com China entre sete países desistiu inteira depois de
três tentativas iguais: o coletor Google Ads (marketmind-pytrends,
`UNSUPPORTED_ISO2`) recusa CN — o Google é bloqueado na China continental e o
Google Ads não devolve volume representativo — enquanto `countries` aqui dizia
`analysis_ready = true`, porque a migration 043 mediu só o macro (World Bank,
UN Data) e a existência do país no catálogo de localizações da DataForSEO. A
China existe no catálogo; o que não existe é demanda medível. Rússia e Belarus
a 043 já tinha reprovado, mas ficam aqui pelo mesmo motivo, para a lista ser a
mesma nos dois serviços.

A regra do grafo é que nada parcial é entregue: um país pedido que não pontua
derruba os outros seis escolhidos na mesma consulta. Oferecer um país que a
fonte de demanda nunca vai atender é vender essa falha.

`MacroPrewarmService._promover` deixa de promover estes três, senão a próxima
passagem de pré-aquecimento os devolveria ao seletor assim que o macro deles
chegasse — ver `GOOGLE_ADS_UNAVAILABLE` em app/services/macro_coverage.py.

Revision ID: 054
Revises: 053
"""

from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None

# Igual a `UNSUPPORTED_ISO2` do coletor, em ISO3. Migration não importa código
# da aplicação, então a lista é repetida aqui de propósito.
_SEM_GOOGLE_ADS = ("CHN", "RUS", "BLR")


def upgrade() -> None:
    op.execute(
        "UPDATE countries SET analysis_ready = false WHERE iso3 IN ('CHN', 'RUS', 'BLR')"
    )


def downgrade() -> None:
    # Só a China tinha sido promovida; Rússia e Belarus nunca foram.
    op.execute("UPDATE countries SET analysis_ready = true WHERE iso3 = 'CHN'")
