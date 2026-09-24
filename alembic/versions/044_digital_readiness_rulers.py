"""Digital Readiness: satura o chip, estreia a banda larga

2026-09-24. A dimensão era a média simples de dois indicadores, e um deles não
media o que o nome dizia.

O CASO QUE EXPÔS. Análise de café, 2026-09-23, Brasil contra Colômbia:

    dimensao                      peso    BRA    COL     efeito
    Demand Volume                0.386    6.9    1.1     BRA +2.24
    Demand Quality               0.193   48.7   43.0     BRA +1.10
    Macro Demand Structure       0.129   35.9   34.8     BRA +0.14
    Market Size                  0.055   80.7   66.6     BRA +0.78
    Digital Readiness            0.096   55.5   83.0     COL +2.64
    Demographic Sustainability   0.064   48.1   59.4     COL +0.72
    Economic Readiness           0.077   40.8   44.7     COL +0.30
    MOS                                  35.87  36.67    COL +0.80

O Brasil vence em quatro das sete, incluindo as duas de maior peso, e perde por
0,80 -- tudo decidido por Digital Readiness, que abria assim:

    BRA:  Internet Penetration 84.5  +  Mobile Connectivity 26.6  = 55.5
    COL:  Internet Penetration 79.3  +  Mobile Connectivity 86.7  = 83.0

POR QUE O CHIP NÃO MEDE CONECTIVIDADE. IT.CEL.SETS.P2 é assinaturas de celular
por 100 habitantes. Acima de uma linha por pessoa ele deixa de medir acesso e
passa a medir comportamento de chip: pré-pago, dual-SIM, linhas M2M. O Brasil
tem 101,93 -- uma linha por pessoa, mercado consolidado -- e pontuava 26,6 com o
texto "conectividade móvel fraca na maior parte da população". A Colômbia tem
174,09 e pontuava 86,7 com "excelente, mercado mobile-first".

Medido na base inteira (151 países com o indicador): mínimo 38,82, máximo
574,15. Fiji com 574 chips por 100 habitantes é a prova de que a escala superior
não significa nada -- nenhum país tem cinco vezes mais conectividade que o
melhor da Europa. Quarenta e dois países estão abaixo de 100.

O QUE MUDA NO CHIP. A régua vira 0..100 e SATURA em uma linha por pessoa. Abaixo
disso ela continua separando, que é onde a pergunta "todo mundo tem linha?" tem
resposta útil; acima, todos empatam em 100, porque todos estão igualmente
servidos. É o mesmo tratamento que a inflação recebeu em 2026-07-24 e pelo mesmo
motivo: quando a cauda é degenerada, o certo é saturar, não esticar a régua para
acomodá-la. A régua 70..190 da migration 030 fazia o oposto -- ajustada à
dispersão observada, empurrava para o fundo da escala justamente quem tem
exatamente uma linha por pessoa.

O valor BRUTO continua sendo gravado sem truncamento (ver
worldbank.compute_mobile_connectivity): quem satura é a régua, não a leitura.

O INDICADOR NOVO. broadband_adoption_score, IT.NET.BBND.P2, banda larga fixa por
100 habitantes. Estava no catálogo do marketmind-worldbank desde o início e
nunca tinha sido pedido por ninguém: zero observações até o backfill de hoje.
Mede infraestrutura de acesso instalada, que é o que faltava entre "usa
internet" e "tem chip", e não tem teto degenerado -- uma assinatura serve um
domicílio, então ninguém passa de 100.

Régua medida contra os 31 países da cesta, valor mais recente de cada:

    min  CF   0.02        max  FR  48.93        média 24.14
    BR  24.08     CO  17.22     US  38.86     DE  45.61

0..55, a dispersão observada arredondada para fora até um número legível --
mesmo método da migration 030. Congelada (_NO_SELF_WIDEN) para que a ordem em
que os países chegam no backfill não mude o resultado de quem já foi pontuado.

EFEITO COMBINADO, com os pesos por indicador de dimension_mapping.py
(internet 2.0, broadband 1.5, chip 1.0):

    Digital Readiness    antes            depois
    BRA                  55.5             74.4
    COL                  83.0             67.9

    MOS                  antes            depois
    BRA                  35.87            37.68
    COL                  36.67            35.22

O Brasil passa a vencer por 2,46. Não é o objetivo -- o objetivo é o indicador
medir o que o nome promete -- mas é a confirmação de que a inversão vinha daqui.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"

# indicator_code -> (new bounds, old bounds). broadband não tem "old": é novo,
# e o downgrade apaga a linha em vez de restaurar uma que nunca existiu.
_BOUNDS: dict[str, tuple[tuple[float, float], tuple[float, float] | None]] = {
    "mobile_connectivity_score": ((0.0, 100.0), (70.0, 190.0)),
    "broadband_adoption_score": ((0.0, 55.0), None),
}

# Chip: 151 países têm o indicador. Banda larga: os 31 da cesta, recém-coletados.
_POPULATION_SIZE = {"mobile_connectivity_score": 151, "broadband_adoption_score": 31}

_table = sa.table(
    "normalization_reference",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("indicator_code", sa.String),
    sa.column("period_key", sa.String),
    sa.column("min_x", sa.Numeric),
    sa.column("max_x", sa.Numeric),
    sa.column("reference_population_size", sa.Integer),
    sa.column("is_closed", sa.Boolean),
    sa.column("computed_at", sa.DateTime(timezone=True)),
)


def _seed(rows: list[tuple[str, tuple[float, float], int]]) -> None:
    codes = [code for code, _bounds, _size in rows]
    op.execute(_table.delete().where((_table.c.period_key == _FIXED) & (_table.c.indicator_code.in_(codes))))
    op.bulk_insert(
        _table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": code,
                "period_key": _FIXED,
                "min_x": bounds[0],
                "max_x": bounds[1],
                "reference_population_size": size,
                "is_closed": False,
                "computed_at": datetime.now(UTC),
            }
            for code, bounds, size in rows
        ],
    )


def upgrade() -> None:
    _seed([(code, new, _POPULATION_SIZE[code]) for code, (new, _old) in _BOUNDS.items()])


def downgrade() -> None:
    # A banda larga some inteira: sem régua, o indicador fica indisponível, que é
    # o estado anterior a esta migration. O chip volta para a régua da 030, com o
    # tamanho de população que ela declarava.
    op.execute(
        _table.delete().where(
            (_table.c.period_key == _FIXED) & (_table.c.indicator_code == "broadband_adoption_score")
        )
    )
    _seed([("mobile_connectivity_score", _BOUNDS["mobile_connectivity_score"][1], 24)])  # type: ignore[list-item]
