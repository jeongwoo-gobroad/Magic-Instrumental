# Deployment branches — developer information

This information belongs in developer documentation. Beginner pages describe installing, opening files and joining Session; they do not explain repository branches, SSH or Raspberry Pi administration.

| Branch | Purpose | Root contents |
|---|---|---|
| `main` | Integrated desktop source and regression tests | Desktop app, integration copies of portable/server sources, developer docs |
| `mobile-player` | GitHub Pages playback app | Built `index.html`, service worker/icons, reviewed browser sources/licenses |
| `landing-page` | Public introduction/install page | `index.html`, GPL license, developer README |
| `serverside` | Raspberry Pi/Linux Session relay | `relay/`, relay tests/requirements, licenses, developer README/docs |

The deployment branches contain reviewed subsets so the landing and relay can be deployed independently. Integrated copies stay on `main` for regression testing and source provenance, as with `mobile-player`. When changing one, update and verify both its integration copy and the corresponding branch. Do not blindly merge the full desktop tree into a deployment branch. Never include operator SSH credentials, DNS tokens, TLS keys, user media or logs.

## Public routing

- `http://music-helper.duckdns.org/` and `https://music-helper.duckdns.org/`: beginner landing page.
- The Session button and invitations open the existing GitHub Pages player.
- `https://music-helper.duckdns.org:13579/api/…` and its secure WebSocket: Session data exchange only.
- The earlier introduction hostname can redirect to the new HTTPS landing address.

The public address is not an availability guarantee. DuckDNS resolves a hostname to the server's IP; it does not carry the application's file/audio traffic. DDNS update credentials remain outside all branches. See [server operation](SESSION_SERVER.md).

## Deployment separation

Check out or download each branch into a distinct external deployment directory. Run the relay from the `serverside` checkout with an external venv. Set `MAGIC_PLAYER_URL` to the GitHub Pages player; the production proxy exposes only `/api/*` on 13579. Static-serving helpers in the integrated relay source exist for local preview/testing, not public production routing.

Serve `landing-page/index.html` from a separate read-only web root such as `/srv/magic-landing`, using Caddy on ports 80/443. Do not proxy the public landing page through Python. The backend therefore requires no copy of the landing page or browser assets to operate. Example routing, using a hostname controlled by the operator:

```caddyfile
http://practice.example.org {
    root * /srv/magic-landing
    file_server
}
https://practice.example.org {
    root * /srv/magic-landing
    file_server
}
https://practice.example.org:13579 {
    @api path /api/*
    handle @api {
        reverse_proxy 127.0.0.1:13580
    }
    handle {
        respond "Not found" 404
    }
}
```

Keep HTTP-01 certificate validation reachable on public port 80 and permit 443/13579 as intended. Verify TLS without bypassing certificate checks. Validate the landing page's direct release ZIP link before publishing it, and compare downloaded bytes with the release checksum. Preserve the current working deployment until the replacement passes checks.

The landing footer's author name and CV link are user-requested public attribution. Keep the direct GitHub third-party-notices link there. Other personal/operator information must remain private.
