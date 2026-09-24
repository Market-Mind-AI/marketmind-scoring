"""Funde as entidades de marca no produto, levando o vocabulário para a marca.

2026-09-24, junto com a 045. Ali o desenho mudou; aqui os dados existentes
passam a caber nele.

Para cada entidade que tem `brand_key` e uma família genérica:

  1. o `localized_markets` dela — que é vocabulário DA MARCA — vai para
     `brand_markets`, chaveado por (família, marca, país);
  2. os aliases dela passam a apontar para a família, que é agora o único
     produto no catálogo;
  3. ela fica marcada com `merged_into` apontando para a família, em vez de
     ser apagada.

NÃO É APAGADA de propósito. `analysis_requests.entity_key` referencia entidades,
e análises já entregues apontam para a entidade da marca; apagá-la quebraria o
histórico que o comprador ainda abre. `merged_into` já existia exatamente para
dizer "fui para lá" sem sumir.

Aliases duplicados são possíveis: `juan valdez coffee` podia estar gravado na
entidade da marca e na genérica ao mesmo tempo — foi o que o resolvedor
invertido produziu em 24/09/2026. `alias_normalized` é único global, então o
repontamento ignora o que já existir no destino e apaga o órfão.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    marcas = conn.execute(
        sa.text(
            "SELECT entity_key, generic_entity_key, brand_key, localized_markets "
            "FROM product_catalog "
            "WHERE brand_key IS NOT NULL AND generic_entity_key IS NOT NULL AND merged_into IS NULL"
        )
    ).fetchall()

    for entity_key, familia, brand_key, mercados in marcas:
        for country, market in (mercados or {}).items():
            if not market:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO brand_markets (entity_key, brand_key, country, market) "
                    "VALUES (:familia, :marca, :pais, CAST(:market AS jsonb)) "
                    "ON CONFLICT (entity_key, brand_key, country) DO NOTHING"
                ),
                {
                    "familia": familia,
                    "marca": brand_key,
                    "pais": country,
                    "market": json.dumps(market, ensure_ascii=False),
                },
            )
        # O alias que já existir no destino vence; o desta entidade some, senão
        # a unicidade global de `alias_normalized` impede o repontamento.
        conn.execute(
            sa.text(
                "DELETE FROM product_aliases a WHERE a.entity_key = :marca AND EXISTS ("
                "  SELECT 1 FROM product_aliases b WHERE b.entity_key = :familia"
                "  AND b.alias_normalized = a.alias_normalized)"
            ),
            {"marca": entity_key, "familia": familia},
        )
        conn.execute(
            sa.text("UPDATE product_aliases SET entity_key = :familia WHERE entity_key = :marca"),
            {"marca": entity_key, "familia": familia},
        )
        conn.execute(
            sa.text("UPDATE product_catalog SET merged_into = :familia WHERE entity_key = :marca"),
            {"marca": entity_key, "familia": familia},
        )


def downgrade() -> None:
    # Os aliases repontados não voltam: qual deles era de quem não é
    # recuperável depois da fusão. O que volta é a marcação, para que as
    # entidades deixem de se declarar fundidas.
    op.execute("UPDATE product_catalog SET merged_into = NULL WHERE brand_key IS NOT NULL")
    op.execute("DELETE FROM brand_markets")
