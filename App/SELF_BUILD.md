# Build Your Own iOS App for Incoming Call Pushes

This setup uses your own Apple Developer team and your own OpenWrt node. The node sends incoming-call VoIP pushes directly to Apple APNs. It does not use another developer's APNs credentials.

## Apple setup

1. Create an explicit App ID with a unique Bundle ID in your Apple Developer account. Enable Push Notifications for that App ID and create provisioning profiles for the build you will install.
2. Create an APNs authentication key for the same team and app topic. Download its `.p8` file once. Record the Team ID and Key ID. Keep the `.p8` file private.
3. Build and install the app with the same Bundle ID and your Team ID. For a signed development build on a connected iPhone, copy `Local.mk.example` to `Local.mk`, set `TEAM_ID`, `BUNDLE_ID`, `DEBUG_SIGNING = YES` and `DEBUG_DESTINATION` there, then run `make debug`. If Xcode selects a wildcard profile, set `DEBUG_PROFILE` to the explicit profile name in `Local.mk`. The selected profile must include Push Notifications. For a signed Release build, set `TEAM_ID` and `BUNDLE_ID` in `Local.mk` and run `make release`. `Local.mk` and `.p8` files are ignored by Git. Do not commit your key.

The app registers a VoIP device token when it starts. Development builds use the APNs sandbox. Release builds use production. The Bundle ID passed to `make` also sets the background SMS task identifier. A provisioning profile without the Push Notifications capability cannot receive VoIP pushes.

## OpenWrt setup

1. Install the node call service, push helper, LuCI app, and `python3-cryptography`. The node also needs `curl` with HTTP/2 support and outbound TLS access to APNs.
2. Open **Services → SIM Anywhere → Settings → Incoming Call Push (APNs)** in LuCI. Enter the same Team ID and Bundle ID as the installed app, plus the APNs Key ID. Save and apply.
3. Upload the `.p8` file in that section. LuCI does not display or return the key. The node installs it as `/etc/sim-anywhere-apns-key.p8` with mode `0600`. Use LuCI over HTTPS or a trusted local network.
4. Open the updated app and connect it to the node. It registers its VoIP token, Bundle ID, build signing Team ID, push environment, device name and model, OS version, and app version and build. These identifiers are app-reported configuration values; they are not signature attestation.
5. Open **Services → SIM Anywhere → Status → Incoming call phones** in LuCI. Check the device row for missing metadata or a Team ID / Bundle ID mismatch. When no phone is selected, the first complete registration becomes the recipient. Use **Set as recipient** to select a matching phone. Other phones stay registered; reconnecting does not replace the selection. A selection change applies to the next incoming call, including foreground WebSocket notifications and background VoIP pushes.
6. Test with a real incoming call while the phone is on the same reachable network as the node. The status starts at **Configuration matches, awaiting a real call**. After a real call, LuCI shows APNs acceptance or an HTTP failure. An expired token stays visible and cannot be selected or pushed until the phone registers a new token. No synthetic VoIP validation call is sent.

The APNs key must belong to the team that signed the installed app. A key from a different team cannot send to that app's VoIP topic. The push wakes the app; the existing WebSocket carries call signaling and audio. For calls outside the local network, provide a secure route from the phone back to the node. APNs delivery does not guarantee that every call will ring.

The phone tab shows VoIP in gray when APNs is not configured, orange when the selected-device push path is not verified, and blue when the registration matches the configuration (pending) and after APNs accepts a real incoming-call push for the current selected device and configuration. APNs acceptance alone does not prove that the phone rang. Old registrations without app metadata require the updated app to reconnect and an explicit recipient selection in LuCI. While all registrations use the old format, foreground calls keep the previous behavior. If the selected device unregisters, other registered phones are not selected automatically.
