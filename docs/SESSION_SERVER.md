# Session relay — operator and developer guide

Ordinary players only need the web player's **Session** menu. They do not install this server. PC leaders use the same web player as tablet leaders; the native desktop button opens it after pausing local playback.

## What the server does

- Creates ephemeral rooms, eight-character alphanumeric codes and QR invitations.
- Exchanges presence, readiness, UTC-based clock probes and future transport commands over WebSocket.
- Enforces leader authority, bounds requests and cancels reservations missing acknowledgements.
- Transfers a leader's explicitly shared `.magic` as opaque bytes. It does **not** separate vocals, recognize scores, decode music, change pitch or play audio.

All participants prepare playback locally. A separate opaque leader token controls the room; the invite code grants member access only. Tokens stay in page memory and authorization headers/initial WebSocket authentication, never invitation URLs or logs. Codes use SHA-256 over the observed leader IP, current time and cryptographic randomness; IP is not recoverable from the displayed code. Treat invitations as private to the intended group.

Current limits: 16 rooms, 16 participants per room including leader, 4-hour room lifetime, 1 GiB per shared file and 2 GiB aggregate file storage including reserved uploads. Creation/join/download and WebSocket message rates are bounded. Leader disconnection closes the room. Member disconnection or readiness loss halts playback. Room close, expiry and process restart remove server copies; previously downloaded copies cannot be recalled.

## Install separately from the desktop

Use a Linux host with Python 3.12/3.13, a trusted HTTPS hostname and enough temporary storage. Keep this checkout separate from private songs. Example commands, run from the reviewed repository:

```sh
RELAY_BASE="$HOME/.magic-relay"
mkdir -p "$RELAY_BASE"
python3 -m venv "$RELAY_BASE/.venv"
"$RELAY_BASE/.venv/bin/python" -m pip install -r relay/requirements.txt
MAGIC_RELAY_URL=https://practice.example.org:13579 \
MAGIC_PLAYER_URL=https://jeongwoo-gobroad.github.io/Magic-Instrumental/ \
  "$RELAY_BASE/.venv/bin/python" -m relay.server
```

The venv is outside the project. Never install these Python packages globally. The relay binds **127.0.0.1:13580** by default; `MAGIC_RELAY_PORT` changes only that private backend port. `MAGIC_RELAY_URL` is the externally reachable HTTPS origin. `MAGIC_PLAYER_URL` optionally chooses a separate static player URL; production sets this to the existing GitHub Pages player. Local preview helpers can serve integrated static files, but the production proxy exposes only `/api/*`. `MAGIC_ALLOWED_ORIGINS` is a comma-separated list of additional exact web origins; the relay's own origin and this project's GitHub Pages origin are allowed by default.

Build the player with the desktop/developer venv using `tools/build_web.py` before deployment. The production relay needs `relay/`, `LICENSE` and `THIRD_PARTY_NOTICES.md`; it runs from the dedicated `serverside` checkout. The independently deployed landing page is served by Caddy from the `landing-page` branch. See `DEPLOYMENT_BRANCHES.md`. Keep the full GPL corresponding source available alongside the distribution. End-user installers never install the relay or a reverse proxy.

Use a supervised service running as an unprivileged account. Give it a private temporary directory, restart-on-failure, read-only application files, no access to unrelated services and suitable memory/storage limits. Do not expose the backend port directly. Disable access logs containing room paths; never log authorization headers or WebSocket payloads. On upgrades, announce interruption, stop the old process, replace only reviewed app files, then restart; rooms are intentionally not persisted.

## HTTPS on port 13579

HTTPS is required by the current browser AudioWorklet path and to avoid HTTPS-to-HTTP mixed-content blocking. TLS termination can stay separate: browser **HTTPS/WSS :13579 → reverse proxy → HTTP loopback :13580**. A trusted certificate can use port 13579 for playback, while HTTP-01 domain validation uses public TCP 80. Example Caddy configuration:

```caddyfile
{
    auto_https disable_redirects
}
https://practice.example.org:13579 {
    tls {
        issuer acme {
            disable_tlsalpn_challenge
        }
    }
    @api path /api/*
    handle @api {
        reverse_proxy 127.0.0.1:13580
    }
    handle {
        respond "Not found" 404
    }
    header {
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
    }
}
```

Forward TCP 80 and 13579 to the server. A domain provider's CAA restrictions can prohibit certificate issuance even when forwarding works. Use a hostname you can validate and keep its DNS address updated if the public IP changes. A DDNS hostname does not automatically update itself merely because it was registered: configure the provider's updater with a private token outside the repository, or update it manually. Never publish that token. Certificate renewal is automatic once DNS and forwarding remain correct. Do not instruct users to ignore certificate warnings or install a private root certificate.

References: [MDN mixed content](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Mixed_content), [AudioWorklet secure context](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet), [Let's Encrypt challenges](https://letsencrypt.org/docs/challenge-types/), [CAA restrictions](https://letsencrypt.org/docs/caa/), [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

## Timing and practical limits

Synchronize the host's system clock with NTP before service startup. The relay takes a UTC epoch and advances it monotonically during the process lifetime; device wall-clock errors do not become playback offsets. Browser probes estimate clock offset using low-RTT samples. The slowest participant's observed delay determines a 1–5 second reservation lead. Every ready participant must acknowledge receipt. Delay above the supported threshold, missing acknowledgements and foreground/audio loss halt rather than silently starting late.

This coordinates prepared playback. It is not low-latency remote microphone jamming and does not stream members' instruments. Network timing tests do not measure actual speaker output. Bluetooth/output-device latency and independent audio-clock drift remain uncalibrated; validate the intended physical equipment. Prefer wired audio for shared-room practice.

## Verify

Use the external developer venv:

```sh
python -m pytest -q tests/test_relay.py
RUN_WEB_TESTS=1 python -m pytest -q tests/test_session_player.py
```

The tests use authored studies and isolated local servers. They cover unauthorized member commands, clock probes, missing acknowledgements, authenticated file/QR transfer and cleanup, two-browser scheduled transport, different GP parts, counted loops and readiness loss. CI browser engines are not physical iPad/Android certification. The public health endpoint `/api/health` exposes only service/protocol readiness; it is not a proof of audio synchronization.
