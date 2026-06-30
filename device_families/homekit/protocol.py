"""Generic Apple HomeKit (HAP) device protocol — local LAN control.

Discovers HomeKit accessories over mDNS, pairs once with the accessory's 8-digit
setup code (via the ``InputRequest`` PIN flow, like ``jarvis-device-apple``), then
controls + reads them locally over the LAN — no cloud, no account, no API key.

v1 domains: climate (thermostats — e.g. ecobee), switch/outlet, light, lock;
sensors are read-only. All ``aiohomekit`` usage is isolated in
``homekit_shared.homekit_client`` so this module is importable without the native
HAP stack and is unit-tested by injecting a fake client.
"""

from __future__ import annotations

import colorsys
import re
import time
import uuid
from typing import Any, Literal

from jarvis_command_sdk import (
    IJarvisDeviceProtocol,
    IJarvisSecret,
    DiscoveredDevice,
    DeviceControlResult,
    InputRequest,
    IJarvisButton,
    JarvisSecret,
    JarvisStorage,
)

from homekit_shared import characteristics as ch
from homekit_shared.homekit_client import HomekitClient, HomekitUnavailable

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug(msg)


logger = JarvisLogger(service="device.homekit")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _normalize_setup_code(raw: str) -> str:
    """HomeKit setup codes are 8 digits; aiohomekit wants ``XXX-XX-XXX``."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    if len(digits) == 8:
        return f"{digits[0:3]}-{digits[3:5]}-{digits[5:8]}"
    return (raw or "").strip()


def _rgb_to_hue_sat(rgb: list[int]) -> tuple[float, float]:
    r, g, b = (max(0, min(255, int(x))) / 255.0 for x in rgb)
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 360.0, 1), round(s * 100.0, 1)


def _hue_sat_to_rgb(hue: float, sat: float) -> list[int]:
    r, g, b = colorsys.hsv_to_rgb((hue or 0.0) / 360.0, (sat or 0.0) / 100.0, 1.0)
    return [round(r * 255), round(g * 255), round(b * 255)]


class _ControlError(Exception):
    """User-facing control validation error (mapped to DeviceControlResult.error)."""


class HomekitProtocol(IJarvisDeviceProtocol):
    """Apple HomeKit (HAP) LAN protocol adapter."""

    protocol_name: str = "homekit"
    friendly_name: str = "HomeKit"
    supported_domains: list[str] = ["climate", "switch", "light", "lock", "sensor"]
    connection_type: Literal["lan", "cloud", "hybrid"] = "lan"
    description: str = "Control Apple HomeKit accessories locally over your LAN"

    # Pairing sessions expire after 5 minutes (setup-code entry window).
    _PAIRING_SESSION_TTL: int = 300

    setup_guide: str = """## Pairing a HomeKit accessory

1. Tap **Scan for Devices** to find HomeKit accessories on your network.
2. Import the accessory, open it, and tap **Pair**.
3. Enter the accessory's **8-digit HomeKit setup code** (on the device, its box,
   or in the manufacturer's app — formatted like `123-45-678`).

> A HomeKit accessory can only be paired to **one** controller at a time. If it is
> already in Apple Home, remove it there first (pairing it to Jarvis means it
> leaves Apple Home / Siri)."""

    def __init__(self, client: HomekitClient | None = None) -> None:
        self._storage = JarvisStorage("homekit_device")
        self._client = client or HomekitClient()
        self._pairing_sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Declarations
    # ------------------------------------------------------------------ #
    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "HOMEKIT_TEMP_UNIT",
                "Temperature unit for thermostats (F or C)",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Temperature Unit",
                enum_values=["F", "C"],
            ),
        ]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Pair", "pair_start", "primary", "link"),
            IJarvisButton("Turn On", "turn_on", "primary", "power"),
            IJarvisButton("Turn Off", "turn_off", "secondary", "power-off"),
        ]

    def _temp_unit(self) -> str:
        return (self._storage.get_secret("HOMEKIT_TEMP_UNIT") or "F").strip().upper() or "F"

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        try:
            found = await self._client.discover(timeout)
        except HomekitUnavailable:
            logger.error("aiohomekit not installed; HomeKit discovery skipped")
            return []
        except Exception as e:
            logger.error(f"HomeKit discovery failed: {e}")
            return []

        devices: list[DiscoveredDevice] = []
        for item in found:
            hap: str = str(item.get("hap_id", "") or "")
            if not hap:
                continue
            category: int = int(item.get("category", 0) or 0)
            raw_name: str = item.get("name") or ""
            model: str = item.get("model", "") or ""
            # Category first (reliable for real devices); fall back to a name/model
            # keyword guess (covers the simulator's category-1 quirk); else switch.
            domain: str = (
                ch.CATEGORY_TO_DOMAIN.get(category)
                or ch.domain_from_name(f"{raw_name} {model}")
                or "switch"
            )
            name: str = raw_name or f"HomeKit {domain.title()}"
            devices.append(
                DiscoveredDevice(
                    entity_id=_slugify(name) or _slugify(hap),
                    name=name,
                    domain=domain,
                    protocol=self.protocol_name,
                    model=model or "HomeKit Accessory",
                    manufacturer="HomeKit",
                    # cloud_id survives the node's device-record reconstruction and
                    # is the stable HAP id we re-pair / re-connect with.
                    cloud_id=hap,
                    is_controllable=True,
                    extra={
                        "hap_id": hap,
                        "category": category,
                        "paired_elsewhere": bool(item.get("paired")),
                    },
                )
            )

        logger.info(f"HomeKit discovery found {len(devices)} accessory(ies)")
        return devices

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    async def control(
        self,
        device: Any,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        data = data or {}
        entity_id, hap, domain = self._ids(device, data, kwargs)

        if action == "pair_start":
            return await self._pair_start(entity_id, hap)
        if action == "pair_finish":
            return await self._pair_finish(entity_id, data)

        pairing_data = self._storage.get(f"pairing:{entity_id}") if entity_id else None
        if not pairing_data:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error="Not paired yet. Tap Pair and enter the HomeKit setup code first.",
            )

        try:
            accessories = await self._client.get_accessories(pairing_data)
        except HomekitUnavailable as e:
            return DeviceControlResult(False, entity_id, action, error=str(e))
        except Exception as e:
            return DeviceControlResult(
                False, entity_id, action, error=f"Could not reach HomeKit accessory: {e}",
            )

        # The device's stored domain can be wrong (e.g. the accessory was
        # mis-categorized at discovery). Trust the actual services: re-detect when
        # the stored domain isn't present on the paired accessory.
        if not domain or ch.find_primary_service(accessories, domain)[1] is None:
            domain = ch.primary_domain(accessories) or domain

        try:
            writes = self._build_writes(accessories, domain, action, data)
        except _ControlError as e:
            return DeviceControlResult(False, entity_id, action, error=str(e))

        if writes is None:
            return DeviceControlResult(
                False, entity_id, action,
                error=f"Unsupported action '{action}' for {domain or 'device'}",
            )

        try:
            await self._client.put_characteristics(pairing_data, writes)
            return DeviceControlResult(success=True, entity_id=entity_id, action=action)
        except Exception as e:
            return DeviceControlResult(False, entity_id, action, error=f"Control failed: {e}")

    def _build_writes(
        self,
        accessories: list[dict[str, Any]],
        domain: str,
        action: str,
        data: dict[str, Any],
    ) -> list[tuple[int, int, Any]] | None:
        """Translate a domain action into HAP characteristic writes ``[(aid, iid, value)]``.

        Returns None for an unsupported action; raises ``_ControlError`` for a
        user-facing validation problem.
        """
        aid, svc = ch.find_primary_service(accessories, domain)
        if svc is None or aid is None:
            raise _ControlError(f"No {domain or 'matching'} service found on this accessory")
        cmap = ch.chars_by_type(svc)

        def iid(type_id: str) -> int | None:
            c = cmap.get(ch.hap_id(type_id))
            return c.get("iid") if c else None

        if domain == "climate":
            return self._climate_writes(aid, cmap, iid, action, data)
        if domain == "light":
            return self._light_writes(aid, cmap, iid, action, data)
        if domain == "switch":
            on = iid(ch.CHAR_ON)
            if on is None:
                raise _ControlError("Switch has no power characteristic")
            if action == "turn_on":
                return [(aid, on, True)]
            if action == "turn_off":
                return [(aid, on, False)]
            return None
        if domain == "lock":
            target = iid(ch.CHAR_LOCK_TARGET_STATE)
            if target is None:
                raise _ControlError("Lock has no settable target state")
            if action in ch.LOCK_TARGET:
                return [(aid, target, ch.LOCK_TARGET[action])]
            return None
        # sensors and anything else: read-only
        return None

    def _climate_writes(
        self, aid: int, cmap: dict[int, dict[str, Any]],
        iid: Any, action: str, data: dict[str, Any],
    ) -> list[tuple[int, int, Any]] | None:
        if action == "set_temperature":
            raw = data.get("temperature", data.get("value", data.get("target_temperature")))
            if raw is None:
                raise _ControlError("set_temperature requires a temperature")
            char = cmap.get(ch.hap_id(ch.CHAR_TARGET_TEMPERATURE))
            if not char:
                raise _ControlError("This thermostat has no settable target temperature")
            celsius = ch.to_celsius(float(raw), self._temp_unit())
            celsius = ch.quantize_setpoint(celsius, char)
            return [(aid, char["iid"], celsius)]
        if action == "set_mode":
            mode = str(data.get("mode", data.get("value", ""))).strip().lower()
            if mode not in ch.MODE_TO_HK:
                raise _ControlError(f"Unknown mode '{mode}' (use off/heat/cool/auto)")
            target = iid(ch.CHAR_TARGET_HEATING_COOLING)
            if target is None:
                raise _ControlError("This thermostat's mode is not settable")
            return [(aid, target, ch.MODE_TO_HK[mode])]
        if action == "turn_off":
            target = iid(ch.CHAR_TARGET_HEATING_COOLING)
            if target is not None:
                return [(aid, target, ch.MODE_TO_HK["off"])]
        return None

    def _light_writes(
        self, aid: int, cmap: dict[int, dict[str, Any]],
        iid: Any, action: str, data: dict[str, Any],
    ) -> list[tuple[int, int, Any]] | None:
        on = iid(ch.CHAR_ON)
        if action == "turn_on" and on is not None:
            return [(aid, on, True)]
        if action == "turn_off" and on is not None:
            return [(aid, on, False)]
        if action == "set_brightness":
            bri_char = cmap.get(ch.hap_id(ch.CHAR_BRIGHTNESS))
            if not bri_char:
                raise _ControlError("This light is not dimmable")
            pct = max(0, min(100, int(data.get("brightness", data.get("value", 100)))))
            writes: list[tuple[int, int, Any]] = []
            if on is not None:
                writes.append((aid, on, True))
            writes.append((aid, bri_char["iid"], pct))
            return writes
        if action == "set_color":
            rgb = data.get("rgb")
            if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
                raise _ControlError("set_color requires an 'rgb' [r, g, b] value")
            hue_char = cmap.get(ch.hap_id(ch.CHAR_HUE))
            sat_char = cmap.get(ch.hap_id(ch.CHAR_SATURATION))
            if not hue_char or not sat_char:
                raise _ControlError("This light does not support color")
            hue, sat = _rgb_to_hue_sat(list(rgb))
            writes = []
            if on is not None:
                writes.append((aid, on, True))
            writes.append((aid, hue_char["iid"], hue))
            writes.append((aid, sat_char["iid"], sat))
            return writes
        return None

    # ------------------------------------------------------------------ #
    # Pairing
    # ------------------------------------------------------------------ #
    async def _pair_start(self, entity_id: str, hap: str) -> DeviceControlResult:
        # No aiohomekit work here — just record the target + prompt for the code.
        # The whole handshake runs in pair_finish (one event loop), because the
        # node runs each request in its own asyncio.run() loop and an in-flight
        # pairing connection can't survive across loops. HomeKit's setup code is
        # static, so nothing needs to happen between start and finish.
        if not hap:
            return DeviceControlResult(
                False, entity_id, "pair_start",
                error="No HomeKit accessory id for this device",
            )
        self._cleanup_expired_sessions()
        session_id = str(uuid.uuid4())
        self._pairing_sessions[session_id] = {
            "hap": hap,
            "entity_id": entity_id,
            "created_at": time.monotonic(),
        }
        logger.info(f"HomeKit pairing requested for {entity_id}, session={session_id[:8]}")
        return DeviceControlResult(
            success=True, entity_id=entity_id, action="pair_start",
            input_required=InputRequest(
                type="text",
                prompt="Enter the 8-digit HomeKit setup code (e.g. 123-45-678)",
                session_id=session_id,
                follow_up_action="pair_finish",
            ),
        )

    async def _pair_finish(self, entity_id: str, data: dict[str, Any]) -> DeviceControlResult:
        session_id = str(data.get("session_id", ""))
        session = self._pairing_sessions.pop(session_id, None)
        if not session:
            return DeviceControlResult(
                False, entity_id, "pair_finish",
                error="Pairing session expired. Tap Pair to try again.",
            )
        entity_id = entity_id or str(session.get("entity_id", ""))
        hap = str(session.get("hap", ""))
        code = _normalize_setup_code(str(data.get("pin", data.get("code", ""))))
        if not code:
            return DeviceControlResult(
                False, entity_id, "pair_finish",
                error="A HomeKit setup code is required",
            )
        try:
            pairing_data = await self._client.pair(hap, alias=entity_id or hap, code=code)
        except HomekitUnavailable as e:
            return DeviceControlResult(False, entity_id, "pair_finish", error=str(e))
        except Exception as e:
            return DeviceControlResult(
                False, entity_id, "pair_finish", error=f"Pairing failed: {e}",
            )

        self._storage.save(f"pairing:{entity_id}", pairing_data)
        logger.info(f"HomeKit pairing complete for {entity_id}")
        return DeviceControlResult(success=True, entity_id=entity_id, action="pair_finish")

    def _cleanup_expired_sessions(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, sess in self._pairing_sessions.items()
            if now - sess.get("created_at", 0) > self._PAIRING_SESSION_TTL
        ]
        for sid in expired:
            self._pairing_sessions.pop(sid, None)
            logger.info(f"Expired HomeKit pairing session {sid[:8]}")

    # ------------------------------------------------------------------ #
    # Removal (called by the node when the device is deleted from Jarvis)
    # ------------------------------------------------------------------ #
    async def on_removed(self, device: Any, **kwargs: Any) -> None:
        """Unpair from the accessory and drop the stored pairing when the device
        is removed from Jarvis — so the accessory is released (HAP is
        single-controller) and a future re-add starts fresh (Pair flow)."""
        entity_id = str(getattr(device, "entity_id", "") or kwargs.get("entity_id", "") or "")
        if not entity_id:
            return
        pairing_data = self._storage.get(f"pairing:{entity_id}")
        if not pairing_data:
            return
        try:
            await self._client.remove_pairing(pairing_data)
            logger.info(f"HomeKit unpaired {entity_id} on removal")
        except HomekitUnavailable as e:
            logger.warning(f"HomeKit unpair skipped: {e}")
        except Exception as e:
            logger.warning(f"HomeKit unpair failed for {entity_id}: {e}")
        # Always forget our side so Jarvis is clean even if the accessory was
        # already gone/unreachable.
        self._storage.delete(f"pairing:{entity_id}")

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    async def get_state(self, ip: str = "", **kwargs: Any) -> dict[str, Any] | None:
        device = kwargs.get("device")
        entity_id = str(getattr(device, "entity_id", "") or kwargs.get("entity_id", "") or "")
        domain = str(getattr(device, "domain", "") or kwargs.get("domain", "") or "")

        pairing_data = self._storage.get(f"pairing:{entity_id}") if entity_id else None
        if not pairing_data:
            # Drives the mobile "Pair" affordance for an unpaired accessory.
            return {"needs_pairing": True, "state": "unpaired"}

        try:
            accessories = await self._client.get_accessories(pairing_data)
        except HomekitUnavailable as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Could not reach HomeKit accessory: {e}"}

        # The node state-query path doesn't pass a domain — detect it from the
        # accessory's services when absent (or when the passed one isn't present).
        if not domain or ch.find_primary_service(accessories, domain)[1] is None:
            domain = ch.primary_domain(accessories) or domain

        return self._normalize_state(accessories, domain)

    def _normalize_state(
        self, accessories: list[dict[str, Any]], domain: str
    ) -> dict[str, Any]:
        _, svc = ch.find_primary_service(accessories, domain)
        if svc is None:
            return {"error": f"No {domain or 'matching'} service found on accessory"}
        cmap = ch.chars_by_type(svc)
        unit = self._temp_unit()

        def val(type_id: str) -> Any:
            c = cmap.get(ch.hap_id(type_id))
            return c.get("value") if c else None

        if domain == "climate":
            # Keys chosen to match the node's ClimateDomainHandler.normalize_state:
            # it reads `mode` (target mode), `hvac_action`/`state` (what it's doing),
            # `current_temperature`, `target_temperature`, `humidity`, and uses
            # `available_modes` for the mode-chip UI hints.
            state: dict[str, Any] = {}
            cur = val(ch.CHAR_CURRENT_TEMPERATURE)
            tgt = val(ch.CHAR_TARGET_TEMPERATURE)
            mode = val(ch.CHAR_TARGET_HEATING_COOLING)
            act = val(ch.CHAR_CURRENT_HEATING_COOLING)
            hum = val(ch.CHAR_CURRENT_RELATIVE_HUMIDITY)
            if cur is not None:
                state["current_temperature"] = ch.from_celsius(cur, unit)
            if tgt is not None:
                state["target_temperature"] = ch.from_celsius(tgt, unit)
            if mode is not None:
                state["mode"] = ch.HK_TO_MODE.get(int(mode), "off")
            if act is not None:
                state["hvac_action"] = ch.HK_ACTION.get(int(act), "idle")
            if hum is not None:
                state["humidity"] = round(hum)
            state["available_modes"] = ["off", "heat", "cool", "auto"]
            return state

        if domain == "light":
            on = val(ch.CHAR_ON)
            light: dict[str, Any] = {"state": "on" if on else "off"}
            bri = val(ch.CHAR_BRIGHTNESS)
            features: list[str] = []
            if bri is not None:
                light["brightness"] = int(bri)
                features.append("brightness")
            hue = val(ch.CHAR_HUE)
            sat = val(ch.CHAR_SATURATION)
            if hue is not None and sat is not None:
                light["rgb"] = _hue_sat_to_rgb(hue, sat)
                features.append("color")
            light["available_features"] = features
            return light

        if domain == "switch":
            return {"state": "on" if val(ch.CHAR_ON) else "off"}

        if domain == "lock":
            cur = val(ch.CHAR_LOCK_CURRENT_STATE)
            return {"state": ch.LOCK_CURRENT.get(int(cur) if cur is not None else 3, "unknown")}

        if domain == "sensor":
            sensor: dict[str, Any] = {}
            temp = val(ch.CHAR_CURRENT_TEMPERATURE)
            if temp is not None:
                sensor["temperature"] = ch.from_celsius(temp, unit)
            hum = val(ch.CHAR_CURRENT_RELATIVE_HUMIDITY)
            if hum is not None:
                sensor["humidity"] = round(hum)
            contact = val(ch.CHAR_CONTACT_SENSOR_STATE)
            if contact is not None:
                sensor["contact"] = "open" if int(contact) else "closed"
            motion = val(ch.CHAR_MOTION_DETECTED)
            if motion is not None:
                sensor["motion"] = bool(motion)
            return sensor or {"state": "unknown"}

        return {"error": f"Unsupported domain '{domain}'"}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _ids(
        self, device: Any, data: dict[str, Any], kwargs: dict[str, Any]
    ) -> tuple[str, str, str]:
        """Extract (entity_id, hap_id, domain) from whichever source provides them."""
        entity_id = hap = domain = ""
        if device is not None and hasattr(device, "entity_id"):
            entity_id = str(getattr(device, "entity_id", "") or "")
            hap = str(getattr(device, "cloud_id", "") or "")
            domain = str(getattr(device, "domain", "") or "")
        if isinstance(data, dict):
            hap = hap or str(data.get("cloud_id", "") or data.get("hap_id", "") or "")
            entity_id = entity_id or str(data.get("entity_id", "") or "")
            domain = domain or str(data.get("domain", "") or "")
        hap = hap or str(kwargs.get("cloud_id", "") or "")
        return entity_id, hap, domain
