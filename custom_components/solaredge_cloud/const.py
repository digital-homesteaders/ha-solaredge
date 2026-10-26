"""Constants for the SolarEdge Cloud integration.

Port of com.solaredge v2.0.0 for Homey by Robin de Gruijter (GPL-3.0).
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "solaredge_cloud"

CONF_POLL_INTERVAL: Final = "poll_interval"

# Live power (powerflow) poll, seconds. 2.0.0 defaults to 10 s; once a minute is
# plenty for data that SolarEdge itself refreshes every few seconds to minutes.
DEFAULT_POLL_INTERVAL: Final = 60
MIN_POLL_INTERVAL: Final = 30
MAX_POLL_INTERVAL: Final = 300

# After an outage or HTTP 429, polls pause for twice the poll interval, doubling
# per failed poll up to this (seconds).
BACKOFF_MAX: Final = 30 * 60

# Entities go unavailable only after this many failed polls in a row.
FAIL_LIMIT: Final = 3

# Every this many energy ticks (5 min each) the slow data is read too: alerts,
# connectivity, optimizer temperatures and the EV charging history.
SLOW_EVERY: Final = 3

# A devices list SolarEdge last updated longer ago than this counts as stale.
STALE_DATA_MS: Final = 10 * 60 * 1000

# Grid power within this many watts either way is idle: a battery holds the
# grid near 0 W, a few watts either side.
GRID_IDLE_W: Final = 100

# Meter yearly totals: without an install year look back at most this far.
YEARS_BACK: Final = 10
# SolarEdge was founded in 2006; no site has data before.
FIRST_YEAR: Final = 2006
# Pause between two uncached yearly requests (seconds), as 2.0.0 does.
YEAR_FETCH_DELAY: Final = 1.0

STORAGE_VERSION: Final = 1

# Device kinds, also the device key suffixes.
KIND_SITE: Final = "site"
KIND_INVERTER: Final = "inverter"
KIND_METER: Final = "meter"
KIND_BATTERY: Final = "battery"
KIND_EV: Final = "ev"
KIND_HLC: Final = "hlc"
