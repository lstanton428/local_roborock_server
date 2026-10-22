# Installation

Start here for a first-time setup. The project supports two installation methods:

- Docker Compose on your own Linux host or VM
- the Home Assistant add-on from this repository

After the stack is running, continue with [Onboarding](onboarding.md) to pair a vacuum.

## Shared Requirements

- A domain name that you own
- A place to run the stack on your LAN
- A second machine for onboarding later. It needs Python 3.11+ and `uv` if you run the onboarding scripts there.
- A network that can host the stack's HTTPS and MQTT TLS ports internally. The defaults are `555` and `8881`.
- A Cloudflare API token with DNS edit access for the zone if you want Cloudflare DNS-01 auto-renew. See [Cloudflare setup](cloudflare_setup.md).
- Sufficient container resources: Allocate at least 1 CPU core and 1 GB of RAM. The server performs cryptographic operations during onboarding, and resource starvation can cause the handshake to silently stall or time out. You can reduce resources after you complete onboarding. If you do not allocate enough resources, just wait a little bit longer when it says "calculating public key".

## Credential Names

The setup uses three different credentials:

- The **admin password** signs in to the local server dashboard at `/admin`.
- The **protocol login email and PIN** are the local login that Home Assistant or the Roborock app use after you repoint them to this server.
- Your **Roborock cloud email and verification code** are used only by the admin dashboard's cloud import flow so the local server can fetch your current homes, rooms, routines, and known vacuums.

After five failed attempts from the same address, the admin sign-in and the protocol PIN login make that address wait before trying again, starting at one second and doubling up to 15 minutes. A successful sign-in resets it, and an address's failures are forgotten a day after its last one. The protocol PIN login also stops accepting guesses from anyone for a while after 60 failures in an hour, so a mistyped PIN in Home Assistant can briefly return `too_many_attempts`.

## Choose Your Certificate Path First

Before you run the setup wizard, check [Tested Vacuums](tested_vacuums.md).

Different vacuums trust different certificate chains. That determines whether you should:

- use `zerossl` with Cloudflare DNS-01 automation
- switch Cloudflare DNS-01 automation to `actalis`
- skip Cloudflare ACME and bring your own certificate files instead

For most users, prefer `zerossl`. Use `actalis` mainly for older vacuums or for models that already have tested-vacuum notes showing better compatibility with that chain.

If your model already has certificate notes on the tested-vacuums page, follow that guidance first. It is easier to choose the right certificate path up front than to reissue certs after onboarding starts.

## Network Setup

1. Pick a hostname for this application. It must be a subdomain of a domain you own, and it **must** start with `api-`.

   For example, if you own `example.com`, use `api-roborock.example.com`. Throughout the docs this is the **stack FQDN**.

   Onboarding also has a hard 32-character limit for the final `host[:port]/` value sent to the vacuum after the `api-` prefix is stripped. Short names are safer:

   - `api-rr.example.com` with the default port becomes `rr.example.com:555/` and fits.
   - `api-roborock-local-server.example.com` with the default port becomes `roborock-local-server.example.com:555/` and is too long.

2. Your network **must** handle its own DNS for the network the vacuum connects to. If the vacuum, phone, or onboarding machine uses an external DNS server like `8.8.8.8`, this will not work.

3. Create a local DNS record pointing your stack FQDN to the LAN IP of the machine running the stack.

   This should be split-horizon or local DNS through your router, Pi-hole, AdGuard Home, Unbound, or similar. Cloudflare DNS-01 certificate issuance does not require public inbound access, public port forwarding, or Cloudflare proxying.

   If you want the stack to work away from your home network, the server does handle auth and lets you disable new devices from connecting. That still makes this a publicly accessible self-hosted service, so only do it if you know what you are doing. Local-only access is always the better option when it fits your workflow.

4. From a client on the same network the vacuum will use, verify the name resolves to the server's LAN IP:

   ```bash
   nslookup api-roborock.example.com
   ```

   For the first setup and onboarding flow, your home network clients should resolve this name to the server's LAN IP. If they resolve to a public IP, make sure your router and firewall setup intentionally support that path before continuing.

   With the current server behavior, the same hostname is advertised for both HTTPS and MQTT/TLS, so you do not need a separate `mqtt-...` hostname unless you have built your own custom client routing around one.

   If a reverse proxy maps public ports to different backend listener ports, see [Reverse Proxy](reverse_proxy.md) before starting the stack.

## Method 1: Docker Compose

### Additional Requirements

- Docker with `docker compose`
- Python
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

### Steps

1. Clone this repository:

   ```bash
   git clone https://github.com/Python-roborock/local_roborock_server
   cd local_roborock_server
   ```

2. Install the project dependencies:

   ```bash
   uv sync
   ```

3. Run the setup wizard:

   ```bash
   uv run roborock-local-server configure
   ```

   The wizard asks for:

   - `stack_fqdn` (must start with `api-`)
   - HTTPS and MQTT TLS ports if you do not want the defaults `555` and `8881`
   - embedded MQTT or your own broker
   - whether to use Cloudflare DNS-01 auto-renew
   - if you chose Cloudflare, the ACME account email and whether to use ZeroSSL or Actalis. In most cases, choose ZeroSSL unless you are targeting an older vacuum.
   - if you chose Actalis, the Actalis EAB KID and EAB HMAC key
   - your admin password
   - your Home Assistant/app login email and 6-digit PIN

   It then writes `config.toml`, generates `admin.password_hash` and `admin.session_secret`, and if you chose Cloudflare it also writes `secrets/cloudflare_token`. If you also chose `acme_server = actalis`, it writes `secrets/acme_eab_kid` and `secrets/acme_eab_hmac_key`.

4. If you chose external MQTT, fill in `broker.host` in `config.toml` before starting the stack. See [Custom MQTT](custom_mqtt.md).

5. If you skipped Cloudflare, put your certificate files in `data/certs/fullchain.pem` and `data/certs/privkey.pem` (relative to the repository root on the host, which maps to `/data/certs/` inside the container). This is the path to use when your vacuum works better with a certificate chain you manage yourself. See [Custom certificate management](custom_cert_management.md).

6. Start the container:

   ```bash
   docker compose up -d --build
   ```

   If you changed `network.https_port` or `network.mqtt_tls_port` in `config.toml`, set matching Docker Compose variables before you start the stack so the published ports stay aligned. For example:

   ```bash
   ROBOROCK_SERVER_HTTPS_PORT=8443
   ROBOROCK_SERVER_MQTT_TLS_PORT=9443
   docker compose up -d --build
   ```

   In PowerShell:

   ```powershell
   $env:ROBOROCK_SERVER_HTTPS_PORT = "8443"
   $env:ROBOROCK_SERVER_MQTT_TLS_PORT = "9443"
   docker compose up -d --build
   ```

   For reverse proxy setups, keep `network.https_port` and `network.mqtt_tls_port` set to the backend listener ports and use `network.advertised_https_port` / `network.advertised_mqtt_tls_port` for the public ports.

### Container Hardening

`compose.yaml` starts the container with `no-new-privileges` and drops every Linux capability except the few the stack uses:

- `SETUID` and `SETGID`: the embedded Mosquitto starts as root and switches to its own user.
- `KILL`: the server stops that Mosquitto process, which runs under a different UID.
- `DAC_OVERRIDE`: root in the container can still read and write `./data`, `./config.toml` and `./secrets` when your host user owns them.
- `NET_BIND_SERVICE`: binding ports below 1024 (`555`, `443`) when Docker's unprivileged-port setting does not apply, for example with `network_mode: host`.

If you copy these settings into your own compose file and the stack fails to start, check `docker compose logs` for `Operation not permitted` or `Permission denied`.

The image still runs as root, so existing `data/` directories keep working without changes. To run it as an unprivileged user instead, first give that user ownership of the mounted files:

```bash
docker compose down
sudo chown -R 1000:1000 data secrets config.toml
```

Then, in `compose.yaml`, add `user: "1000:1000"` and remove the `cap_add` list (keep `cap_drop: [ALL]`). In this mode Mosquitto and coturn run as the same user. coturn logs that it cannot open its SQLite database or `/var/run/turnserver.pid`. You can ignore that, because provided TURN mode sets its user in the config file. With `network_mode: host`, the HTTPS and MQTT ports must be 1024 or higher. Cloudflare ACME renewal has not been tested as a non-root user. If you switch back to root later, `data/` stays usable.

## Method 2: Home Assistant Add-on

Use [Home Assistant](home_assistant.md) as the installation guide if you want to run the stack as a Home Assistant add-on instead of Docker Compose.

## After The Stack Starts

New connections (Home Assistant or app logins, onboarding, and a vacuum's first connection) are allowed for 30 minutes after the server starts, then close on their own. Devices and apps that already connected keep working. To add something later, open **New Connections** in the admin dashboard, tick the box and select **Save** to open another 30-minute window; starting an onboarding session reopens it too. Change the length with `admin.new_connections_window_minutes` (`new_connections_window_minutes` in the add-on), or set it to `0` to leave new connections open until you turn them off.

1. Open the admin dashboard at `https://api-roborock.example.com:555/admin` by default, or `https://api-roborock.example.com:YOUR_HTTPS_PORT/admin` if you chose a custom HTTPS port.

   The dashboard only answers clients on loopback, private LAN ranges and Tailscale's `100.64.0.0/10`; anything else gets HTTP 403. Set `admin.allowed_networks` (`admin_allowed_networks` in the add-on) to change that list; `["0.0.0.0/0", "::/0"]` allows every address, and an empty list is rejected. The Roborock API on the same port is not affected.

   The defaults include every private range, so a vacuum or other IoT device on a separate private VLAN can still reach the dashboard. If your IoT devices use their own subnet, list only your trusted subnets, for example `["192.168.1.0/24", "100.64.0.0/10", "127.0.0.0/8"]`. The check sees the address the server receives: with Docker Desktop, rootless Docker or a userland port proxy every client can appear as the Docker gateway (`172.17.0.1` or similar), and a tunnel or proxy on the same host that does not send `X-Forwarded-For` appears as `127.0.0.1`. In those setups the allowlist cannot tell clients apart.

2. If the page does not load, check the container and DNS before onboarding:

   ```bash
   docker compose ps
   docker compose logs -f roborock-local-server
   nslookup api-roborock.example.com
   ```

3. Import your data from the cloud so things like routines and rooms will work. Enter your Roborock cloud email under cloud import, select **Send code**, then enter the returned code and select **Fetch data**.

4. For any routines that use zones, re-save them so the server stores the zone data correctly. In the Roborock app, open each routine that has zones, open the zone, tap **Edit**, open any **Zone Cleaning** entry, then tap **Save**. Repeat for each zone in the routine.

## Next Steps

- [Onboarding](onboarding.md) for pairing a new vacuum
- [Home Assistant](home_assistant.md) if you want to repoint Home Assistant's Roborock integration to your local stack
- [Mobile App Options](roborock_app.md) if you want to control your vacuum with a mobile app (the official Roborock app or LocalRock)
- [Updating](updating.md) for upgrading an existing install
- [Docs index](index.md) for the rest of the guides
