"""Manual HomeKit pair + control harness — run ON a node against a real or
simulated accessory (e.g. Apple's HomeKit Accessory Simulator).

Exercises the REAL aiohomekit path (the one the unit tests mock):
  discover -> pair_start -> pair_finish(<setup code>) -> set_temperature/set_mode
  -> get_state read-back.

The pairing is PERSISTED to a JSON file (default /tmp/hk_store.json) so re-runs
reuse it instead of re-pairing (HAP is single-controller). Delete that file (and
reset the accessory in the simulator) to start clean.

Usage (on the node):
    cd /opt/jarvis-node
    PYTHONPATH=/opt/jarvis-node .venv/bin/python /tmp/sim_pair_harness.py <setup-code> [name-substring]
    # re-run with NO code to reuse the saved pairing:
    PYTHONPATH=/opt/jarvis-node .venv/bin/python /tmp/sim_pair_harness.py "" thermostat
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from jarvis_command_sdk import StorageBackend, set_backend

_STORE = "/tmp/hk_store.json"


class _FileBackend(StorageBackend):
    """Storage backend that persists to a JSON file across runs."""

    def __init__(self, path: str = _STORE) -> None:
        self._path = path
        self._d: dict[str, dict] = {}
        self._s: dict[str, str] = {}
        if os.path.exists(path):
            raw = json.load(open(path))
            self._d = {tuple(k.split("\x1f")): v for k, v in raw.get("data", {}).items()}
            self._s = raw.get("secrets", {})

    def _flush(self) -> None:
        json.dump(
            {"data": {"\x1f".join(k): v for k, v in self._d.items()}, "secrets": self._s},
            open(self._path, "w"),
        )

    def save(self, command_name, data_key, data, expires_at=None):
        self._d[(command_name, data_key)] = data
        self._flush()

    def get(self, command_name, data_key):
        return self._d.get((command_name, data_key))

    def get_all(self, command_name):
        return [v for (c, _), v in self._d.items() if c == command_name]

    def delete(self, command_name, data_key):
        ok = self._d.pop((command_name, data_key), None) is not None
        self._flush()
        return ok

    def delete_all(self, command_name):
        keys = [k for k in self._d if k[0] == command_name]
        for k in keys:
            self._d.pop(k)
        self._flush()
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self._s.get(key)

    def set_secret(self, key, value, scope, value_type="string", user_id=None):
        self._s[key] = value
        self._flush()

    def delete_secret(self, key, scope, user_id=None):
        self._s.pop(key, None)
        self._flush()


async def main(code: str, name_sub: str) -> None:
    backend = _FileBackend()
    set_backend(backend)
    from utils.device_family_discovery_service import get_device_family_discovery_service

    hk = get_device_family_discovery_service().discover_families()["homekit"]

    print("Discovering…")
    devices = await hk.discover(timeout=10)
    target = next((d for d in devices if name_sub.lower() in d.name.lower()), None)
    if target is None:
        print(f"No accessory matching {name_sub!r}. Saw: {[d.name for d in devices]}")
        return
    print(f"Target: {target.name} | domain={target.domain} | hap={target.cloud_id} "
          f"| paired_elsewhere={target.extra.get('paired_elsewhere')}")

    stored = backend.get("homekit_device", f"pairing:{target.entity_id}")
    if stored:
        print("Reusing saved pairing (skipping pair).")
    else:
        if not code:
            print("No saved pairing and no setup code given — pass the code to pair.")
            return
        r = await hk.control(target, "pair_start")
        if not r.input_required:
            print(f"  pair_start failed: {r.error}")
            return
        r = await hk.control(target, "pair_finish",
                             {"session_id": r.input_required.session_id, "pin": code})
        if not r.success:
            print(f"  pair_finish failed: {r.error}")
            return
        print("  paired ✓")

    # RAW accessory DB dump (so we can see the exact structure aiohomekit returns).
    pairing_data = backend.get("homekit_device", f"pairing:{target.entity_id}")
    try:
        accs = await hk._client.get_accessories(pairing_data)
        print("=== RAW /accessories (truncated) ===")
        print(json.dumps(accs, indent=2, default=str)[:2500])
        print("=== service types seen ===")
        for acc in accs:
            for svc in acc.get("services", []):
                print("  aid", acc.get("aid"), "svc.type", svc.get("type"),
                      "| chars", [c.get("type") for c in svc.get("characteristics", [])][:6])
    except Exception as e:
        print("get_accessories raised:", repr(e))
        return

    print("=== protocol get_state / control ===")
    print("state:", await hk.get_state(device=target, entity_id=target.entity_id))
    r = await hk.control(target, "set_temperature", {"temperature": 71})
    print(f"set_temperature 71: success={r.success} {r.error or ''}")
    r = await hk.control(target, "set_mode", {"mode": "heat"})
    print(f"set_mode heat: success={r.success} {r.error or ''}")
    print("state after:", await hk.get_state(device=target, entity_id=target.entity_id))


if __name__ == "__main__":
    setup_code = sys.argv[1] if len(sys.argv) > 1 else ""
    substring = sys.argv[2] if len(sys.argv) > 2 else "thermostat"
    asyncio.run(main(setup_code, substring))
