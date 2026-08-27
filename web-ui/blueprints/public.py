"""Public-facing routes for the HelloDJ SaaS platform.

Serves the marketing landing page at the site root. This blueprint is intended
for unauthenticated visitors browsing pricing, features, and platform info.

Routes:
- GET /  — Landing page with pricing, feature comparison, and CTAs
"""

from __future__ import annotations

import logging

from flask import Blueprint, render_template

log = logging.getLogger(__name__)

public_bp = Blueprint(
    "public",
    __name__,
    url_prefix="",
    template_folder="../templates",
)


@public_bp.route("/landing")
def landing():
    """Render the public landing/marketing page.

    Displays platform description, pricing cards, feature comparison matrix,
    showcase sections, and call-to-action buttons for login/trial/subscribe.
    """
    return render_template("pages/landing.html")
