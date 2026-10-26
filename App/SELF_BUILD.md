# Build Your Own iOS App for Incoming Call Pushes

This setup uses your own Apple Developer team and your own OpenWrt node. The node sends incoming-call VoIP pushes directly to Apple APNs. It does not use another developer's APNs credentials.

## Apple setup

1. Create an explicit App ID with a unique Bundle ID in your Apple Developer account. Enable Push Notifications for that App ID and create provisioning profiles for the build you will install.
2. Create an APNs authentication key for the same team and app topic. Download its `.p8` file once. Record the Team ID and Key ID. Keep the `.p8` file private.
3. Build and install the app with the same Bundle ID and your Team ID. For a signed development build on a connected iPhone, use `make debug TEAM_ID=YOUR_TEAM_ID BUNDLE_ID=your.unique.bundle.id DEBUG_SIGNING=YES DEBUG_DESTINATION='platform=iOS,id=YOUR_DEVICE_ID'`. For a signed Release build, use `make release TEAM_ID=YOUR_TEAM_ID BUNDLE_ID=your.unique.bundle.id`. Do not commit your key.

The app registers a VoIP device token when it starts. Development builds use the APNs sandbox. Release builds use production. The Bundle ID passed to `make` also sets the background SMS task identifier. A provisioning profile without the Push Notifications capability cannot receive VoIP pushes.

## OpenWrt setup

1. Install the node call service, push helper, LuCI app, and `python3-cryptography`. The node also needs `curl` with HTTP/2 support and outbound TLS access to APNs.
2. Open **Services → SIM Anywhere → Settings → Incoming Call Push (APNs)** in LuCI. Enter the same Team ID and Bundle ID as the installed app, plus the APNs Key ID. Save and apply.
3. Upload the `.p8` file in that section. LuCI does not display or return the key. The node installs it as `/etc/sim-anywhere-apns-key.p8` with mode `0600`. Use LuCI over HTTPS or a trusted local network.
4. Open the app and connect it to the node so it can register its VoIP token. Test with a real incoming call while the phone is on the same reachable network as the node.

The APNs key must belong to the team that signed the installed app. A key from a different team cannot send to that app's VoIP topic. The push wakes the app; the existing WebSocket carries call signaling and audio. For calls outside the local network, provide a secure route from the phone back to the node. APNs delivery does not guarantee that every call will ring.

The phone tab shows **VoIP not configured** when the node lacks identifiers or a key, **VoIP not active** when validation or device registration is incomplete, and **VoIP active** only after APNs accepts a real incoming-call push for this device and the current configuration. APNs acceptance alone does not prove that the phone rang.
