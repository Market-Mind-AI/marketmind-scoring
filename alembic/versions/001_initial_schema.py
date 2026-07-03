"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-07-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_catalog",
        sa.Column("entity_key", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_title", sa.String(), nullable=False),
        sa.Column("google_mid", sa.String(), nullable=True),
        sa.Column("google_type", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("target_age_min", sa.Integer(), nullable=True),
        sa.Column("target_age_max", sa.Integer(), nullable=True),
        sa.Column(
            "merged_into", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_catalog_google_mid", "product_catalog", ["google_mid"])
    op.create_index("ix_product_catalog_merged_into", "product_catalog", ["merged_into"])
    op.create_index("ix_product_catalog_category", "product_catalog", ["category"])

    op.create_table(
        "countries",
        sa.Column("iso3", sa.String(length=3), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("iso2", sa.String(length=2), nullable=True),
        sa.Column("un_numeric_code", sa.String(), nullable=True),
        sa.Column("oecd_ref_area", sa.String(), nullable=True),
        sa.Column("worldbank_country_id", sa.String(), nullable=True),
        sa.Column("undata_location_id", sa.String(), nullable=True),
        sa.Column("pytrends_geo", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_countries_iso2", "countries", ["iso2"])
    op.create_index("ix_countries_worldbank_country_id", "countries", ["worldbank_country_id"])
    op.create_index("ix_countries_undata_location_id", "countries", ["undata_location_id"])

    op.create_table(
        "product_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("alias_normalized", sa.String(), nullable=False),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False
        ),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_aliases_alias_normalized", "product_aliases", ["alias_normalized"], unique=True)
    op.create_index("ix_product_aliases_entity_key", "product_aliases", ["entity_key"])

    op.create_table(
        "monthly_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_monthly_snapshots_lookup_with_entity",
        "monthly_snapshots",
        ["source", "entity_key", "country", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_monthly_snapshots_lookup_country_level",
        "monthly_snapshots",
        ["source", "country", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NULL"),
    )
    op.create_index("ix_monthly_snapshots_is_closed", "monthly_snapshots", ["is_closed"])

    op.create_table(
        "coverage_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("indicator_code", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_coverage_index_lookup_with_entity",
        "coverage_index",
        ["source", "entity_key", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_coverage_index_lookup_country_level",
        "coverage_index",
        ["source", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NULL"),
    )
    op.create_index("ix_coverage_index_is_closed", "coverage_index", ["is_closed"])

    op.create_table(
        "indicator_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("indicator_code", sa.String(), nullable=False),
        sa.Column("period_type", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("raw_value", sa.Numeric(), nullable=True),
        sa.Column("normalized_score", sa.Numeric(), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("calculation_window", sa.String(), nullable=True),
        sa.Column("availability_status", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_indicator_values_lookup_with_entity",
        "indicator_values",
        ["entity_key", "country", "indicator_code", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_indicator_values_lookup_country_level",
        "indicator_values",
        ["country", "indicator_code", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NULL"),
    )
    op.create_index("ix_indicator_values_country", "indicator_values", ["country"])
    op.create_index("ix_indicator_values_indicator_code", "indicator_values", ["indicator_code"])

    op.create_table(
        "normalization_reference",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("indicator_code", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("min_x", sa.Numeric(), nullable=False),
        sa.Column("max_x", sa.Numeric(), nullable=False),
        sa.Column("reference_population_size", sa.Integer(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_normalization_reference_lookup", "normalization_reference", ["indicator_code", "period_key"], unique=True
    )

    op.create_table(
        "indicator_weights",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("indicator_code", sa.String(), nullable=False),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("weight", sa.Numeric(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_indicator_weights_lookup", "indicator_weights", ["indicator_code", "period_key"], unique=True)
    op.create_index("ix_indicator_weights_dimension", "indicator_weights", ["dimension"])

    op.create_table(
        "mos_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("period_type", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("mos", sa.Numeric(), nullable=True),
        sa.Column("mos_before_cap", sa.Numeric(), nullable=True),
        sa.Column("capped_by", sa.String(), nullable=True),
        sa.Column("topsis_score", sa.Numeric(), nullable=True),
        sa.Column("availability_status", sa.String(), nullable=False),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mos_results_lookup", "mos_results", ["entity_key", "country", "period_type", "period_key"], unique=True
    )
    op.create_index("ix_mos_results_country", "mos_results", ["country"])

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_job_tasks_job_id", "job_tasks", ["job_id"])
    op.create_index("ix_job_tasks_status", "job_tasks", ["status"])


def downgrade() -> None:
    op.drop_table("job_tasks")
    op.drop_table("jobs")
    op.drop_table("mos_results")
    op.drop_table("indicator_weights")
    op.drop_table("normalization_reference")
    op.drop_table("indicator_values")
    op.drop_table("coverage_index")
    op.drop_table("monthly_snapshots")
    op.drop_table("product_aliases")
    op.drop_table("countries")
    op.drop_table("product_catalog")
