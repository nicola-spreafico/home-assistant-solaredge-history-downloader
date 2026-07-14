# SolarEdge History Downloader

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/validate.yml/badge.svg)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/validate.yml)
[![GitHub Release](https://img.shields.io/github/v/release/nicola-spreafico/home-assistant-solaredge-history-downloader?include_prereleases)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/releases)
[![GitHub Last Commit](https://img.shields.io/github/last-commit/nicola-spreafico/home-assistant-solaredge-history-downloader)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/commits)
[![GitHub Issues](https://img.shields.io/github/issues/nicola-spreafico/home-assistant-solaredge-history-downloader)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/issues)
[![Buy Me a Pizza](https://img.shields.io/badge/Buy%20me%20a%20pizza-%F0%9F%8D%95-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/mf3ebnouct)

SolarEdge History Downloader retroactively fills or realigns an existing Home
Assistant `utility_meter` with production data from the SolarEdge Monitoring
API. It downloads and validates the complete replacement dataset first, then
replaces the target entity's raw states, short-term statistics, and long-term
statistics in one recorder transaction.

The integration does not create a sensor and does not store SolarEdge
credentials in its configuration. It exposes the action
`solaredge_history_downloader.update_history`.

## Safety

This integration deliberately rewrites recorder data. Before every run:

1. create and verify a full Home Assistant backup
2. test with a non-critical utility meter
3. confirm that the SolarEdge site and selected granularity are correct

Deletion starts only after the target, credentials, site, time range, units,
and complete in-memory download have passed validation. Raw states and
statistics are replaced in one transaction; a database failure rolls the
transaction back. The required confirmation value is exactly `REPLACE`.

## Requirements

- Home Assistant 2025.12.0 or newer
- Recorder enabled and the target entity included by its filter
- A loaded standard Home Assistant `utility_meter` sensor
- A SolarEdge Monitoring API key with access to the requested site
- Target unit `Wh`, `kWh`, or `MWh`

Tariff meters and custom cron reset schedules are rejected because SolarEdge
production points do not contain enough information to reconstruct those
periods safely.

## Installation

### HACS

1. Open HACS.
2. Add this repository as a custom repository with category **Integration**.
3. Install **SolarEdge History Downloader**.
4. Restart Home Assistant.

### Manual

Copy `custom_components/solaredge_history_downloader` into the Home Assistant
`custom_components` directory and restart Home Assistant.

### Enable the integration

Add the following top-level configuration and restart. The API key remains in
`secrets.yaml`; select the site explicitly on every action call:

```yaml
solaredge_history_downloader:
  api_key: !secret solaredge_api_key
```

You may instead provide `api_key` on an action call to override the YAML
default. `site_id` is always required on the action, so a single API key can
access multiple SolarEdge sites.

## Action

The action is available under **Developer tools > Actions** after restart.

```yaml
action: solaredge_history_downloader.update_history
data:
  site_id: 12345678
  target_entity: sensor.my_solaredge_utility_meter
  granularity: monthly
  confirm_replacement: REPLACE
```

The integration never logs or returns `api_key`, and does not persist it in its
own configuration. Home Assistant automation and script traces can retain
service-call data, so run this destructive maintenance action manually or
disable/delete its trace when the key must not remain in Home Assistant trace
storage.

### Parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `api_key` | No | SolarEdge Monitoring API key. Overrides the YAML default. |
| `site_id` | Yes | Numeric site ID that must be accessible by the API key. |
| `target_entity` | Yes | Existing standard `utility_meter` sensor to replace. |
| `granularity` | Yes | `hourly`, `daily`, `monthly`, `annual`, or `lifetime`. |
| `confirm_replacement` | Yes | Must be exactly `REPLACE`. |

### Granularity

| Value | Result | SolarEdge requests |
| --- | --- | --- |
| `hourly` | One point per available hour | Windows of at most 31 days |
| `daily` | One point per available day | Windows of at most 365 days |
| `monthly` | One point per calendar month | Complete site period |
| `annual` | One point per calendar year | Complete site period |
| `lifetime` | One point for the complete site period | Annual data aggregated to one point |

The requested data must be at least as fine as the target meter cycle. For
example, hourly data can rebuild a monthly meter and preserves every hourly
point, while annual data cannot truthfully rebuild a monthly meter and is
rejected. Hourly is the finest production-history resolution exposed by this
action. A quarter-hourly utility meter therefore cannot be reconstructed and
is rejected.

Offsets are supported only when source bucket boundaries can represent them
without splitting SolarEdge values. Coarse monthly, annual, and lifetime
buckets therefore require a zero meter offset.

## What Happens

1. Validate the target entity, recorder status, cycle, offset, and unit.
2. Validate the API key and site ID through SolarEdge site details.
3. Read the site's real data period and timezone.
4. Download all points, chunking requests where SolarEdge requires it.
5. Convert SolarEdge units to the target unit, sort, deduplicate, and validate.
6. Reconstruct `state` as the cumulative value inside each utility-meter cycle.
7. Reconstruct `sum` as a monotonic lifetime total for Energy Dashboard use.
8. Atomically replace raw states and both statistics tables.
9. Calibrate the live utility meter to the latest reconstructed cycle value.

Long-term statistics are timestamped at the last hour of each represented
interval. Existing short-term statistics are removed but are not fabricated
from hourly-or-coarser source data; Home Assistant resumes creating current
short-term statistics normally after the action.

## Response

When response data is requested, the action returns a summary such as:

```yaml
status: success
target_entity: sensor.solar_production_monthly
site_id: 123456
site_name: Home
site_timezone: Europe/Rome
granularity: daily
source_start: "2021-05-12"
source_end: "2025-12-31"
api_requests: 5
downloaded_points: 1695
deleted_states: 2500
deleted_short_term_statistics: 1800
deleted_long_term_statistics: 1600
imported_states: 1695
imported_long_term_statistics: 1695
calibrated_value: "284.42"
```

## API Limits and Failures

SolarEdge documents a limit of 300 requests per API key and site per day and a
maximum of three concurrent requests. This integration downloads sequentially.
HTTP authentication, access, not-found, and rate-limit errors are translated
into Home Assistant action errors before recorder data is touched.

If live calibration fails after a successful database replacement, Home
Assistant reports the calibration error. The historical transaction has
already completed in that case; rerun the standard `utility_meter.calibrate`
action with the latest reconstructed value or restore the backup.

## Development

```bash
python -m pip install "homeassistant==2025.12.0" "pycares<5" pytest pytest-asyncio
python -m pytest -q
```

Hassfest and HACS validation run in GitHub Actions. The recorder adapter uses
Home Assistant recorder internals because Home Assistant has no public API for
inserting historical raw state rows; the minimum supported Home Assistant
version is intentionally strict for that reason.