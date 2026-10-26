"""Os textos das métricas em português e espanhol, ao lado dos ingleses.

2026-10-26. O relatório sai no idioma da análise (pt, en ou es), mas os
rótulos dos sinais e das dimensões, o glossário e a frase de cada score só
existiam em inglês: um relatório em espanhol trazia a narrativa em espanhol e
"Demand Volume - High (72/100)" ao lado. Os textos traduzidos ficam numa
coluna `translations` de cada tabela, {idioma: texto}, para continuarem
editáveis no banco como os ingleses — que seguem sendo os canônicos e o
último recurso de uma tradução ausente.

Lê de app/services/mos/explanation_seed_i18n.py, pelo mesmo motivo da 032 e da
056: uma versão só de cada texto.

Revision ID: 062
Revises: 061
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.mos.explanation_seed_i18n import BAND_PHRASES, LABELS, MEANINGS

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None

_explanations = sa.table(
    "metric_explanations",
    sa.column("metric_code", sa.String),
    sa.column("translations", postgresql.JSONB),
)
_bands = sa.table(
    "metric_band_phrases",
    sa.column("metric_code", sa.String),
    sa.column("band", sa.String),
    sa.column("translations", postgresql.JSONB),
)


def upgrade() -> None:
    op.add_column("metric_explanations", sa.Column("translations", postgresql.JSONB(), nullable=True))
    op.add_column("metric_band_phrases", sa.Column("translations", postgresql.JSONB(), nullable=True))
    idiomas = sorted(LABELS)
    for code in LABELS["pt"]:
        textos = {lang: {"label": LABELS[lang][code], "meaning": MEANINGS[lang][code]} for lang in idiomas}
        op.execute(_explanations.update().where(_explanations.c.metric_code == code).values(translations=textos))
    for code, faixas in BAND_PHRASES["pt"].items():
        for band in faixas:
            textos = {lang: BAND_PHRASES[lang][code][band] for lang in idiomas}
            op.execute(
                _bands.update()
                .where(_bands.c.metric_code == code, _bands.c.band == band)
                .values(translations=textos)
            )


def downgrade() -> None:
    op.drop_column("metric_band_phrases", "translations")
    op.drop_column("metric_explanations", "translations")
