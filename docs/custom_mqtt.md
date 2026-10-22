# Custom MQTT

Use this if you want the stack to connect to your own MQTT broker instead of the embedded Mosquitto instance described in [Installation](installation.md).

## Required Config

Set the broker section in `config.toml` like this before you start the stack:

```toml
[broker]
mode = "external"
host = "your-broker-hostname-or-ip"
port = 1883
enable_topic_bridge = true
```

The setup wizard will write `mode = "external"` for you if you choose an external broker, but you still need to fill in `broker.host` before starting.

## Related Docs

- [Installation](installation.md)
- [Onboarding](onboarding.md)
- [Docs index](index.md)

## Topic permissions

The broker behind the proxy accepts anonymous local connections, so the TLS proxy decides what each client may do after it signs in. Each connection is limited to its own topics:

- Home Assistant and app logins: `rr/m/i/<rriot.u>/<mqtt username>/<duid>` and `rr/m/o/...` with the same username.
- Vacuums: `rr/d/i/<did>/<mqtt username>` and `rr/d/o/...` with the same did and username.

`#` and subscriptions that span other usernames or devices fall outside these limits. `broker.topic_acl` (`mqtt_topic_acl` in the add-on) controls what happens:

- `audit` (default): the packet is forwarded and `mqtt_server.log` records `topic ACL would block ...` with the client's identity.
- `enforce`: the client is disconnected before the packet reaches the broker.
- `off`: no check.

Run with `audit` for a while and search the log for `topic ACL`. If nothing shows up for your vacuums, Home Assistant and apps, switch to `enforce`. Please report any `would block` lines from normal use, with the model and client, so the limits can be corrected before `enforce` becomes the default.
