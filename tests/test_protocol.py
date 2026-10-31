"""Behavioral tests for the HomeKit device protocol.

Mirrors the jarvis-device-hue test scaffold: load ``protocol.py`` via importlib
(repo root on sys.path so ``homekit_shared`` resolves), inject a fake
``HomekitClient`` and an in-memory storage backend, and drive the async API with
``asyncio.run``. No real HAP stack (aiohomekit) is needed.

    <node venv with jarvis_command_sdk>/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # so `homekit_shared` resolves

from jarvis_command_sdk import DiscoveredDevice, StorageBackend, set_backend  # noqa: E402

_PROTOCOL_PATH = _REPO_ROOT / "device_families" / "homekit" / "protocol.py"


def _load_protocol() -> Any:
    spec = importlib.util.spec_from_file_location("homekit_protocol_under_test", _PROTOCOL_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hk_mod = _load_protocol()
HomekitProtocol = hk_mod.HomekitProtocol


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _uuid(short: str) -> str:
    """Build a full HAP characteristic/service UUID from its short hex id."""
    return f"{int(short, 16):08X}-0000-1000-8000-0026BB765291"


def _char(short: str, iid: int, value: Any, **extra: Any) -> dict[str, Any]:
    return {"iid": iid, "type": _uuid(short), "value": value, **extra}


# Thermostat accessory DB (full UUIDs, current values in °C).
THERMOSTAT_ACC = [
    {
        "aid": 1,
        "services": [
            {
                "iid": 8,
                "type": _uuid("4A"),  # Thermostat
                "characteristics": [
                    _char("11", 9, 21.0),   # CurrentTemperature
                    _char("35", 10, 20.0, minValue=10, maxValue=35, minStep=0.5),  # TargetTemperature
                    _char("33", 11, 1),     # TargetHeatingCoolingState (heat)
                    _char("F", 12, 1),      # CurrentHeatingCoolingState (heating)
                    _char("10", 13, 44),    # CurrentRelativeHumidity
                ],
            }
        ],
    }
]

SWITCH_ACC = [
    {"aid": 1, "services": [
        {"iid": 7, "type": _uuid("49"), "characteristics": [_char("25", 5, False)]}  # Switch / On
    ]}
]

LOCK_ACC = [
    {"aid": 1, "services": [
        {"iid": 6, "type": _uuid("45"), "characteristics": [  # LockMechanism
            _char("1D", 6, 1),   # LockCurrentState (secured)
            _char("1E", 7, 1),   # LockTargetState
        ]}
    ]}
]


class FakeHomekitClient:
    def __init__(self, discover_result: list[dict] | None = None, accessories: list | None = None,
                 pair_error: Exception | None = None) -> None:
        self._discover = discover_result or []
        self._pair_error = pair_error
        self._accessories = accessories or []
        self.writes: list[tuple[int, int, Any]] = []
        self.paired: dict | None = None
        self.removed: dict | None = None

    async def discover(self, timeout: float = 5.0) -> list[dict]:
        return self._discover

    async def remove_pairing(self, pairing_data: dict) -> None:
        self.removed = pairing_data

    async def pair(self, hap_id: str, alias: str, code: str) -> dict:
        # Single-shot pairing (the whole handshake in one call).
        if self._pair_error is not None:
            raise self._pair_error
        self.paired = {"hap": hap_id, "alias": alias, "code": code}
        return {"AccessoryPairingID": "AA:BB:CC", "secret": "ltsk"}

    async def get_accessories(self, pairing_data: dict) -> list:
        return self._accessories

    async def put_characteristics(self, pairing_data: dict, writes: list) -> list:
        self.writes.extend(writes)
        return []


class FakeBackend(StorageBackend):
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.secrets: dict[str, str] = {}

    def save(self, command_name, data_key, data, expires_at=None):
        self.data[(command_name, data_key)] = data

    def get(self, command_name, data_key):
        return self.data.get((command_name, data_key))

    def get_all(self, command_name):
        return [v for (c, _k), v in self.data.items() if c == command_name]

    def delete(self, command_name, data_key):
        return self.data.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name):
        keys = [k for k in self.data if k[0] == command_name]
        for k in keys:
            self.data.pop(k)
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get(key)

    def set_secret(self, key, value, scope, value_type="string", user_id=None):
        self.secrets[key] = value

    def delete_secret(self, key, scope, user_id=None):
        self.secrets.pop(key, None)


@pytest.fixture()
def backend() -> FakeBackend:
    be = FakeBackend()
    set_backend(be)
    return be


def _device(domain: str = "climate", entity_id: str = "bedroom", hap: str = "AA:BB:CC") -> DiscoveredDevice:
    return DiscoveredDevice(
        name="Bedroom", domain=domain, manufacturer="HomeKit",
        model="ecobee", protocol="homekit", entity_id=entity_id, cloud_id=hap,
    )


def _seed_pairing(backend: FakeBackend, entity_id: str = "bedroom") -> None:
    backend.save("homekit_device", f"pairing:{entity_id}", {"AccessoryPairingID": "AA:BB:CC"})


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_discover_maps_category_to_domain_and_stashes_hap_id(backend: FakeBackend) -> None:
    client = FakeHomekitClient(discover_result=[
        {"hap_id": "AA:BB:CC", "name": "Hallway", "model": "ecobee", "category": 9, "paired": False},
        {"hap_id": "DD:EE:FF", "name": "Lamp", "model": "Bulb", "category": 5, "paired": False},
        # Apple's HomeKit Accessory Simulator advertises a thermostat as category 1
        # (Other) — domain must fall back to the name/model keyword guess.
        {"hap_id": "11:22:33", "name": "Thermostat", "model": "Thermostat", "category": 1, "paired": False},
    ])
    devices = asyncio.run(HomekitProtocol(client=client).discover())
    by_name = {d.name: d for d in devices}
    assert by_name["Hallway"].domain == "climate"
    assert by_name["Hallway"].cloud_id == "AA:BB:CC"  # survives node record reconstruction
    assert by_name["Lamp"].domain == "light"
    assert by_name["Thermostat"].domain == "climate"  # category-1 → name fallback


def test_domain_from_name_keyword_fallback() -> None:
    from homekit_shared.characteristics import domain_from_name

    assert domain_from_name("Sim Thermostat") == "climate"
    assert domain_from_name("Front Door Lock") == "lock"
    assert domain_from_name("Desk Lamp") == "light"
    assert domain_from_name("Mystery Gadget") is None


def test_control_redetects_domain_when_stored_domain_wrong(backend: FakeBackend) -> None:
    # Device imported as "switch" (mis-categorized at discovery) but the paired
    # accessory is actually a thermostat — control must re-detect climate from the
    # accessory's services so set_temperature still works.
    _seed_pairing(backend, "bedroom")
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    result = asyncio.run(
        HomekitProtocol(client=client).control(
            _device("switch", "bedroom"), "set_temperature", {"temperature": 72}
        )
    )
    assert result.success
    assert client.writes == [(1, 10, 22.0)]


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #
def test_pair_start_returns_input_request(backend: FakeBackend) -> None:
    client = FakeHomekitClient()
    result = asyncio.run(HomekitProtocol(client=client).control(_device(), "pair_start"))
    assert result.success
    assert result.input_required is not None
    assert result.input_required.follow_up_action == "pair_finish"
    assert result.input_required.type == "text"
    # pair_start does NO aiohomekit work (the handshake happens in pair_finish).
    assert client.paired is None


def test_pair_finish_does_full_pairing_and_persists(backend: FakeBackend) -> None:
    proto = HomekitProtocol(client=FakeHomekitClient())
    start = asyncio.run(proto.control(_device(), "pair_start"))
    session_id = start.input_required.session_id
    result = asyncio.run(proto.control(_device(), "pair_finish", {"session_id": session_id, "pin": "12345678"}))
    assert result.success
    # pair() called with the device's hap id + code reformatted to XXX-XX-XXX
    assert proto._client.paired["hap"] == "AA:BB:CC"
    assert proto._client.paired["code"] == "123-45-678"
    # pairing data persisted under the entity_id
    assert backend.get("homekit_device", "pairing:bedroom") == {"AccessoryPairingID": "AA:BB:CC", "secret": "ltsk"}


def test_pair_finish_with_expired_session_errors(backend: FakeBackend) -> None:
    result = asyncio.run(
        HomekitProtocol(client=FakeHomekitClient()).control(
            _device(), "pair_finish", {"session_id": "nope", "pin": "12345678"}
        )
    )
    assert not result.success
    assert "expired" in (result.error or "").lower()


# --------------------------------------------------------------------------- #
# Control — climate
# --------------------------------------------------------------------------- #
def test_set_temperature_fahrenheit_converts_to_celsius(backend: FakeBackend) -> None:
    _seed_pairing(backend)  # default unit = F
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    result = asyncio.run(client_control(client, "set_temperature", {"temperature": 72}))
    assert result.success
    # 72°F → 22.0°C, written to TargetTemperature (iid 10) on aid 1
    assert client.writes == [(1, 10, 22.0)]


def test_set_temperature_celsius_unit(backend: FakeBackend) -> None:
    backend.set_secret("HOMEKIT_TEMP_UNIT", "C", "integration")
    _seed_pairing(backend)
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    result = asyncio.run(client_control(client, "set_temperature", {"temperature": 21}))
    assert result.success
    assert client.writes == [(1, 10, 21.0)]


def test_set_mode_writes_target_heating_cooling_state(backend: FakeBackend) -> None:
    _seed_pairing(backend)
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    result = asyncio.run(client_control(client, "set_mode", {"mode": "cool"}))
    assert result.success
    assert client.writes == [(1, 11, 2)]  # cool == 2 on iid 11


def test_control_without_pairing_errors(backend: FakeBackend) -> None:
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    result = asyncio.run(client_control(client, "set_temperature", {"temperature": 70}))
    assert not result.success
    assert "not paired" in (result.error or "").lower()


# --------------------------------------------------------------------------- #
# Control — switch / lock
# --------------------------------------------------------------------------- #
def test_switch_turn_on(backend: FakeBackend) -> None:
    _seed_pairing(backend, "outlet")
    client = FakeHomekitClient(accessories=SWITCH_ACC)
    result = asyncio.run(
        HomekitProtocol(client=client).control(_device("switch", "outlet"), "turn_on")
    )
    assert result.success
    assert client.writes == [(1, 5, True)]


def test_lock_lock(backend: FakeBackend) -> None:
    _seed_pairing(backend, "frontdoor")
    client = FakeHomekitClient(accessories=LOCK_ACC)
    result = asyncio.run(
        HomekitProtocol(client=client).control(_device("lock", "frontdoor"), "lock")
    )
    assert result.success
    assert client.writes == [(1, 7, 1)]  # LockTargetState iid 7 → secured(1)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def test_on_removed_unpairs_and_clears(backend: FakeBackend) -> None:
    _seed_pairing(backend, "bedroom")
    client = FakeHomekitClient()
    asyncio.run(HomekitProtocol(client=client).on_removed(_device("climate", "bedroom")))
    assert client.removed is not None  # HAP remove_pairing was called
    assert backend.get("homekit_device", "pairing:bedroom") is None  # forgot our side


def test_on_removed_without_pairing_is_noop(backend: FakeBackend) -> None:
    client = FakeHomekitClient()
    asyncio.run(HomekitProtocol(client=client).on_removed(_device("climate", "neverpaired")))
    assert client.removed is None


def test_get_state_unpaired_signals_needs_pairing(backend: FakeBackend) -> None:
    state = asyncio.run(
        HomekitProtocol(client=FakeHomekitClient()).get_state(device=_device())
    )
    assert state == {"needs_pairing": True, "state": "unpaired"}


def test_get_state_climate_converts_to_user_unit(backend: FakeBackend) -> None:
    _seed_pairing(backend)
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    state = asyncio.run(HomekitProtocol(client=client).get_state(device=_device()))
    assert state["current_temperature"] == 70   # 21.0°C → 70°F
    assert state["target_temperature"] == 68     # 20.0°C → 68°F
    assert state["mode"] == "heat"               # node climate normalizer reads `mode`
    assert state["hvac_action"] == "heating"
    assert state["humidity"] == 44
    assert "off" in state["available_modes"]


def test_get_state_real_handler_signature_detects_domain(backend: FakeBackend) -> None:
    # The node state-query path calls get_state(ip=, entity_id=, cloud_id=, mac_address=)
    # with NO device and NO domain — the protocol must detect the domain from the
    # accessory's services (Thermostat → climate).
    _seed_pairing(backend, "bedroom")
    client = FakeHomekitClient(accessories=THERMOSTAT_ACC)
    state = asyncio.run(
        HomekitProtocol(client=client).get_state(
            ip="", entity_id="bedroom", cloud_id="AA:BB:CC", mac_address=""
        )
    )
    assert state["current_temperature"] == 70
    assert state["mode"] == "heat"


def client_control(client: FakeHomekitClient, action: str, data: dict) -> Any:
    """Helper: run a climate control action against the bedroom device."""
    return HomekitProtocol(client=client).control(_device(), action, data)


# --------------------------------------------------------------------------- #
# Already paired elsewhere (issue #3: "Pairing failed: step bytearray(b'\\x02')")
# --------------------------------------------------------------------------- #
# aiohomekit's errors, by name: pair_finish maps the class (its message is only the stage).
class UnavailableError(Exception):
    pass


class AuthenticationError(Exception):
    pass


def _pair(proto: Any, device: DiscoveredDevice) -> Any:
    start = asyncio.run(proto.control(device, "pair_start"))
    return asyncio.run(proto.control(device, "pair_finish", {"session_id": start.input_required.session_id, "pin": "12345678"}))


@pytest.fixture(autouse=True)
def _forget_paired_elsewhere() -> None:
    hk_mod._PAIRED_ELSEWHERE.clear()


@pytest.mark.parametrize("error, says", [
    (UnavailableError(bytearray(b"\x02")), "already paired with another home"),
    (AuthenticationError("step 4"), "setup code didn't work"),
    (RuntimeError("socket closed"), "Pairing failed (RuntimeError: socket closed)"),
])
def test_pair_finish_says_why(backend: FakeBackend, error: Exception, says: str) -> None:
    result = _pair(HomekitProtocol(client=FakeHomekitClient(pair_error=error)), _device())
    assert not result.success
    assert says in result.error
    assert "bytearray" not in result.error
    assert backend.get("homekit_device", "pairing:bedroom") is None


def test_paired_elsewhere_is_refused_before_asking_for_a_code(backend: FakeBackend) -> None:
    hue = {"hap_id": "67:E4:76:B1:12:AA", "name": "Philips Hue HomeKit", "model": "BSB002", "category": 2, "paired": True}
    proto = HomekitProtocol(client=FakeHomekitClient(discover_result=[hue]))
    devices = asyncio.run(proto.discover())
    assert devices[0].extra["paired_elsewhere"] is True
    device = _device("switch", devices[0].entity_id, hue["hap_id"])

    # The app shows the reason instead of a Pair button…
    state = asyncio.run(proto.get_state(device=device))
    assert "needs_pairing" not in state and "already paired with another home" in state["error"]
    # …and pair_start (an old screen, a routine) refuses without prompting for a code.
    result = asyncio.run(proto.control(device, "pair_start"))
    assert not result.success and result.input_required is None
    assert "already paired with another home" in result.error

    # Removed from Apple Home: the next discovery brings Pair back.
    proto._client._discover = [{**hue, "paired": False}]
    asyncio.run(proto.discover())
    assert asyncio.run(proto.get_state(device=device)) == {"needs_pairing": True, "state": "unpaired"}
    assert asyncio.run(proto.control(device, "pair_start")).input_required is not None


def test_pair_start_looks_first_when_not_discovered_yet(backend: FakeBackend) -> None:
    # After a node restart nothing is known until an app scan; pair_start discovers itself.
    hue = {"hap_id": "AA:BB:CC", "name": "Bedroom", "model": "BSB002", "category": 2, "paired": True}
    result = asyncio.run(HomekitProtocol(client=FakeHomekitClient(discover_result=[hue])).control(_device(), "pair_start"))
    assert not result.success and result.input_required is None
    assert "already paired with another home" in result.error


def test_paired_here_is_not_paired_elsewhere(backend: FakeBackend) -> None:
    # Once Jarvis holds the pairing, the accessory advertises paired: that's us, not another home.
    _seed_pairing(backend, "bedroom")
    proto = HomekitProtocol(client=FakeHomekitClient(
        discover_result=[{"hap_id": "AA:BB:CC", "name": "Bedroom", "model": "ecobee", "category": 9, "paired": True}],
        accessories=THERMOSTAT_ACC))
    asyncio.run(proto.discover())
    assert "error" not in asyncio.run(proto.get_state(device=_device()))
