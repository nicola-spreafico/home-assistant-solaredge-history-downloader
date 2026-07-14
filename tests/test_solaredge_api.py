"""Tests for the SolarEdge Monitoring API adapter."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from custom_components.solaredge_history_downloader.history import DownloadGranularity
from custom_components.solaredge_history_downloader.solaredge_api import (
    LegacyMonitoringClient,
    SolarEdgeDataError,
    SolarEdgeHistoryDownloader,
)


class FakeClient:
    """Minimal async SolarEdge client for downloader tests."""

    def __init__(self, _: str, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.energy_calls: list[tuple[datetime, datetime, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get_site_details(self, site_id: int) -> dict:
        return {
            "details": {
                "id": site_id,
                "name": "Home",
                "location": {"timeZone": "Europe/Rome"},
            }
        }

    async def get_site_data(self, site_ids: list[int]) -> dict:
        return {
            "dataPeriod": {
                "count": 1,
                "list": [
                    {
                        "id": site_ids[0],
                        "startDate": "2024-01-01",
                        "endDate": "2024-02-15",
                    }
                ],
            }
        }

    async def get_energy(
        self,
        site_ids: list[int],
        start_date: datetime,
        end_date: datetime,
        time_unit: str,
    ) -> dict:
        self.energy_calls.append((start_date, end_date, time_unit))
        return self.responses.pop(0)


class FakeLegacyClient:
    """Synchronous solaredge 0.0.4-compatible client."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls: list[tuple[str, tuple]] = []

    def get_details(self, site_id: int) -> dict:
        self.calls.append(("details", (site_id,)))
        return {"details": {"id": site_id}}

    def get_data_period(self, site_id: int) -> dict:
        self.calls.append(("data_period", (site_id,)))
        return {"dataPeriod": {"startDate": "2024-01-01", "endDate": "2024-01-31"}}

    def get_energy(
        self, site_id: int, start_date: str, end_date: str, time_unit: str
    ) -> dict:
        self.calls.append(("energy", (site_id, start_date, end_date, time_unit)))
        return {"energy": {"values": []}}


@pytest.mark.asyncio
async def test_legacy_client_adapts_sync_api_without_blocking_callers() -> None:
    legacy_client = LegacyMonitoringClient("key", client_factory=FakeLegacyClient)

    details = await legacy_client.get_site_details(123)
    period = await legacy_client.get_site_data([123])
    energy = await legacy_client.get_energy(
        [123], datetime(2024, 1, 1), datetime(2024, 1, 31), "MONTH"
    )

    assert details == {"details": {"id": 123}}
    assert period["dataPeriod"]["startDate"] == "2024-01-01"
    assert energy == {"energy": {"values": []}}
    assert legacy_client._client.calls == [
        ("details", (123,)),
        ("data_period", (123,)),
        ("energy", (123, "2024-01-01", "2024-01-31", "MONTH")),
    ]


@pytest.mark.asyncio
async def test_hourly_download_chunks_and_deduplicates_boundaries() -> None:
    responses = [
        {
            "energy": {
                "unit": "Wh",
                "values": [
                    {"date": "2024-01-01 00:00:00", "value": 100},
                    {"date": "2024-02-01 00:00:00", "value": 200},
                ],
            }
        },
        {
            "energy": {
                "unit": "Wh",
                "values": [
                    {"date": "2024-02-01 00:00:00", "value": 200},
                    {"date": "2024-02-15 00:00:00", "value": 300},
                ],
            }
        },
    ]
    client = FakeClient("key", responses)
    downloader = SolarEdgeHistoryDownloader("key", 123, client_factory=lambda _: client)

    result = await downloader.async_download(DownloadGranularity.HOURLY)

    assert result.requests == 2
    assert [point.watt_hours for point in result.intervals] == [
        Decimal("100"),
        Decimal("200"),
        Decimal("300"),
    ]
    assert [call[2] for call in client.energy_calls] == ["HOUR", "HOUR"]
    assert (client.energy_calls[0][1] - client.energy_calls[0][0]).days == 30


@pytest.mark.asyncio
async def test_monthly_download_converts_kwh_to_wh() -> None:
    client = FakeClient(
        "key",
        [
            {
                "energy": {
                    "unit": "kWh",
                    "values": [
                        {"date": "2024-01-01 00:00:00", "value": "1.25"},
                        {"date": "2024-02-01 00:00:00", "value": 2},
                    ],
                }
            }
        ],
    )
    downloader = SolarEdgeHistoryDownloader("key", 123, client_factory=lambda _: client)

    result = await downloader.async_download(DownloadGranularity.MONTHLY)

    assert [point.watt_hours for point in result.intervals] == [
        Decimal("1250.00"),
        Decimal("2000"),
    ]
    assert client.energy_calls[0][2] == "MONTH"


@pytest.mark.asyncio
async def test_explicit_start_date_overrides_data_period_start() -> None:
    client = FakeClient(
        "key",
        [
            {
                "energy": {
                    "unit": "Wh",
                    "values": [
                        {"date": "2020-01-01 00:00:00", "value": 100},
                        {"date": "2024-01-01 00:00:00", "value": 200},
                    ],
                }
            }
        ],
    )
    downloader = SolarEdgeHistoryDownloader("key", 123, client_factory=lambda _: client)

    result = await downloader.async_download(
        DownloadGranularity.MONTHLY, start_date=date(2020, 1, 1)
    )

    assert client.energy_calls[0][0].date() == date(2020, 1, 1)
    assert result.intervals[0].start.date() == date(2020, 1, 1)


@pytest.mark.asyncio
async def test_conflicting_duplicate_is_rejected_before_replacement() -> None:
    responses = [
        {
            "energy": {
                "unit": "Wh",
                "values": [{"date": "2024-02-01 00:00:00", "value": 200}],
            }
        },
        {
            "energy": {
                "unit": "Wh",
                "values": [{"date": "2024-02-01 00:00:00", "value": 201}],
            }
        },
    ]
    client = FakeClient("key", responses)
    downloader = SolarEdgeHistoryDownloader("key", 123, client_factory=lambda _: client)

    with pytest.raises(SolarEdgeDataError, match="conflicting"):
        await downloader.async_download(DownloadGranularity.HOURLY)
