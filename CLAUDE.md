# jarvis-device-homekit

Generic **Apple HomeKit (HAP)** device protocol — local LAN discovery, pairing,
and control. `IJarvisDeviceProtocol`, routed through the node's built-in
`control_device` command (no own voice command). Built primarily for the ecobee
thermostat (its cloud dev API is closed to new developers) but covers any HomeKit
accessory.

## Layout

```
device_families/homekit/protocol.py   # HomekitProtocol(IJarvisDeviceProtocol)
homekit_shared/characteristics.py     # pure HAP id maps + °C/°F + accessory-DB parsing (NO aiohomekit)
homekit_shared/homekit_client.py      # the ONLY aiohomekit usage (lazy-imported)
tests/test_protocol.py                # importlib load + FakeHomekitClient + in-memory backend
```

`homekit_shared/` (not `shared`/`lib`/`client` — those are blocked names that fail
`jdt test` static analysis and shadow node built-ins).

## Commands

```bash
JDT=../jarvis-developer-toolkit/.venv/bin/jdt
$JDT validate .                 # fast manifest lint
$JDT test .                     # 3-phase Pantry gate (manifest + AST + import) — passes WITHOUT aiohomekit
$JDT test . --install-deps      # also pip-installs aiohomekit then import-checks
$JDT deploy local .             # install onto the local node (or: deploy ssh pi@<host> .)
../jarvis-developer-toolkit/.venv/bin/python -m pytest tests/ -q   # behavioral tests (no aiohomekit needed)
```

## Load-bearing facts (learned from the node runtime — don't regress)

1. **`control()` is device-first**, NOT the SDK's stale `ip`-first abstract.
   The node calls `adapter.control(DiscoveredDevice, action, data)` (see
   `jarvis-node-setup/services/direct_device_service.py:206`). Signature here:
   `control(self, device, action, data=None, **kwargs)`.
2. **`get_state()` is called keyword-only**: `get_state(ip=..., device=..., mac_address=...)`.
   Signature: `get_state(self, ip="", **kwargs)`; pull `device` from kwargs.
3. **The node reconstructs `DiscoveredDevice` WITHOUT `extra`** before control
   (only entity_id/name/domain/protocol/cloud_id/local_ip/mac_address survive). So:
   - the stable HAP accessory id is stashed in **`cloud_id`** (used to re-pair/connect),
   - pairing data is persisted in `JarvisStorage("homekit_device")` keyed by
     **`pairing:{entity_id}`** (entity_id survives). Mirrors `jarvis-device-apple`.
4. **Pairing UX** uses `InputRequest` (type `text`, `follow_up_action="pair_finish"`),
   same primitive as `jarvis-device-apple`. The mobile `DeviceEditScreen` already
   renders the dialog and sends `{session_id, pin}` back.
5. **Mobile Pair affordance:** `get_state` returns `{"needs_pairing": True}` when the
   accessory has no stored pairing. `jarvis-node-mobile`'s `DeviceControlPanel`
   keys off that to show a "Pair" button (the per-domain panels — e.g. ClimateControl
   — otherwise can't trigger `pair_start`).
6. **aiohomekit seam:** all aiohomekit calls live in `homekit_shared/homekit_client.py`
   and are **lazy-imported inside methods** so `protocol.py` imports/instantiates
   without the native stack (this is what makes `jdt test` pass in the Pantry
   container). Verified against **aiohomekit 3.2.20**: `Controller(async_zeroconf_instance, char_cache)`,
   `async_start/stop`, `async_discover` (asyncgen), `async_find`, `load_pairing`,
   `controller.aliases`/`.pairings`; `discovery.async_start_pairing(alias) -> finish(code)`;
   `IpPairing.pairing_data`; `pairing.list_accessories_and_characteristics()/get_characteristics()/put_characteristics()`.
   Tests inject a fake client, so aiohomekit API drift only touches this one file —
   pin the version in `jarvis_package.yaml`.
   - **GOTCHA (verified on the Pi, 3.2.20):** aiohomekit ATTACHES to existing
     `_hap._tcp`/`_hap._udp` `AsyncServiceBrowser`s — it does NOT create them (HA
     supplies a shared one). You MUST start both browsers on the AsyncZeroconf
     BEFORE `controller.async_start()`, or the IP/CoAP transports raise
     "no zeroconf browser for _hap._tcp.local." (`_udp` is needed because the CoAP
     transport also registers). And `async_discover()` does NOT wait — it drains the
     browser cache, so sleep ~timeout after a fresh start to let mDNS populate. Both
     handled in `HomekitClient`. Confirmed live: discover() found a real Hue bridge
     on the LAN.

## Gotchas

- **One controller per accessory (HAP).** If it's already in Apple Home, pairing
  fails — remove it there first. Discovery exposes `extra.paired_elsewhere`.
- **Temps are Celsius on the wire**; `characteristics.to_celsius/from_celsius`
  convert against the `HOMEKIT_TEMP_UNIT` secret (default F). Setpoints are
  clamped/rounded to the characteristic's `minValue/maxValue/minStep`.
- **Pi arch:** aiohomekit pulls `cryptography`/`orjson`/`bleak` (native/Rust).
  Wheels exist for aarch64 (Pi Zero 2 W / Pi 4); verify before prod on the node arch.
  Graceful ImportError keeps the node booting if install fails.

## Loop (agents)

**Apple HomeKit (Local)**: Control Apple HomeKit (HAP) accessories locally over your LAN: thermostats, switches, outlets, lights and locks, with read-only sensors. A Jarvis package (device_protocol) that nodes install through Pantry.

**Verify** (what CI runs: `.github/workflows/ci.yml` calls the toolkit's reusable `package-ci.yml`):

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install "jarvis-command-sdk @ git+https://github.com/alexberardi/jarvis-command-sdk@main" \
  "jarvis-developer-toolkit @ git+https://github.com/alexberardi/jarvis-developer-toolkit@main" pytest
jdt test . --install-deps
python -m pytest tests
```

**Merging.** An agent may squash-merge **its own** PR into `main` once CI has passed, then check main's CI after the merge. **Hold for the user's review** instead, with a note saying why, when the PR:

- changes `jarvis_package.yaml` at all (its `version`, `secrets`, `packages`, `apt_packages` and `post_install` are what reach users' nodes);
- touches `*_scripts/**` or `*_shared/**` (OAuth and token code lives there);
- touches `.github/**`;
- deletes, renames or weakens a test;
- adds a secret (`JarvisSecret`) or a new outbound host, or is marked "needs review" by its author.

**Releases are the user's.** Tagging `v<version>` and submitting to Pantry are always done by the user: never push a tag or submit. Never merge someone else's PR, never force-push to `main`.
