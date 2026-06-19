"""Shared Jinja2 templates instance for the API routes."""

from fastapi.templating import Jinja2Templates

from app.core import config

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
