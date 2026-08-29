"""Shared Jinja2 templates instance for the API routes."""

from fastapi.templating import Jinja2Templates

from app.core import config
from app.services.backtest.activity_presenter import absolute_time, relative_time

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
templates.env.filters["relative_time"] = relative_time
templates.env.filters["absolute_time"] = absolute_time
