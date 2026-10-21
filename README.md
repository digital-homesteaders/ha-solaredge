# SolarEdge Cloud (mySolarEdge) for Home Assistant

A Home Assistant custom integration for SolarEdge sites: solar production, the
grid meter and the site's power split, batteries, SolarEdge EV chargers and
home load controllers (smart relays). It logs in with your **mySolarEdge
e-mail and password**. You don't need an API key.

> **Status: v0.1, not yet tested against a real SolarEdge account.** It is a
> port of the [com.solaredge](https://github.com/gruijter/com.solaredge) v2.0.0
> rewrite for Homey by Robin de Gruijter. The tests run against that project's
> captured SolarEdge answers, on Home Assistant 2026.10. `hacs.json` asks for
> 2025.2 or newer, but versions before 2026.10 haven't been tried. See
> [What needs testing](#what-needs-testing).

Domain: `solaredge_cloud`. It can run next to Home Assistant's own
`solaredge` integration.

## How it differs from the core SolarEdge integration

| | Core `solaredge` | This one (`solaredge_cloud`) |
| --- | --- | --- |
| API | SolarEdge's public Monitoring API (`monitoringapi.solaredge.com`), with an API key from the installer or the portal. Optionally a web login, used only for per-module (optimizer) statistics. | The private API behind the mySolarEdge phone app (`monitoring.solaredge.com/services/m/...`), with your mySolarEdge login. |
| Live power | Every 15 minutes. The public API allows 300 requests a day per site. | Every 60 s by default (30–300 s). |
| Batteries | Yes | Yes, plus state of health and stored energy |
| EV chargers, load controllers | No | Yes, including on/off/auto control |
| Stability | Documented, supported API | Not documented. SolarEdge can change it without notice. |

If you have an API key and 15-minute data is enough, the core integration is
the safer choice. Use this one for faster live power, or for SolarEdge EV
chargers and home load controllers.

## How it works

Each config entry is one mySolarEdge account. It has one login and one poll
schedule, shared by every device on the account.

### Login

SolarEdge retired its old form login on 2026-10-21 (it answers 410 Gone). This
integration logs in the way the SolarEdge web portal does now, as com.solaredge
v2.1 does: the OAuth2 login with PKCE on `login.solaredge.com`. It gets an
access token, which goes with every request, and a refresh token. The access
token is renewed with the refresh token a minute before it expires, so your
password is only sent again when the refresh token stops working. The refresh
token is kept in Home Assistant's storage, so a restart needs no new login
either.

- **Wrong password:** polling stops and Home Assistant asks you to
  re-authenticate.
- **Refused login** (for example a second login step, such as a verification
  code, which isn't supported): the integration says so, and waits before
  trying again, from 1 minute up to 1 hour.
- **SolarEdge or the network failing during the login:** treated as an
  outage. A server error or rate limit during the login also makes it wait
  before the next login, because logging in again and again keeps a locked
  account locked.

- **Every poll (default 60 s):** 1 power flow request per site, for all its
  devices. Sites with EV chargers or load controllers also get 1 devices-list
  request.
- **Energy tick, every 5 minutes**, just after the inverters upload: the
  energy overview, inverter details (all inverters in one request), batteries,
  and the meter's totals for this year.
- **Every third energy tick (15 min):** site alerts and connectivity, the
  hottest optimizer of the day, and the EV charging history.
- **Once:** the meter's past years. They are cached in Home Assistant's
  storage.
- An inverter-only site skips the power flow on plain polls at night. It is
  read on the 5-minute energy tick until the sun is back.

A site with an inverter, meter and battery makes about **2,900 requests a
day**. The Homey app 1.8.1 sends about 86,000 for the same three devices.

The energy tick learns when your inverters upload (2.0.0 measured every 300 s
at a fixed second) and runs about 20 s after that. If it finds no new data, it
moves a little later.

### Changes from com.solaredge 2.0.0

These fixes come from a review of the Homey app (1.8.1 and the 2.0.0 rewrite):

1. **Poll interval:** live power every 60 s by default (30–300 s), not 10 s.
   Energy totals, inverter details and EV charging history are read only on
   the 5-minute energy tick or less often.
2. **Requests:** polls never overlap. The power flow is fetched once per site
   per poll. Identical requests running at the same time go out once. Every
   request times out after 20 s.
3. **Outages:** after an outage or an HTTP 429, polling pauses for twice the
   poll interval, doubling up to 30 minutes. Energy ticks are skipped while
   polling is paused. A wrong password is kept apart from a refused login and
   from a SolarEdge or network error. A wrong password stops polling and asks
   you to log in again (reauth), so the integration never retries it. On a 401
   it renews the token once and retries. An endpoint that still answers 401
   with a new token is left alone for an hour, so it can't cause a new login
   on every poll.
4. **Night:** the inverter never goes unavailable at night or while idle. It
   reports 0 W and its status becomes *Sleeping*. The site connectivity sensor
   stays *connected* while the inverters sleep. Entities go unavailable only
   after 3 failed polls in a row.
5. **Meter yearly totals:** the walk back through past years stops at a hard
   floor: the install year, or 10 years back if SolarEdge doesn't give one.
   "No data" ends the walk. Any other error aborts the whole run, and the next
   energy tick tries again. Finished years are cached. Only the current year
   is read again, on the energy tick. Last year is also read again until two
   days into January.
6. **Credentials:** the e-mail address and password are never written to the
   log. Diagnostics redact them, along with inverter serials and the vehicle
   name. (2.0.0 ships with `LOG_CREDENTIALS = true`.)
7. **EV charger:** no "discharged" counter. It was always 0.

Also:

- **Sessions:** each account has its own HTTP session without a shared cookie
  jar, so two accounts never mix logins.
- **Counters:** lifetime counters never fall. A short or missing answer is
  held rather than counted as a reset, so the Energy dashboard doesn't show
  spikes.
- **Devices:** the inverter is always added. (2.0.0 left it out when you
  paired it at night.) A device that was found once stays, even if SolarEdge
  reports it inactive at the next restart (a battery at night, for example).

## Install

### HACS (custom repository)

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/digital-homesteaders/ha-solaredge` with type **Integration**.
3. Find **SolarEdge Cloud (mySolarEdge)** in HACS, then **Download** it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration**, search for
   **SolarEdge Cloud**, and enter your mySolarEdge e-mail and password.

### Manually

1. Copy `custom_components/solaredge_cloud` into your Home Assistant
   `config/custom_components/` folder.
2. Restart Home Assistant and add the integration as in step 5 above.

To add several mySolarEdge accounts, add the integration once per account.
All sites of an account come in one entry.

### Options

**Settings → Devices & services → SolarEdge Cloud → Configure** sets the live
power poll interval: 30–300 s, default 60. The new interval applies without a
restart. The energy tick stays at 5 minutes.

If SolarEdge rejects the password later, for example after you change it,
polling stops and Home Assistant asks you to re-authenticate the integration
with the new password.

## Devices and entities

Each site is a device. Its inverter, meter, battery, EV chargers and load
controllers are devices linked to it. Power is in W and energy in kWh.

Entities marked *optional* only appear once SolarEdge reports a value for
them. For example, a single-phase inverter never gets L2/L3 voltage, and the
optimizer temperature only appears after sunrise.

### Site

| Entity | Source |
| --- | --- |
| Connectivity (binary sensor) | site communication status. Stays *connected* while the inverters sleep. |
| Problem (binary sensor), Open alerts | number of open site alerts |

### Inverter

The inverter device covers all inverters of the site. The power flow is
reported per site.

| Entity | Source |
| --- | --- |
| Power | power flow, solar production. 0 W when not producing. |
| Lifetime production | energy overview. `total_increasing`, for the Energy dashboard. |
| Production today / this month / this year | energy overview |
| Voltage L1–L3 *(optional; L2 and L3 disabled by default)* | phase voltages, averaged over the site's inverters |
| Status *(optional)* | producing, sleeping, starting, throttled, shutting down, fault, standby, off |
| Hottest optimizer today *(optional)* | highest optimizer temperature of the day |

### Meter

| Entity | Source |
| --- | --- |
| Grid power | import positive, export negative |
| Consumption, Solar power | the site's power split |
| Imported, Exported | lifetime grid import and export: the sum of every year since install. `total_increasing`, for the Energy dashboard. They appear once all past years are in. |
| Imported / Exported this year | this year |

### Battery

| Entity | Source |
| --- | --- |
| Power | charging positive, discharging negative |
| Battery | charge level % |
| State | charging, discharging, idle |
| Charged, Discharged | kWh into and out of the batteries, added up from the battery power at each poll. `total_increasing`, for the Energy dashboard. |
| Stored energy, State of health *(optional)* | stored kWh, summed over the batteries; full-pack capacity as % of the nameplate |

Checked against a real site with two 9.7 kWh SolarEdge Home Batteries: stored
energy matches SolarEdge's own total. SolarEdge's charge and discharge
counters in the same answer were 0 although the batteries cycle daily, so
Charged and Discharged are calculated from the power instead (2.0.0 read the
counters). With a 60 s poll they are within a few percent. Energy during an
outage longer than 10 minutes is not counted.

### EV charger

| Entity | Source |
| --- | --- |
| Charging (switch) | on: charge now; off: stop. Both put the charger in manual mode. The state shows whether it is charging. |
| Automatic mode (button) | back to the charger's own schedule and solar surplus charging |
| Power | the charger's own reading, else the site's power flow |
| Charging state | plugged in, charging, unplugged |
| Session energy, Session duration, Charge mode, Vehicle, Solar surplus charging *(optional)* | current or last session |
| Charged *(optional)* | sum of the last year's sessions, kept rising as old sessions drop out |

### Home load controller

| Entity | Source |
| --- | --- |
| Mode (select) | on, off, automatic. Unknown while SolarEdge holds a manual level other than 0 or 100. |
| Power *(optional)* | the device's meter, else rated power × level. The `estimated` attribute says which. |

## Energy dashboard

- **Solar production:** *Inverter → Lifetime production*.
- **Grid consumption / return to grid:** *Meter → Imported* / *Exported*.
- **Battery:** *Battery → Charged* / *Discharged*.

## Diagnostics

**Settings → Devices & services → SolarEdge Cloud → ⋮ → Download
diagnostics** gives you the polling state (interval, backoff, failures, energy
tick timing), the cached meter years and the current values. The e-mail
address, password, inverter serials and vehicle name are redacted.

## What needs testing

- **The OAuth login, against a real account.** It follows com.solaredge
  v2.1.1. The same flow is confirmed working in the Hubitat port on a real
  hub, but this Python version has only run against a fake login server.
- **EV charger and load controller fields with real hardware.** The mapping
  follows 2.0.0 and the real EV/load-controller answers in its tests. The
  battery fields are checked against one real two-battery site.
- **Whether the inverter's upload moment works for energy-tick timing on
  sites with several inverters.**

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

The tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
and these fixtures:

- `tests/fixtures/synthetic-site.json`: 2.0.0's made-up site with an
  inverter, meter, battery, EV charger and load controller.
- `tests/fixtures/ev-charger-devices.json`: real `getSiteDevices` answers of
  an EV charger in nine states and a KEBA charger with two load controllers.
  They come from briadelour/SolarEdgeEVCharger and solaredge-evcharger-ha#6,
  via 2.0.0.
- `tests/fixtures/inverter-live.json`: real inverter answers.
- `tests/fixtures/batteries-two.json`: a real battery answer from a site with
  two SolarEdge Home Batteries (serials and ids replaced).

| Test file | What it covers |
| --- | --- |
| `test_api.py` | the OAuth login against a fake login.solaredge.com (PKCE checked, RFC 7636 example), wrong password vs refused login vs outage, login hold-back, token renewal and refresh-token fallback, renewal on 401 / redirect / HTML, error codes, merged requests, no credentials or tokens in logs |
| `test_mapping.py` | every value against the fixtures, night readings, EV states, load controllers |
| `test_timing.py` | the energy tick timing and inverter sleep (port of 2.0.0's `test/timing.js`) |
| `test_meter_totals.py` | the yearly-totals loop: install-year and 10-year floor, "no data", aborting on errors, caching |
| `test_config_flow.py` | user, reauth and options flows |
| `test_init.py` | setup, requests per poll, poll interval, night, backoff, 429, reauth, commands, restart from the cache, diagnostics |

## Credits and license

Based on [com.solaredge](https://github.com/gruijter/com.solaredge) v2.0.0 for
Homey by Robin de Gruijter, which built on the original app by Emile Nijssen
(Athom). Like that project, this one is licensed under the **GNU General
Public License v3.0**; see [LICENSE](LICENSE).

It is not affiliated with or endorsed by SolarEdge.
