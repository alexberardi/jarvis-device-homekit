# Apple HomeKit (Local)

Jarvis device protocol for controlling **Apple HomeKit (HAP) accessories locally
over your LAN** — no cloud, no account, no API key. Built for the ecobee Smart
Thermostat (whose cloud developer API is closed to new developers) and any other
HomeKit accessory.

Control is handled through Jarvis's built-in device control — this package
contributes the `homekit` **protocol adapter** (discovery + pairing + control +
state); it does not ship its own voice command. (Every device package is
protocol-only and routes through the generic `control_device` path.)

## Features

- **Local LAN control** — talks HAP directly to the accessory; nothing leaves your network
- **Pair once** — enter the accessory's 8-digit HomeKit setup code in the app
- **Thermostats** (`climate`) — read temperature/humidity, set target temperature, set mode (off/heat/cool/auto)
- **Switches & outlets** (`switch`) — on/off
- **Lights** (`light`) — on/off, brightness, RGB color
- **Locks** (`lock`) — lock/unlock
- **Sensors** (`sensor`) — read-only temperature/humidity/contact/motion

## Supported domains

| Domain | HAP service | Actions |
|---|---|---|
| `climate` | Thermostat | `set_temperature`, `set_mode`, `turn_off` |
| `light` | Lightbulb | `turn_on`, `turn_off`, `set_brightness`, `set_color` |
| `switch` | Switch / Outlet | `turn_on`, `turn_off` |
| `lock` | Lock Mechanism | `lock`, `unlock` |
| `sensor` | Temperature / Humidity / Contact / Motion | read-only |

## Setup

1. Install the package on your node (via Pantry, or `jdt deploy`).
2. In the Jarvis app, **Scan for Devices** → import the HomeKit accessory.
3. Open the device, tap **Pair**, and enter its **8-digit HomeKit setup code**
   (printed on the device, its box, or shown in the manufacturer's app — e.g.
   `123-45-678`).

> **One controller at a time.** A HomeKit accessory can only be paired to a single
> controller. If it is already in **Apple Home**, remove it there first — pairing
> it to Jarvis means it leaves Apple Home / Siri.

## Settings

| Secret | Required | Description |
|---|---|---|
| `HOMEKIT_TEMP_UNIT` | no | Thermostat temperature unit, `F` (default) or `C` |

## Notes

- Discovery is mDNS-based (no extra config on native nodes).
- HomeKit temperatures are exchanged in Celsius on the wire; the protocol converts
  to/from your `HOMEKIT_TEMP_UNIT`.
- Pairing data is stored encrypted on the node and keyed by device — re-pairing is
  only needed if you remove and re-add the accessory.

## Dependencies

- [`aiohomekit`](https://github.com/Jc2k/aiohomekit) (the HAP client used by Home
  Assistant). Installed on the node at runtime; the protocol degrades gracefully if
  it is unavailable.
