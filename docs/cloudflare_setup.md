# Cloudflare setup

Use this optional guide if you want Cloudflare DNS-01 certificate issuance and automatic renewal during [Installation](installation.md). Before choosing this path, check [Tested Vacuums](tested_vacuums.md) to confirm whether your model should start with `zerossl` or `actalis`. In most cases, prefer `zerossl`; use `actalis` mainly for older vacuums or when the tested-vacuum notes point you there. If you would rather provide your own certificate files, see [Custom certificate management](custom_cert_management.md).

Cloudflare is used for DNS-01 validation against your zone so that the stack can request and renew certificates automatically. The ACME CA is configurable. The default and recommended choice for most users is ZeroSSL, and any deployment can switch to `actalis` if an older vacuum trusts that chain more reliably.

DNS-01 validation only needs permission to create temporary TXT records in Cloudflare. It does not make the service reachable from the internet by itself, and local-only setups do not need public inbound access, public port forwarding, or Cloudflare proxying for the stack hostname.

If you choose `acme_server = actalis`, you must also provide `acme_eab_kid` and `acme_eab_hmac_key` from your Actalis ACME account. Generated configs store those in separate secret files instead of embedding them directly in `config.toml`.

Actalis provides the EAB KID and HMAC key from its ACME account setup. Create or sign in to an Actalis ACME account first, then copy both EAB values into the setup wizard or Home Assistant add-on options.

The automated issuance shape differs by ACME CA:

- `zerossl` requests `stack_fqdn` and the same name without `api-` (plus any `tls.extra_names`). Set `tls.wildcard = true` to request `base_domain` plus `*.base_domain` instead; a wildcard certificate from an earlier release keeps that shape until you set `tls.wildcard = false`.
- `actalis` requests only `stack_fqdn`

## Create the Cloudflare Token

Create a user API token in Cloudflare for the zone you will use in `tls.base_domain`.

1. Sign in to the Cloudflare dashboard.
2. Open `My Profile` -> `API Tokens`.
3. Select `Create Token`.
4. Start from the `Edit Zone DNS` template.
5. Give the token a clear name such as `roborock-local-server-example-com`.
6. Scope the token to only the zone you will use for this project.
7. Review the summary and create the token.
8. Copy the token secret immediately. Cloudflare only shows it once.

For this project, keep the token limited to the single zone you are using. Do not use a global API key.

Cloudflare cannot limit a token to one DNS record, so the token can edit every record in that zone. If that matters to you, use a zone that holds nothing else for the stack hostname.

If you also create a public DNS record for the stack hostname because you want remote access, remember that you are making a self-hosted service publicly accessible. The server handles auth and can disable new devices from connecting, but there is still always risk. Keep it local-only if that works for you.

## Related Docs

- [Installation](installation.md)
- [Custom certificate management](custom_cert_management.md)
- [Onboarding](onboarding.md)
