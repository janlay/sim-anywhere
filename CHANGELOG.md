# Changelog

## 1.6

### Features

- The status screen shows the link uptime as hh:mm:ss and counts it up every second.
- Tap the status card to switch between the SIM alias and the masked phone number.
- The phone tab shows Wi-Fi Calling and VoIP on one row. Blue means normal; VoIP is blue once APNs is configured.

### Other

- The node announces its address only on the LAN firewall zone interface, so a virtual address is no longer announced.

## 1.5

### Features

- Incoming calls ring in the background through APNs VoIP push. Each build must use its own Push Notifications signing setup; see [App/SELF_BUILD.md](App/SELF_BUILD.md).
- SMS and call records are scoped to each SIM. The SIM card menu shows masked numbers.
- Each SIM can have an alias. The status screen shows the network mode (5G, LTE, 3G, 2G).
- New app icon.

### Fixes

- Unsupported Wi-Fi Calling registration is no longer attempted, and its state is reported.
- Modem status collection is restored.
- A missing AT device no longer becomes a regular file after a USB disconnect.

## 1.1

### Features

- The status screen shows the SIM phone number in masked form.

### Other

- Added iOS build and distribution workflows (`Makefile`, `Scripts/`).

## 1.0

- Initial release.
