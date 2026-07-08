# HAOS initial test state

This seed Home Assistant config is adapted from ha-mcp's HAOS E2E
`tests/initial_test_state` fixture. It gives the reusable HAOS image a
controlled pre-onboarded config instead of baking whatever state was left by the
first build boot.

The vendored `custom_components/hacs` files are test fixtures from HACS, carried
only so the HAOS seed state matches the proven ha-mcp lane closely.
