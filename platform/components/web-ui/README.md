# web-ui component

Configuration and administration UI for the re-platformed HelloDJ.

- **Stack:** Flask + HTMX + Alpine.js + Tailwind CSS v4 (R14.2)
- **Design:** dark glassmorphism sidebar shell per the modern-web-ui standard
  (R14.1, R14.3), OKLCH palette meeting WCAG AA contrast (R14.4)
- **Auth:** routed by purpose through
  `hellodj_platform_logic.auth_routing.route_auth` — Cognito for
  admin/registration/recovery, Discord OAuth for day-to-day login, first-party
  Tidal OAuth callback forwarded to `tidal-stream` (R8, R9.2, R9.5)
- **Config:** DynamoDB `hellodj-core` via `data_access.CoreTable` (R6.5, R7)
- **Secrets:** AWS Secrets Manager (`hellodj/<stage>/<leaf>`)

## Layout

| File | Responsibility |
|------|----------------|
| `app.py` | Flask app factory, static hashing, health, `login_required` |
| `auth.py` | Auth blueprint (Discord / Cognito / Tidal callback) via `route_auth` |
| `pages.py` | Page + HTMX-partial routes (dashboard, config, guilds) |
| `config_store.py` | Config read/write over `hellodj-core` (`CoreTable`) |
| `secrets_store.py` | Secrets Manager accessor (`hellodj/<stage>/<leaf>`) |
| `templates/` | `base.html` shell, `pages/`, `partials/`, `components/` macros |
| `static/css/app.css` | Tailwind v4 entry (`@import "tailwindcss"` + `@theme`) |
| `static/js/app.js` | Ambient background + toast glue |

## Build

Development (zero-build) uses the Tailwind/HTMX/Alpine CDN (see `base.html`
notes and `static/js/VENDOR.md`). Production compiles CSS and vendors the
client libraries locally:

```bash
npm install
npm run build:css      # static/css/app.build.css (minified)
npm run fetch:vendor   # static/js/{htmx,alpine}.min.js
```

The `Dockerfile` performs both phases; the platform ships the same steps as
Nix derivations (R5) with the build-stage base-image gate enforcing no
Ubuntu/Debian base.

## Run

```bash
gunicorn --bind 0.0.0.0:8080 app:app
```

Environment: `HELLODJ_STAGE`, `HELLODJ_PUBLIC_BASE_URL`, `DISCORD_CLIENT_ID`,
`COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`, `TIDAL_STREAM_URL`, `FLASK_SECRET_KEY`.
