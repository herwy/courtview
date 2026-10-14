"""CourtView application package (Padelmates API proxy and dashboard backend).

create_app() builds the Flask app. Every cross-module reference in the package is qualified
(`config.DB_PATH`, `security._gate()`), so tests and tools can patch a name in the module that owns it.
Layers, lowest first: clubs, config, upstream, privacy, db, jobs, accesslog, security, payments,
refresh, routes_*, startup. A module imports only lower layers, always as `from . import module`.
"""
from flask import Flask

from . import (accesslog, routes_archive, routes_availability, routes_club, routes_core, routes_payments,
               security, startup)

_ROUTE_MODULES = (routes_core, routes_availability, routes_club, routes_payments, routes_archive)

# Registration order of the original single-file app (endpoint = view function name)
_ROUTE_ORDER = (
    "index", "static_asset", "api_heatmap", "api_court_popularity", "api_month", "api_availability_fresh",
    "api_membership_members", "api_club_info", "api_booked_hours", "api_activity_summary",
    "api_day_activities", "api_revenue_summary", "api_payment_history", "api_payment_leaderboard",
    "api_archive_snapshots", "api_archive_heatmap", "api_archive_club_info", "api_archive_financial",
    "api_coach_stats", "api_health", "api_clubs", "proxy", "access_page", "access_js", "dashboard_css",
    "access_data",
)


def create_app() -> Flask:
    # static_folder=None: Flask's built-in /static route would bypass the token gate.
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MiB (Caddy already caps requests at 1 MB)
    app.url_map.merge_slashes = False

    # Hook order matters: before_request runs in registration order, after_request in reverse.
    app.before_request(security._allow_only_get_head)
    app.before_request(security._block_archived_club)

    specs = {}
    for module in _ROUTE_MODULES:
        for rule, view, options in module.ROUTES:
            specs[view.__name__] = (rule, view, options)
    for name in _ROUTE_ORDER:
        rule, view, options = specs.pop(name)
        app.add_url_rule(rule, view_func=view, **options)
    if specs:
        raise RuntimeError(f"routes missing from _ROUTE_ORDER: {sorted(specs)}")

    app.after_request(security._security_headers)
    app.after_request(accesslog.log_req)
    app.register_error_handler(404, security._not_found)
    return app
