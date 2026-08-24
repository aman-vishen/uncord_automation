# Quality Verifier Responsive UI Fix — v13.2

This update fixes the 8-router verification area on smaller production monitors.

Changes:
- Main window now fits the detected screen instead of forcing 1500x940.
- Router section now has a vertical scrollbar.
- Routers 01–04 and 05–08 automatically stack on narrower screens.
- Wide displays continue to use the two-panel side-by-side layout.
- 1366x768 layout was smoke-tested under a virtual display.
- 1920x1080 layout was smoke-tested under a virtual display.

No router commands, MES schema, server API, or verification logic were changed.
