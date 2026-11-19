# Session relay — developer deployment branch

This branch contains only the independent Session backend and its developer documentation. It does not perform vocal separation, score recognition or playback. General users join Session through the existing web player without installing this backend.

Read [operation and protocol limits](docs/SESSION_SERVER.md) and [branch/routing layout](docs/DEPLOYMENT_BRANCHES.md).

Install the pinned `relay/requirements.txt` into an external venv and run `python -m relay.server` from this checkout. Set `MAGIC_RELAY_URL` to the HTTPS relay origin and `MAGIC_PLAYER_URL` to the existing GitHub Pages player. Use a supervised unprivileged service. The public reverse proxy exposes `/api/*` on port 13579 only; the Python backend listens on loopback.

The landing page is deployed separately from the `landing-page` branch and served directly on HTTP/HTTPS default ports. The backend does not need its files or the web player's assets in production. Keep temporary transfers, DNS tokens, operator settings and logs outside this checkout.

`tests/test_relay.py` covers role authorization, clocks, acknowledgement cancellation, authenticated sharing and cleanup. Install pytest into the same external development venv to run it. Integrated desktop/browser tests live on `main`; synchronize reviewed `relay/` source changes with that branch and rerun the multi-client browser tests there before deployment.

Original backend code: GPL-3.0-only. aiohttp and Segno retain upstream licenses; see `THIRD_PARTY_NOTICES.md`.
