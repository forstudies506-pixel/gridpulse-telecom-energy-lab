"""Vercel adapter for the synthetic dashboard health endpoint."""

from dashboard import DashboardHandler


class handler(DashboardHandler):
    """Expose the existing GET /api/health contract to Vercel."""

    pass
