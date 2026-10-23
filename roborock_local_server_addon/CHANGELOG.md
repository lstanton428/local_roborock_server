# Changelog

## 1.2.1-hardened.4

- **Open Web UI** now opens a Roborock panel inside Home Assistant: server health, connected vacuums, certificate expiry, and the new-connections window with **Open** / **Close now** buttons. Each change is logged with the Home Assistant user's name.
- New option `ingress_new_connections_controls` (default on). Turn it off to make the panel read-only, so only the admin dashboard can open or close new connections.
- The panel's **Open admin dashboard** link opens in your default browser from the Home Assistant iOS, macOS and Android apps, and in a new tab in a browser.

## 1.2.1-hardened.3

- **Open Web UI** now opens the admin dashboard on `stack_fqdn` (with `advertised_https_port`) instead of the hostname you opened Home Assistant with. It goes through Home Assistant ingress, so you can also show **Roborock** in the sidebar. The ingress page only answers Home Assistant's ingress proxy and holds nothing but the dashboard link.

## 1.2.1-hardened.2

Fixes found while onboarding a Saros 10. Existing settings and paired vacuums keep working; no re-pairing is needed.

- Typing `retry` or `refresh` in the onboarding tool now reopens the new-connections window if it closed while the tool waited at its prompt. Before, the vacuum was refused with `new_connections_disabled_onboarding`.
- The Cloudflare API token is masked on the add-on configuration page. Your saved token is kept.
- The startup log names the address the internal MQTT broker really listens on (`127.0.0.1`), instead of `0.0.0.0`.
- The add-on repository no longer lists the unused Beta add-on, which pulled the old upstream `1.1.0-rc2` image.

## 1.2.1-hardened.1

Security-hardened build of 1.2.1 from the lstanton428 fork (`hardened` branch). Existing settings, sessions and paired vacuums keep working; no re-pairing is needed.

- The add-on no longer mounts `/ssl` or other add-ons' config folders. Use `tls_mode: cloudflare_acme`, or copy certificates into this add-on's config folder (`/config`).
- ZeroSSL certificates cover only `stack_fqdn` and its token hostname instead of a wildcard (an existing wildcard certificate keeps its shape). Superseded wildcard keys are deleted.
- New connections close 30 minutes after start (`new_connections_window_minutes`); reopen them in the dashboard before logins or onboarding.
- Admin and PIN logins are rate-limited; the dashboard only answers local networks (`admin_allowed_networks`, `trusted_proxies`).
- Login codes, tokens, local keys and MQTT passwords are kept out of logs; secret files are written owner-only.
- Per-client MQTT topic permissions in audit mode (`mqtt_topic_acl`), MQTT handshake timeouts, and request size limits.
- TURN credentials are only given to connected vacuums; plugin downloads and public-key recovery are bounded.

## 1.2.1

- Fixed vacuums losing control about 48 hours after onboarding with `rrcheck access denied` (seen on the Qrevo Edge 2). The server now answers the firmware's periodic `/f/c` access check locally.
- Fixed routines that start cleaning with `do_scenes_app_start` stopping before the vacuum started, because some firmware rejected the clean repeat count.
- Fixed region and timezone mismatches during onboarding for non-US regions: the location endpoint now uses the region's timezone, and European timezones map to the `eu` region.
- Fixed the Roborock app's v4 home requests reaching the Roborock cloud instead of the local API through the MITM redirect.
- Fixed cloud import rejecting every code for some accounts (for example UK accounts on the EU cloud).
- Fixed a crash in guided onboarding when the typed input was not valid UTF-8.
- Existing settings, cloud imports, and recovered keys are retained when updating.

## 1.2.0

- Added onboarding for vacuums that have never been on the Roborock cloud: choose **New vacuum** in the guided CLI or GUI. The server adopts the vacuum from its own onboarding traffic and adds it to the inventory as soon as it registers, with its model, product name, and product id taken from a built-in catalog of Roborock robot vacuums.
- Guided onboarding now shows when the server is calculating the public key, so you know to wait before starting the next pairing cycle.
- Fixed onboarding behind a reverse proxy or SNAT when the HTTP and MQTT client addresses differ.
- Fixed the advertised MQTT port when it differs from the listening port.
- Fixed explicit HTTPS ports for reverse proxies, including `--local-api` on `:443`.
- Deleting a routine in the Roborock app now persists.
- Existing settings, cloud imports, and recovered keys are retained when updating.

## 1.1.0

- Added V2 onboarding support, including automatic public-key recovery and multipart NC registration.
- Removed the obsolete V2 unsupported status from the dashboard and guided onboarding.
- Confirmed working on QRevo Edge 2 Set and Saros 20 Sonic. See the tested-vacuum list for firmware and certificate details.
- Fixed certificate renewal handling when acme.sh reports that renewal is not yet needed.
- Existing settings and recovered keys are retained when updating. The Beta add-on is currently unused; use the stable add-on for this release.

## 1.0.2

- Added external TLS support and basic reverse proxy support.
- Explicitly handle the unsupported `v2` `/region` flow so detection no longer falls through.
- Improved device id matching and routine resuming.

## 1.0.1

- Added support for the iOS app's `/v4/user/homes/{home_id}` home-data route so device lists no longer fall through to the generic catchall response.
- Protected `/v4/user/*` routes with the same Hawk authentication used by existing user API versions.

## 0.0.2-rc8

- Initial Home Assistant add-on manifest using the shared GHCR image.
