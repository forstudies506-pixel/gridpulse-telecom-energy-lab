"""Vercel adapter for the synthetic simulation endpoint."""

from dashboard import DashboardHandler


class handler(DashboardHandler):
    """Expose the existing POST /api/simulation contract to Vercel."""

    pass
