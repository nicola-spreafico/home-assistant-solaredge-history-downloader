<p align="center">
  <img src="custom_components/solaredge_history_downloader/brand/icon.png" alt="SolarEdge History Downloader" width="128">
</p>

# SolarEdge History Downloader

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/validate.yml/badge.svg)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/validate.yml)
[![HACS Validation](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/hacs.yml/badge.svg)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/actions/workflows/hacs.yml)
[![GitHub Release](https://img.shields.io/github/v/release/nicola-spreafico/home-assistant-solaredge-history-downloader?include_prereleases)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/releases)
[![GitHub Last Commit](https://img.shields.io/github/last-commit/nicola-spreafico/home-assistant-solaredge-history-downloader)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/commits)
[![GitHub Issues](https://img.shields.io/github/issues/nicola-spreafico/home-assistant-solaredge-history-downloader)](https://github.com/nicola-spreafico/home-assistant-solaredge-history-downloader/issues)
[![License: GPL-3.0](https://img.shields.io/github/license/nicola-spreafico/home-assistant-solaredge-history-downloader)](LICENSE)
[![Buy Me a Pizza](https://img.shields.io/badge/Buy%20me%20a%20pizza-%F0%9F%8D%95-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/mf3ebnouct)

## Why this integration exists

> *"Do you have a SolarEdge installation that has been producing energy for
> years, but you configured the Home Assistant `utility_meter` only recently?
> Your energy dashboard then starts from zero and has no historical
> production."*

> *"Does your utility meter contain gaps, incorrect values, or a broken history
> after a configuration change? Do you want to rebuild it from the production
> data that SolarEdge still has available?"*

SolarEdge History Downloader was created for these cases. It downloads
historical production data from the SolarEdge Monitoring API and can rebuild an
existing Home Assistant `utility_meter` from that data. It does not create a
new sensor and it does not replace the SolarEdge integration.

## Safety first

`update_history` permanently rewrites recorder data. Use it as a maintenance
tool, not as a routine automation.

Before every replacement:

1. Create and verify a full Home Assistant backup.
2. Test the process with a non-critical utility meter first.
3. Confirm the SolarEdge site, target entity, granularity, and expected period.
4. Run `inspect_history` before `update_history` when you are unsure what data
   SolarEdge can provide.

The integration validates the target, credentials, site, meter settings, and
complete downloaded dataset before deleting anything. Raw states and recorder
statistics are replaced in one database transaction. If that transaction
fails, it is rolled back. The destructive action also requires the exact
confirmation value `REPLACE`.

The integration does not log or return the API key. However, Home Assistant
automation and script traces can retain action data. For this reason, run
destructive maintenance actions manually, or disable and delete their traces
when an API key must not remain in trace storage.

## Requirements

- Home Assistant 2025.12.0 or newer.
- HACS, or a manual installation of the custom component.
- Recorder enabled. If the target sensor is excluded by the recorder filter,
  raw states are skipped and only long-term statistics are replaced.
- A target energy sensor (Wh, kWh, or MWh). Utility meters are recognized
  generically — the standard `utility_meter` integration and any custom
  integration whose sensor extends it (exposing the same cycle, tariff, and
  cron attributes) — and their history is reconstructed per reset cycle. Any
  other sensor is accepted with `target_type: sensor` and receives a
  cumulative lifetime total.
- A SolarEdge Monitoring API key with access to the requested site.
- The SolarEdge site ID used by the API.

The integration currently requires `solaredge==1.1.1`. Home Assistant installs
this Python dependency when it loads the integration.

### Python dependency compatibility

This integration uses `solaredge==1.1.1`, the latest version available when
this release was published. Home Assistant installs Python requirements in a
shared environment, so it cannot load integrations that require incompatible
versions of the same package. Before installing, check the requirements of any
other SolarEdge custom integration already in use. For example,
[SolarEdge Forecast](https://github.com/nelbs/solaredge-forecast/blob/main/custom_components/solaredge_forecast/manifest.json#L10)
requires `solaredge==0.0.4`; installing both integrations can therefore fail.

Tariff meters and custom cron reset schedules are not supported because their
period boundaries cannot be reconstructed reliably from SolarEdge production
points alone.

## Installation

### HACS

[![Install with HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nicola-spreafico&repository=home-assistant-solaredge-history-downloader&category=integration)

Use [Install SolarEdge History Downloader with HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=nicola-spreafico&repository=home-assistant-solaredge-history-downloader&category=integration), or:

1. Open HACS.
2. Add this repository as a custom repository.
3. Select **Integration** as the repository category.
4. Install **SolarEdge History Downloader**.
5. Restart Home Assistant.

### Manual installation

Copy `custom_components/solaredge_history_downloader` into the Home Assistant
`custom_components` directory, then restart Home Assistant.

### Configure the API key

Retrieve the key from the **SolarEdge Monitoring portal** under **Admin > API
Access**. You need SolarEdge account permissions to view or create an API key,
and the key must have access to the site identified by `site_id`. If your
SolarEdge account does not provide access to this data or section, ask your
SolarEdge installer to enable it or provide the API key.

First add the SolarEdge Monitoring API key to Home Assistant's `secrets.yaml`:

```yaml
solaredge_api_key: YOUR_SOLAREDGE_API_KEY
```

Then add this top-level configuration to `configuration.yaml` and restart:

```yaml
solaredge_history_downloader:
  api_key: !secret solaredge_api_key
```

The API key can also be supplied directly in an action call. An action-level
key overrides the YAML value. The `site_id` is always supplied per action, so
one API key can be used with multiple SolarEdge sites.

After the restart, the actions are available under **Developer tools > Actions**.

## How to use it

There are two actions. Start with Action 1 when you need to understand what
SolarEdge can provide; use Action 2 only after reviewing the result and
creating a backup.

## Action 1: inspect the available history

### What it does

`solaredge_history_downloader.inspect_history` downloads SolarEdge data from a
requested start date and reports the data period and points that were actually
returned. It is completely read-only: it does not change utility-meter
states, recorder history, or recorder statistics.

This is the recommended first step when you need to answer questions such as:

- When does SolarEdge's data period really begin for this site?
- Does SolarEdge have data for the date I want to restore?
- How many points will be downloaded at the selected granularity?

### How to use it

Open **Developer tools > Actions**, select
`solaredge_history_downloader.inspect_history`, fill in the fields, and run
the action. The equivalent YAML is:

```yaml
action: solaredge_history_downloader.inspect_history
data:
  site_id: 12345678
  start_date: "2021-01-01"
  granularity: monthly
```

### Parameters

| Parameter | Required | What it does |
| --- | --- | --- |
| `api_key` | No | Uses this API key instead of the key configured in YAML. |
| `site_id` | Yes | Identifies the SolarEdge site to inspect. It must be accessible by the API key. |
| `start_date` | Yes | Earliest date to request. SolarEdge may still return a later effective range if older data is unavailable. |
| `granularity` | Yes | Selects `hourly`, `daily`, `monthly`, `annual`, or `lifetime` aggregation. |

### Successful output

The action returns a response like this:

```yaml
status: success
site_id: 123456
site_name: Home
site_timezone: Europe/Rome
granularity: monthly
requested_start: "2021-01-01"
data_period_start: "2025-01-07"
data_period_end: "2026-07-14"
source_start: "2025-01-01"
source_end: "2026-07-14"
api_requests: 19
downloaded_points: 19
```

`data_period_start` and `data_period_end` describe the period reported by the
SolarEdge site metadata. `source_start` and `source_end` describe the points
that were actually returned after the requested date and aggregation were
applied.

## Action 2: rebuild utility-meter history

### What it does

`solaredge_history_downloader.update_history` downloads and validates the
SolarEdge production history, reconstructs the selected utility-meter cycle,
and permanently replaces the target entity's:

- raw state history;
- short-term statistics;
- long-term statistics.

After the database replacement, the integration calibrates the live utility
meter to the latest reconstructed cycle value. This lets the meter continue
from the repaired history instead of jumping back to an unrelated value.

### How to use it

1. Run `inspect_history` and review the returned period and point count.
2. Create and verify a full Home Assistant backup.
3. Open **Developer tools > Actions**.
4. Select `solaredge_history_downloader.update_history`.
5. Fill in the parameters, type `REPLACE` in the confirmation field, and run
   the action manually.

Equivalent YAML:

```yaml
action: solaredge_history_downloader.update_history
data:
  site_id: 12345678
  target_entity: sensor.my_solaredge_utility_meter
  target_type: meter
  granularity: monthly
  confirm_replacement: REPLACE
```

### Parameters

| Parameter | Required | What it does |
| --- | --- | --- |
| `api_key` | No | Uses this API key instead of the key configured in YAML. |
| `site_id` | Yes | Identifies the SolarEdge site used as the source of production data. |
| `target_entity` | Yes | Selects the energy sensor whose history will be replaced. |
| `target_type` | Yes | Declares the target as `meter` or `sensor`. A `meter` (from any integration) is reconstructed per reset cycle and calibrated afterwards; a `sensor` gets a cumulative lifetime total. The action is blocked when the declaration does not match the loaded entity. |
| `granularity` | Yes | Selects `hourly`, `daily`, `monthly`, `annual`, or `lifetime` source aggregation. |
| `confirm_replacement` | Yes | Must be exactly `REPLACE`; prevents accidental destructive calls. |

### Successful output

The action returns a response like this:

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

The counts describe the recorder rows removed and inserted by the replacement.
Short-term statistics are removed but are not fabricated from hourly-or-coarser
source data; Home Assistant resumes creating current short-term statistics
normally after the action.

## Supported granularity and meter rules

| Value | Result |
| --- | --- |
| `hourly` | One point per available hour |
| `daily` | One point per available day |
| `monthly` | One point per calendar month |
| `annual` | One point per calendar year |
| `lifetime` | One point for the complete site period |

The requested source must be at least as fine as the target meter cycle. For
example, hourly data can rebuild a monthly meter, while annual data cannot
truthfully rebuild a monthly meter and is rejected. Hourly is the finest
resolution exposed by this integration, so a quarter-hourly utility meter
cannot be reconstructed.

Offsets are supported only when source bucket boundaries can represent them
without splitting SolarEdge values. Monthly, annual, and lifetime operations
therefore require a zero meter offset.

## Possible errors

The integration validates inputs before touching recorder data. Common errors
include:

| Error | Meaning |
| --- | --- |
| `SolarEdge API key is required by the action or integration YAML` | No API key was supplied in the action or integration configuration. |
| `SolarEdge rejected the API key or the key cannot access the site` | The key is invalid, expired, or not authorized for the site. |
| `SolarEdge site ... does not exist or is not accessible` | The site ID is incorrect or unavailable to the key. |
| `SolarEdge API request limit reached; retry later` | The API returned HTTP 429. Wait before retrying. |
| `Unable to reach the SolarEdge API` | Home Assistant could not connect to SolarEdge. |
| `SolarEdge returned no production values...` | The selected site/date range returned no usable data. |
| `Home Assistant recorder is not ready` | Wait until Recorder has finished starting. |
| `Entity ... is excluded from recorder` | Add the target entity to the Recorder include filter. |
| `A history update is already running...` | Another replacement for the same meter is still running. |
| `Recorder history replacement failed; the database transaction was rolled back` | The database replacement failed; the transaction was rolled back. |
| `Granularity or offset validation errors` | The selected source resolution cannot reconstruct the target meter safely. |

If live calibration fails after a successful database replacement, the action
reports the calibration error while the historical transaction remains
completed. In that case, rerun the standard `utility_meter.calibrate` action
with the latest reconstructed value, or restore the backup.

## SolarEdge API limits

SolarEdge documents these limits for the Monitoring API:

- 300 requests per API key and site per day.
- A maximum of three concurrent requests.

This integration downloads sequentially and never intentionally exceeds three
concurrent requests. Long hourly or daily ranges may require multiple API
requests, so check `api_requests` in the inspection response before starting a
large replacement. The integration does not bypass SolarEdge's account or API
limits.

## How the replacement works

For `update_history`, the integration:

1. Validates the target utility meter, Recorder, cycle, offset, and unit.
2. Validates the API key and site through SolarEdge site details.
3. Reads the site's actual data period and timezone.
4. Downloads all requested points and chunks requests where required.
5. Converts units, sorts points, removes duplicates, and validates the data.
6. Reconstructs utility-meter states and monotonic long-term statistics.
7. Replaces raw states and recorder statistics atomically. Recorder-excluded
   targets get no raw states: stale states are deleted and only statistics
   rows are written.
8. Calibrates the live sensor to the latest reconstructed value when its
   integration provides a `calibrate` action; otherwise this step is skipped
   and `calibrated_value` is null in the response.

