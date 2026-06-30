"""Thin async wrapper around ``aiohomekit`` — the single integration seam.

All ``aiohomekit`` usage is isolated here so the protocol + tests depend only on
this small interface (tests inject a fake), and ``aiohomekit`` is imported lazily
so ``protocol.py`` stays importable without the native stack (lets ``jdt test``
pass in the Pantry container).

IMPORTANT — every operation is SELF-CONTAINED: it creates a fresh aiohomekit
Controller (+ the required `_hap._tcp`/`_hap._udp` zeroconf browsers) bound to the
*current* event loop, does its work, and tears it down. The node runs each request
in its own ``asyncio.run()`` loop, so a Controller persisted across calls dies with
"Event loop is closed" on the second request. Hence no shared controller; pairing
is done in a single call (`pair`) because HomeKit's setup code is static and needs
no mid-handshake round trip.

``aiohomekit`` offers "no API guarantees" — if its surface drifts, this file is the
only place to adjust (pin the version in ``jarvis_package.yaml``).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class HomekitUnavailable(RuntimeError):
    """Raised when aiohomekit (or its native deps) is not importable."""


def _as_int(value: Any) -> int:
    """Coerce an int / IntEnum / .value enum to a plain int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(getattr(value, "value", 0) or 0)


class HomekitClient:
    """Stateless HAP client: one fresh Controller per operation."""

    def __init__(self) -> None:
        # Intentionally no persistent controller/zeroconf — see module docstring.
        pass

    def _import(self) -> tuple[Any, Any]:
        try:
            from aiohomekit import Controller
            from aiohomekit.characteristic_cache import CharacteristicCacheMemory
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise HomekitUnavailable(
                "aiohomekit is not installed. Run: pip install aiohomekit"
            ) from exc
        return Controller, CharacteristicCacheMemory

    @asynccontextmanager
    async def _controller(self) -> AsyncIterator[Any]:
        """A started Controller (with the required HAP browsers), torn down on exit.

        aiohomekit ATTACHES to existing `_hap._tcp`/`_hap._udp` AsyncServiceBrowsers
        (HA supplies a shared one) — without them the IP/CoAP transports raise
        "no zeroconf browser for _hap._tcp.local." on start, so we start both here.
        """
        Controller, CharacteristicCacheMemory = self._import()
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
        from aiohomekit.zeroconf import ZeroconfServiceListener

        azc = AsyncZeroconf()
        browsers = [
            AsyncServiceBrowser(azc.zeroconf, [hap_type], listener=ZeroconfServiceListener())
            for hap_type in ("_hap._tcp.local.", "_hap._udp.local.")
        ]
        controller = Controller(async_zeroconf_instance=azc, char_cache=CharacteristicCacheMemory())
        await controller.async_start()
        try:
            yield controller
        finally:
            for browser in browsers:
                try:
                    await browser.async_cancel()
                except Exception:  # pragma: no cover - best effort
                    pass
            try:
                await controller.async_stop()
            except Exception:  # pragma: no cover - best effort
                pass
            try:
                await azc.async_close()
            except Exception:  # pragma: no cover - best effort
                pass

    @staticmethod
    def _alias(pairing_data: dict[str, Any]) -> str:
        return str(pairing_data.get("AccessoryPairingID") or pairing_data.get("alias") or "homekit")

    # -- discovery ---------------------------------------------------------- #

    async def discover(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Return ``[{hap_id, name, model, category, paired}]`` for LAN accessories."""
        async with self._controller() as controller:
            # async_discover() doesn't wait — give the fresh browser time to gather.
            await asyncio.sleep(timeout)
            found: list[dict[str, Any]] = []
            async for disc in controller.async_discover(timeout):
                desc = getattr(disc, "description", None)
                if desc is None:
                    continue
                found.append(
                    {
                        "hap_id": getattr(desc, "id", "") or "",
                        "name": getattr(desc, "name", "") or "",
                        "model": getattr(desc, "model", "") or "",
                        "category": _as_int(getattr(desc, "category", 0)),
                        "paired": bool(getattr(disc, "paired", False)),
                    }
                )
            return found

    # -- pairing (single-shot: HomeKit setup code is static) ---------------- #

    async def pair(self, hap_id: str, alias: str, code: str) -> dict[str, Any]:
        """Find the accessory, run the full SRP pairing with the setup code, and
        return the pairing-data dict to persist. Done in one call so it never
        spans event loops."""
        async with self._controller() as controller:
            # Let the browser surface the accessory before async_find.
            await asyncio.sleep(2.0)
            discovery = await controller.async_find(hap_id)
            finish_pairing = await discovery.async_start_pairing(alias)
            pairing = await finish_pairing(code)
            return dict(pairing.pairing_data)

    # -- control / state ---------------------------------------------------- #

    async def get_accessories(self, pairing_data: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._controller() as controller:
            pairing = controller.load_pairing(self._alias(pairing_data), dict(pairing_data))
            return await pairing.list_accessories_and_characteristics()

    async def put_characteristics(
        self, pairing_data: dict[str, Any], writes: list[tuple[int, int, Any]]
    ) -> Any:
        async with self._controller() as controller:
            pairing = controller.load_pairing(self._alias(pairing_data), dict(pairing_data))
            return await pairing.put_characteristics(writes)

    async def remove_pairing(self, pairing_data: dict[str, Any]) -> None:
        """Unpair from the accessory — deletes the pairing on BOTH ends (accessory
        + controller), so the accessory becomes pairable again."""
        async with self._controller() as controller:
            alias = self._alias(pairing_data)
            controller.load_pairing(alias, dict(pairing_data))
            await controller.remove_pairing(alias)
