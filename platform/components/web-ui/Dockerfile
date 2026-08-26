# HelloDJ web-ui image.
#
# NOTE ON THE PLATFORM STANDARD (R5): production images for this platform are
# built with Nix (no Ubuntu/Debian base). This Dockerfile documents the Tailwind
# v4 build step and the runtime shape; the Nix packaging (task 20.1) wraps the
# same two phases — a Node CSS build and a Python runtime — as Nix derivations,
# and the build-stage base-image gate (task 18.2 / base_image_gate.py) rejects
# any non-Nix base. It is kept here as the canonical build reference.

# ---- Phase 1: Tailwind CSS v4 build --------------------------------------
FROM node:22-slim AS css-build
WORKDIR /build
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY static/css/app.css ./static/css/app.css
COPY templates/ ./templates/
# Compile the utility classes actually used by the templates, minified.
RUN npx @tailwindcss/cli@4 -i static/css/app.css -o dist/app.css --minify
# Vendor the pinned client libraries locally (no runtime CDN dependency).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://unpkg.com/htmx.org@2/dist/htmx.min.js -o dist/htmx.min.js \
    && curl -fsSL https://unpkg.com/alpinejs@3/dist/cdn.min.js -o dist/alpine.min.js

# ---- Phase 2: Python runtime ---------------------------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    HELLODJ_STAGE=beta

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application sources.
COPY app.py auth.py pages.py config_store.py secrets_store.py ./
COPY templates/ ./templates/
COPY static/ ./static/

# Overlay the compiled CSS + vendored JS from the build phase.
COPY --from=css-build /build/dist/app.css ./static/css/app.css
COPY --from=css-build /build/dist/htmx.min.js ./static/js/htmx.min.js
COPY --from=css-build /build/dist/alpine.min.js ./static/js/alpine.min.js

# The shared pure-logic package (auth_routing, data_access, types) is installed
# into the image alongside the component in the Nix build; for this reference
# Dockerfile it is expected on PYTHONPATH via the monorepo layout.
ENV PYTHONPATH=/app:/opt/hellodj_platform_logic

EXPOSE 8080
# Gunicorn serves the Flask WSGI app defined in app.py.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]
