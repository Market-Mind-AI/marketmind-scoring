"""Apaga só as linhas escritas na janela em que marca e produto dividiram linha.

2026-09-24. Entre a migration 046 (que fundiu as entidades de marca no produto)
e a 048 (que separou as linhas por marca), as duas análises escreveram no mesmo
lugar: `indicator_values` e `mos_results` eram únicos por (entidade, país,
período), sem marca. Quem escrevesse por último vencia.

O sintoma enganava e por isso vale registrar: os MOS saíam DIFERENTES — 38,44 e
37,82 no Brasil — e pareciam prova de que a separação funcionava. Não era. Cada
execução lia a linha que a outra tinha acabado de escrever, e a diferença era
ordem de execução. `trend_score` valia 3.557.340 nas quatro análises, com e sem
marca.

O ALVO É ESTREITO DE PROPÓSITO. Antes da 046 não havia colisão: a marca tinha
entidade própria, com `entity_key` diferente. Depois da 048 também não. Então
só é suspeito o que foi calculado NESSA JANELA e para uma entidade que recebeu
uma fusão — nenhum outro produto foi tocado.

Apagar e não corrigir porque não há o que corrigir: a linha não guarda de quem
era o número. O que existe é a resposta certa, e ela é recalculada sozinha na
próxima análise daquele produto. Os relatórios já entregues não dependem disso
— eles têm `analysis_requests.result` e `breakdown` gravados.
"""

import sqlalchemy as sa
from alembic import op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None

# A janela em que as duas gravaram no mesmo lugar, em UTC. Começa quando a 046
# rodou em dev e termina quando a 048 rodou.
_DE = "2026-09-24 12:40:00"
_ATE = "2026-09-24 14:10:00"


def upgrade() -> None:
    conn = op.get_bind()
    # Só entidades que RECEBERAM uma fusão: são as únicas onde duas análises
    # podiam disputar a mesma linha.
    alvos = sa.text(
        "SELECT DISTINCT merged_into FROM product_catalog "
        "WHERE merged_into IS NOT NULL AND brand_key IS NOT NULL"
    )
    entidades = [linha[0] for linha in conn.execute(alvos)]
    if not entidades:
        return

    for tabela in ("indicator_values", "mos_results"):
        resultado = conn.execute(
            sa.text(
                f"DELETE FROM {tabela} "  # noqa: S608 — nome de tabela é literal desta lista
                "WHERE entity_key = ANY(:entidades) AND brand_key IS NULL "
                "AND computed_at >= :de AND computed_at < :ate"
            ),
            {"entidades": entidades, "de": _DE, "ate": _ATE},
        )
        print(f"{tabela}: {resultado.rowcount} linhas suspeitas removidas")


def downgrade() -> None:
    # Não há volta: o que foi apagado não guardava de quem era. O caminho de
    # volta é recalcular, que é o que a próxima análise faz sozinha.
    pass
