# Live HAOS development environment

Run a disposable HAOS instance directly in this owned repository with the
**Component E2E** workflow's opt-in `dev_minutes` mode. It uses the ordinary HAOS
image and embedded ESPHome MCP fixture. This follows
[ha-mcp's dev-environment workflow](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/dev-ha-env.md):
temporary Cloudflare tunnels, encrypted URLs, and testing through MCP.

No test or VM runs on your PC. Ordinary PR E2E jobs never open a tunnel.

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
   Update the component through HACS and restart Core when iterating, or stop the
   run and dispatch the new branch commit. The VM starts on the dispatched commit.

5. Cancel the run when done:

   ```sh
   gh run cancel RUN_ID -R kingpanther13/esphome-mcp
   ```

The holder is capped at 240 minutes, and the whole job at 330 minutes including
image creation. Initial uncached image builds take longer. Tests and regular CI
should finish before another push; the explicitly requested dev holder may stay
up for component updates. Dev mode skips the ordinary E2E suite, so run that suite
separately on the final commit. No production Home Assistant instance is used.
