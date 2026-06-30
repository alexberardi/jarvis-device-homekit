"""HomeKit Accessory Protocol (HAP) constants + pure mapping helpers.

This module has **no aiohomekit dependency** so it is fully unit-testable and
importable without the native HAP stack installed. It encodes:

- HAP accessory-category ids (known at *discovery* time, before pairing)
- HAP service-type ids → Jarvis HA-style domains (known after pairing, by reading
  the accessory database)
- HAP characteristic-type ids
- value mappings (HVAC mode, lock state) and °C↔°F conversion
- helpers to parse the accessory DB returned by aiohomekit's
  ``list_accessories_and_characteristics()``

HAP identifies services/characteristics by 128-bit UUIDs whose Apple-defined
short form is the hex prefix of ``XXXXXXXX-0000-1000-8000-0026BB765291``. We
compare by integer value so ``"25"``, ``"00000025"`` and the full UUID all match.
"""

from __future__ import annotations

from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# HAP accessory categories (advertised in mDNS, available BEFORE pairing).
# https://developer.apple.com/.../accessory-categories
# --------------------------------------------------------------------------- #
CATEGORY_TO_DOMAIN: dict[int, str] = {
    5: "light",    # Lighting
    6: "lock",     # Locks
    7: "switch",   # Outlets
    8: "switch",   # Switches
    9: "climate",  # Thermostats
    10: "sensor",  # Sensors
    14: "cover",   # Window Coverings (discovered, not controlled in v1)
}

# --------------------------------------------------------------------------- #
# HAP service-type ids (read from the accessory DB AFTER pairing).
# --------------------------------------------------------------------------- #
SERVICE_THERMOSTAT = "4A"
SERVICE_LIGHTBULB = "43"
SERVICE_SWITCH = "49"
SERVICE_OUTLET = "47"
SERVICE_LOCK_MECHANISM = "45"
SERVICE_TEMPERATURE_SENSOR = "8A"
SERVICE_HUMIDITY_SENSOR = "82"
SERVICE_CONTACT_SENSOR = "80"
SERVICE_MOTION_SENSOR = "85"

# --------------------------------------------------------------------------- #
# HAP characteristic-type ids.
# --------------------------------------------------------------------------- #
CHAR_ON = "25"
CHAR_BRIGHTNESS = "8"
CHAR_HUE = "13"
CHAR_SATURATION = "2F"
CHAR_CURRENT_TEMPERATURE = "11"
CHAR_TARGET_TEMPERATURE = "35"
CHAR_CURRENT_HEATING_COOLING = "F"
CHAR_TARGET_HEATING_COOLING = "33"
CHAR_TEMPERATURE_DISPLAY_UNITS = "36"
CHAR_COOLING_THRESHOLD = "D"
CHAR_HEATING_THRESHOLD = "12"
CHAR_CURRENT_RELATIVE_HUMIDITY = "10"
CHAR_LOCK_CURRENT_STATE = "1D"
CHAR_LOCK_TARGET_STATE = "1E"
CHAR_CONTACT_SENSOR_STATE = "6A"
CHAR_MOTION_DETECTED = "22"


def hap_id(type_str: Any) -> int:
    """Normalize a HAP short id or full UUID to its integer value.

    ``"25"`` → 0x25, ``"00000025-0000-1000-8000-0026BB765291"`` → 0x25.
    Returns -1 for anything unparseable.
    """
    try:
        return int(str(type_str).split("-")[0], 16)
    except (ValueError, AttributeError):
        return -1


def same_type(a: Any, b: Any) -> bool:
    return hap_id(a) == hap_id(b)


SERVICE_TO_DOMAIN: dict[int, str] = {
    hap_id(SERVICE_THERMOSTAT): "climate",
    hap_id(SERVICE_LIGHTBULB): "light",
    hap_id(SERVICE_SWITCH): "switch",
    hap_id(SERVICE_OUTLET): "switch",
    hap_id(SERVICE_LOCK_MECHANISM): "lock",
    hap_id(SERVICE_TEMPERATURE_SENSOR): "sensor",
    hap_id(SERVICE_HUMIDITY_SENSOR): "sensor",
    hap_id(SERVICE_CONTACT_SENSOR): "sensor",
    hap_id(SERVICE_MOTION_SENSOR): "sensor",
}

# --------------------------------------------------------------------------- #
# Value mappings
# --------------------------------------------------------------------------- #
# TargetHeatingCoolingState: 0=Off 1=Heat 2=Cool 3=Auto
MODE_TO_HK: dict[str, int] = {"off": 0, "heat": 1, "cool": 2, "auto": 3}
HK_TO_MODE: dict[int, str] = {0: "off", 1: "heat", 2: "cool", 3: "auto"}
# CurrentHeatingCoolingState: 0=Off 1=Heat 2=Cool (what the unit is actually doing)
HK_ACTION: dict[int, str] = {0: "idle", 1: "heating", 2: "cooling"}
# LockTargetState: 0=Unsecured(unlocked) 1=Secured(locked)
LOCK_TARGET: dict[str, int] = {"unlock": 0, "lock": 1}
# LockCurrentState: 0=Unsecured 1=Secured 2=Jammed 3=Unknown
LOCK_CURRENT: dict[int, str] = {0: "unlocked", 1: "locked", 2: "jammed", 3: "unknown"}


def f_to_c(value: float) -> float:
    """°F → °C, rounded to the HAP-typical 0.5° step."""
    return round(((value - 32.0) * 5.0 / 9.0) * 2.0) / 2.0


def c_to_f(value: float) -> int:
    """°C → °F, rounded to whole degrees for display."""
    return round(value * 9.0 / 5.0 + 32.0)


def to_celsius(value: float, unit: str) -> float:
    """Convert a user-supplied setpoint to °C (HAP wire unit)."""
    return f_to_c(value) if str(unit).upper().startswith("F") else round(value * 2.0) / 2.0


def from_celsius(value: float, unit: str) -> float | int:
    """Convert a °C reading from the device to the user's display unit."""
    return c_to_f(value) if str(unit).upper().startswith("F") else round(value, 1)


# --------------------------------------------------------------------------- #
# Accessory-DB parsing (shape from aiohomekit list_accessories_and_characteristics)
#   [ {"aid": 1, "services": [ {"iid": 8, "type": <svc-uuid>,
#        "characteristics": [ {"iid": 9, "type": <char-uuid>, "value": ...,
#                              "minValue":..,"maxValue":..,"minStep":..}, ... ] } ] } ]
# --------------------------------------------------------------------------- #
def iter_services(
    accessories: list[dict[str, Any]],
) -> Iterator[tuple[int | None, dict[str, Any]]]:
    for acc in accessories or []:
        aid = acc.get("aid")
        for svc in acc.get("services", []) or []:
            yield aid, svc


def service_domain(svc: dict[str, Any]) -> str | None:
    return SERVICE_TO_DOMAIN.get(hap_id(svc.get("type", "")))


def find_primary_service(
    accessories: list[dict[str, Any]], domain: str
) -> tuple[int | None, dict[str, Any] | None]:
    """Return ``(aid, service)`` for the first service matching ``domain``."""
    for aid, svc in iter_services(accessories):
        if service_domain(svc) == domain:
            return aid, svc
    return None, None


# Keyword fallback when the advertised HAP category is unmapped/Other. Apple's
# HomeKit Accessory Simulator advertises category 1 (Other) even for a thermostat,
# and some real accessories under-specify their category. Names/models are the only
# other pre-pairing hint we have.
_NAME_DOMAIN_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("climate", ("thermostat", "hvac", "climate", "heat pump", "ac ")),
    ("lock", ("lock", "deadbolt", "latch")),
    ("light", ("light", "bulb", "lamp", "lightstrip", "sconce", "downlight")),
    ("switch", ("outlet", "plug", "switch", "socket")),
    ("sensor", ("sensor", "motion", "contact", "humidity")),
]


def domain_from_name(text: str) -> str | None:
    """Best-effort domain guess from an accessory's name/model string."""
    lowered = f" {(text or '').lower()} "
    for domain, keywords in _NAME_DOMAIN_HINTS:
        if any(kw in lowered for kw in keywords):
            return domain
    return None


# Priority when an accessory exposes several controllable services and the caller
# didn't tell us which domain to use (the node state-query path doesn't pass one).
_DOMAIN_PRIORITY = ["climate", "lock", "light", "switch", "sensor"]


def primary_domain(accessories: list[dict[str, Any]]) -> str | None:
    """Best controllable domain present on the accessory (priority-ordered)."""
    present = {service_domain(svc) for _aid, svc in iter_services(accessories)}
    for domain in _DOMAIN_PRIORITY:
        if domain in present:
            return domain
    return None


def chars_by_type(svc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map characteristic-type-int → characteristic dict for a service."""
    out: dict[int, dict[str, Any]] = {}
    for char in svc.get("characteristics", []) or []:
        out[hap_id(char.get("type", ""))] = char
    return out


def quantize_setpoint(value: float, char: dict[str, Any] | None) -> float:
    """Clamp/round a °C setpoint to the characteristic's min/max/step metadata."""
    if not char:
        return value
    lo = char.get("minValue")
    hi = char.get("maxValue")
    step = char.get("minStep") or 0.5
    if isinstance(lo, (int, float)):
        value = max(float(lo), value)
    if isinstance(hi, (int, float)):
        value = min(float(hi), value)
    if step:
        value = round(value / step) * step
    return round(value, 2)
