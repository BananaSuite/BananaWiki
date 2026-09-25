"""WSGI entry point for the BananaWiki managed hosting portal."""

from .app import create_hosting_app
from banana_ops.gate import MaintenanceGate

app = create_hosting_app()
app.wsgi_app = MaintenanceGate(app.wsgi_app)
