import uuid

import httpx
import pytest

from app.db.models.country import Country
from app.domain.enums import DataSource, IngestionRunStatus
from app.services.collection_client import (
    CollectionClient,
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

    def test_oecd_without_ref_area_is_not_covered(self) -> None:
        # Sem `oecd_ref_area` a OCDE não conhece o país. Cair para o ISO3 era
        # coletar um código que a própria cobertura dizia não existir.
        assert resolve_source_country_code(DataSource.OECD, _country()) is None
        assert resolve_source_country_code(DataSource.OECD, _country(oecd_ref_area="ESP")) == "ESP"

    def test_worldbank_is_read_by_iso2(self) -> None:
        assert resolve_source_country_code(DataSource.WORLDBANK, _country(), reading=True) == "ES"

    def test_undata_uses_iso2(self) -> None:
        assert resolve_source_country_code(DataSource.UNDATA, _country()) == "ES"
        assert resolve_source_country_code(DataSource.UNDATA, _country(undata_location_id=None)) is None

    def test_none_when_no_code_available(self) -> None:
        country = _country(iso2=None, pytrends_geo=None)
        assert resolve_source_country_code(DataSource.PYTRENDS, country) is None


class TestBuildRequest:
    def test_pytrends_search_volume_requires_keywords(self) -> None:
        # location + language present, but keywords missing -> raises.
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="search_volume",
            country=_country(),
            collection_params={"location": "ES", "language": "es"},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_pytrends_search_volume_payload(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="search_volume",
            country=_country(),
            collection_params={"location": "ES", "language": "es", "keywords": ["air fryer", "freidora de aire"]},
        )
        path, payload = _build_request(req)
        assert path == "/search-volume"
        assert payload == {"location": "ES", "language": "es", "keywords": ["air fryer", "freidora de aire"]}

    def test_pytrends_keywords_for_keywords_payload(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="keywords_for_keywords",
            country=_country(),
            collection_params={"location": "ES", "language": "es", "seed_keywords": ["air fryer"]},
        )
        path, payload = _build_request(req)
        assert path == "/keywords-for-keywords"
        assert payload == {"location": "ES", "language": "es", "seed_keywords": ["air fryer"]}

    def test_pytrends_keywords_for_keywords_includes_optional_limit(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="keywords_for_keywords",
            country=_country(),
            collection_params={"location": "ES", "language": "es", "seed_keywords": ["air fryer"], "limit": 50},
        )
        _path, payload = _build_request(req)
        assert payload["limit"] == 50

    def test_pytrends_keywords_for_keywords_requires_seed_keywords(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="keywords_for_keywords",
            country=_country(),
            collection_params={"location": "ES", "language": "es"},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

    def test_pytrends_search_volume_requires_location(self) -> None:
        req = CollectionRequest(
            source=DataSource.PYTRENDS,
            indicator_code="search_volume",
            country=_country(),
            collection_params={"language": "es", "keywords": ["air fryer"]},
        )
        with pytest.raises(CollectionDispatchError):
            _build_request(req)

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


class TestATransportFailureSaysWhatItWas:
    """httpx raises its timeouts with an EMPTY message, and an exception object
    is always truthy — so `{exc or type(exc).__name__}` silently keeps the
    empty message and the report ends at the colon. A ten-minute World Bank
    timeout on Colombia's CPI reached the caller as a blank reason."""

    async def test_freshness_names_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _timeout(*args: object, **kwargs: object) -> None:
            raise httpx.ReadTimeout("")

        monkeypatch.setattr(httpx.AsyncClient, "get", _timeout)

        with pytest.raises(CollectionDispatchError) as caught:
            await CollectionClient().freshness(
                DataSource.WORLDBANK, countries=["COL"], indicators=["FP.CPI.TOTL.ZG"], stale_after_days=30
            )

        assert "ReadTimeout" in str(caught.value)

    async def test_polling_names_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _timeout(*args: object, **kwargs: object) -> None:
            raise httpx.ReadTimeout("")

        monkeypatch.setattr(httpx.AsyncClient, "get", _timeout)

        with pytest.raises(CollectionDispatchError) as caught:
            await CollectionClient().poll_until_done(DataSource.WORLDBANK, uuid.uuid4())

        assert "ReadTimeout" in str(caught.value)
