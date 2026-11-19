# DuckDNS updates — operator and developer guide

The optional `relay/duckdns_update.py` utility keeps a DuckDNS IPv4 record aligned with the server's public connection. It runs independently of Session and never restarts the player, relay or proxy. This is server administration, not an end-user installation step. Source and unit templates are maintained on `serverside`, with integration copies on `main`.

## Behavior

- The systemd timer runs shortly after boot and approximately every five minutes, with up to 25 seconds of scheduling slack/jitter.
- A direct, IPv4-only HTTPS request lets DuckDNS detect the current public IPv4. The updater does not set IPv6 or TXT records or clear records.
- Only a valid `OK` response containing a public IPv4 and `UPDATED`/`NOCHANGE` counts as success. Transport failures retry twice with bounded delays; later timer runs retry after an outage.
- The token is loaded as a systemd credential from a root-only file. It is passed to curl through standard input, never in process arguments or environment variables. Raw network errors/responses and tracebacks are not logged.
- A separate dynamic service user has no home access or write access to application files. Only its private state directory is writable. The status file records the last successful response and UTC time, never the token.

DNS caching can delay reconnection after an IP change. Existing Session connections can disconnect; automatic DNS updates do not migrate active connections. The router must retain the forwarding rules and the server's stable private address. The host needs outbound IPv4 HTTPS and DNS. This utility deliberately ignores HTTP proxies so DuckDNS observes the server connection's IP.

## Install on a Linux/systemd server

Requires system Python 3.12/3.13, curl and trusted CA certificates. The updater uses only Python's standard library: it installs no packages globally and does not change the desktop or relay venv.

From a reviewed `serverside` checkout:

```sh
sudo install -d -m 755 /usr/local/lib/magic-duckdns
sudo install -m 644 relay/duckdns_update.py /usr/local/lib/magic-duckdns/
sudo install -m 644 relay/magic-duckdns.service relay/magic-duckdns.timer /etc/systemd/system/
sudo install -d -m 700 /etc/magic-duckdns
sudoedit /etc/magic-duckdns/token
sudoedit /etc/magic-duckdns/config
sudo chmod 600 /etc/magic-duckdns/token /etc/magic-duckdns/config
```

Put only the DuckDNS account token in `token`. In `config`, put `DUCKDNS_DOMAIN=your-subdomain`, omitting `.duckdns.org`. These files must be root-owned and outside every checkout. Do not put a real token in a shell command, repository, chat, screenshot or support log. The service reads the token again on each run, so a privately replaced token takes effect on the next attempt.

```sh
sudo systemd-analyze verify /etc/systemd/system/magic-duckdns.service /etc/systemd/system/magic-duckdns.timer
sudo systemctl daemon-reload
sudo systemctl start magic-duckdns.service
systemctl show magic-duckdns.service -p Result -p ExecMainStatus
sudo systemctl enable --now magic-duckdns.timer
systemctl list-timers magic-duckdns.timer
```

Enable the timer only after a confirmed first update. A missing token skips the service; an invalid token or network problem returns failure. Check the actual result and last-success time, not only whether the timer is enabled.

```sh
sudo cat /var/lib/magic-duckdns/status.json
sudo journalctl -u magic-duckdns.service -n 10 --no-pager
```

Compare the confirmed IPv4 with the hostname's DNS A record and verify normal HTTPS and the Session health endpoint without disabling certificate checks. No production IP change or host reboot is needed just to test an update. To stop automatic updates: `sudo systemctl disable --now magic-duckdns.timer`. Existing DNS records and Session services remain in place.

Tests: run `python -m pytest -q tests/test_duckdns.py` in an external developer venv. They cover rejected/non-public responses, credential-safe process arguments/logging, retries, config injection and preserving the previous success on failure. They never contact DuckDNS or use a real credential.

Protocol reference: [DuckDNS official API specification](https://www.duckdns.org/spec.jsp). This operator addition does not change the desktop version or rewrite existing release assets.
