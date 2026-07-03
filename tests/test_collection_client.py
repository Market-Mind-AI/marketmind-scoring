import uuid

import pytest

from app.db.models.country import Country
from app.domain.enums import DataSource, IngestionRunStatus
from app.services.collection_client import (
    CollectionDispatchError,
    CollectionRequest,
    _build_request,
    _extract_ingestion_run_id,
    _extract_run_status,
    resolve_source_country_code,
)


def _country(**overrides: object) -> Country:
    defaults: dict[str, object] = {
        "iso3": "ESP",
        "name": "Spain",
        "iso2": "ES",
        "un_numeric_code": "724",
        "oecd_ref_area": None,
        "worldbank_country_id": None,
        "undata_location_id": "724",
        "pytrends_geo": None,
    }
    defaults.update(overrides)
    return Country(**defaults)


class TestResolveSourceCountryCode:
    def test_pytrends_falls_back_to_iso2_when_geo_unset(self) -> None:
        assert resolve_source_country_code(DataSource.PYTRENDS, _country()) == "ES"

    def test_pytrends_prefers_explicit_geo(self) -> None:
        country = _country(pytrends_geo="ES-CT")
        assert resolve_source_country_code(DataSource.PYTRENDS, country) == "ES-CT"

    def test_worldbank_falls_back_to_iso3(self) -> None:
        assert resolve_source_country_code(DataSource.WORLDBANK, _country()) == "ESP"

    def test_oecd_falls_back_to_iso3(self) -> None:
        assert resolve_source_country_code(DataSource.OECD, _country()) == "ESP"

    def test_undata_uses_iso2(self) -> None:
        assert resolve_source_country_code(DataSource.UNDATA, _country()) == "ES"

    def test_raises_when_no_code_available(self) -> None:
        country = _country(iso2=None, pytrends_geo=None)
        with pytest.raises(CollectionDispatchError):
            resolve_source_country_code(DataSource.PYTRENDS, country)


class TestBuildRequest:
    def test_pytrends_interest_over_time_requires_keyword(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="interest_over_time",
            country=_country(),
            collection_params={},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_pytrends_interest_over_time_payload(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="interest_over_time",
            country=_country(),
            collection_params={"keyword": "air fryer", "timeframe": "today 12-m"},
        )
        path, payload = _build_request(req)
        assert path == "/interest-over-time"
        assert payload == {"keyword": "air fryer", "country": "ES", "timeframe": "today 12-m"}

    def test_pytrends_unknown_indicator_code(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS, indicator_code="not_a_real_endpoint", country=_country(), collection_params={}
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_worldbank_payload_batches_single_indicator(self) -> None:
        req = CollectionRequest(
            source=DataSource.WORLDBANK,
            indicator_code="NY.GDP.PCAP.CD",
            country=_country(),
            collection_params={"start_year": 2020, "end_year": 2025},
        )
        path, payload = _build_request(req)
        assert path == "/indicators/collect"
        assert payload == {
            "country": "ESP",
            "start_year": 2020,
            "end_year": 2025,
            "indicator_codes": ["NY.GDP.PCAP.CD"],
        }

    def test_worldbank_missing_start_year_raises(self) -> None:
        req = CollectionRequest(
            source=DataSource.WORLDBANK,
            indicator_code="NY.GDP.PCAP.CD",
            country=_country(),
            collection_params={"end_year": 2025},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_undata_requires_location_id_mapping(self) -> None:
        req = CollectionRequest(
            source=DataSource.UNDATA,
            indicator_code="TOTAL_POPULATION",
            country=_country(undata_location_id=None),
            collection_params={"start_year": 2020, "end_year": 2025},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_undata_payload(self) -> None:
        req = CollectionRequest(
            source=DataSource.UNDATA,
            indicator_code="TOTAL_POPULATION",
            country=_country(),
            collection_params={"start_year": 2020, "end_year": 2025},
        )
        path, payload = _build_request(req)
        assert path == "/indicators/collect"
        assert payload["location_id"] == 724
        assert payload["country"] == "ES"


class TestExtractIngestionRunId:
    def test_reads_metadata_ingestion_run_id(self) -> None:
        run_id = uuid.uuid4()
        body = {"metadata": {"ingestion_run_id": str(run_id)}}
        assert _extract_ingestion_run_id(DataSource.WORLDBANK, body) == run_id

    def test_raises_when_missing(self) -> None:
        with pytest.raises(CollectionDispatchError):
            _extract_ingestion_run_id(DataSource.WORLDBANK, {"metadata": {}})

    def test_undata_reads_data_ingestion_run_ids_list(self) -> None:
        """UNData's /indicators/collect never fills metadata.ingestion_run_id
        (it can create more than one run per call) — confirmed by reading
        app/services/undata_service.py::collect_indicators in that repo."""
        run_id = uuid.uuid4()
        body = {"metadata": {"ingestion_run_id": None}, "data": {"ingestion_run_ids": [str(run_id)]}}
        assert _extract_ingestion_run_id(DataSource.UNDATA, body) == run_id

    def test_undata_raises_when_ingestion_run_ids_empty(self) -> None:
        with pytest.raises(CollectionDispatchError):
            _extract_ingestion_run_id(DataSource.UNDATA, {"metadata": {}, "data": {"ingestion_run_ids": []}})


class TestExtractRunStatus:
    @pytest.mark.parametrize(
        "source,body,expected",
        [
            (DataSource.PYTRENDS, {"status": "running"}, IngestionRunStatus.RUNNING),
            (DataSource.PYTRENDS, {"status": "success"}, IngestionRunStatus.SUCCESS),
            (DataSource.PYTRENDS, {"status": "failed"}, IngestionRunStatus.FAILED),
            (DataSource.UNDATA, {"data": {"run": {"status": "pending"}}}, IngestionRunStatus.RUNNING),
            (DataSource.UNDATA, {"data": {"run": {"status": "partial"}}}, IngestionRunStatus.FAILED),
            (DataSource.UNDATA, {"data": {"run": {"status": "success"}}}, IngestionRunStatus.SUCCESS),
        ],
    )
    def test_status_normalization(
        self, source: DataSource, body: dict[str, object], expected: IngestionRunStatus
    ) -> None:
        assert _extract_run_status(source, body) == expected

    def test_unknown_status_raises(self) -> None:
        with pytest.raises(CollectionDispatchError):
            _extract_run_status(DataSource.PYTRENDS, {"status": "not_a_real_status"})
