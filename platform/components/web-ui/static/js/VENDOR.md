# Vendored client libraries

`htmx.min.js` and `alpine.min.js` are **fetched at image-build time** by the
Dockerfile (pinned versions), not committed to the repo. This keeps the
component free of vendored blobs while still serving the libraries locally in
production (no third-party CDN dependency at runtime).

Pinned versions (see `Dockerfile` and `package.json`):

- HTMX 2.x  → `static/js/htmx.min.js`
- Alpine.js 3.x → `static/js/alpine.min.js`

## Development (zero-build) path

For local iteration without a build step, `base.html` can be pointed at the
CDN instead (per the modern-web-ui standard):

```html
<script defer src="https://unpkg.com/htmx.org@2"></script>
<script defer src="https://unpkg.com/alpinejs@3"></script>
```

The production images always serve the locally vendored, version-pinned files.
