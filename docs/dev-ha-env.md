# Live HAOS development environment

Run a disposable HAOS instance directly in this owned repository with the
**Component E2E** workflow's opt-in `dev_minutes` mode. It uses the ordinary HAOS
image and embedded ESPHome MCP fixture. This follows
[ha-mcp's dev-environment workflow](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/dev-ha-env.md):
temporary Cloudflare tunnels, encrypted URLs, and testing through MCP.

No test or VM runs on your PC. Ordinary PR E2E jobs never open a tunnel.

Every PR runs the full HAOS suite twice: on the minimum supported Core and on
the current beta-channel Core resolved from Home Assistant's official
`beta.json` at run time. Both lanes must pass; failures are not ignored. The
beta lane still runs when that channel points to a stable release, so an older
minimum-version pin cannot hide compatibility failures. Each lane has its own
VM and diagnostics artifact. Both reuse the base-image cache, with the beta
lane upgrading its booted VM in place before testing.

1. Generate a throwaway RSA key locally (keep the private key private):

   ```sh
   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out devenv-key.pem
   ```

2. Dispatch your branch, supplying the base64-encoded PEM public key:

   ```sh
   gh workflow run e2e-component.yml -R kingpanther13/esphome-mcp --ref YOUR_BRANCH \
     -f dev_minutes=120 \
     -f public_key="$(openssl pkey -in devenv-key.pem -pubout | openssl base64 -A)"
   ```

3. Wait for **Keep running**, then download and decrypt the URL artifact:

   ```sh
   gh run download RUN_ID -R kingpanther13/esphome-mcp -n dev-ha-env-urls
   openssl pkeyutl -decrypt -inkey devenv-key.pem -pkeyopt rsa_padding_mode:oaep \
     -in dev-ha-env-urls.enc -out dev-ha-env-urls.txt
   ```

   `HA:` is the UI (test login `mcp` / `mcp`); `MCP:` is the embedded ESPHome MCP
   endpoint. Anyone with those URLs has access to the disposable instance. Keep
   them private and do not put personal data in this environment. The URLs are
   masked in logs and published only encrypted to your key. A restarted tunnel
   republishes encrypted URLs in the `DEVENV_URLS` log line.

4. Connect an MCP client or HTTP shim and exercise `tools/list` and `tools/call`.
   For native integration, discover `esp_native_tools`, invoke `esp_native_call`,
   read back any fixture edits, and confirm the existing HA/file/stream tools.
   The VM starts on the dispatched commit. Keep it running while iterating:
   update Core through Supervisor, apps through their update controls, and this
   component through Marketplace (`2026.11+`) or HACS (`2026.10`), then restart
   Core when required. For an unreleased branch, select that branch in the
   store's download dialog and verify the installed files before testing.
   These updates do not require rebuilding the image or replacing the VM.

   To use the current beta, pass `-f core_version=beta`. To test a particular
   Core version, pass `-f core_version=2026.11.0b4` when
   dispatching. The fixture upgrades Core in the booted VM and verifies the
   running version before loading either MCP integration. This works for both
   dev mode and the ordinary E2E suite and leaves the cached base image's
   version selection unchanged. Omit it to test the minimum supported Core.

5. Cancel the run when done:

   ```sh
   gh run cancel RUN_ID -R kingpanther13/esphome-mcp
   ```

The holder is capped at 240 minutes, and the whole job at 330 minutes including
image creation. Initial uncached image builds take longer. Tests and regular CI
should finish before another push; the explicitly requested dev holder may stay
up for component updates. Dev mode skips the ordinary E2E suite, so run that suite
separately on the final commit. No production Home Assistant instance is used.
