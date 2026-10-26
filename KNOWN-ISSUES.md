# Known Issues

- New-SMS notifications while the app is closed depend on iOS background scheduling. Delivery can be late or missing; reliable real-time delivery requires push support from the node.
- The modem call-audio path depends on modem firmware exposing usable audio over USB. Some firmware does not provide it; Wi-Fi Calling is used when configured and registered.
- Verification-code detection uses keyword and digit-length heuristics. It can select an unrelated number; users can disable this feature in settings.
- End-to-end Wi-Fi Calling interoperability varies by carrier and modem. Validate registration, incoming calls, and registration refresh on the target network.
