"""Shared Jinja2 environment for AlertAgent's outbound HTML emails.

A plain ``Environment`` rather than FastAPI's ``Jinja2Templates`` (see
``app.api.templating``) since these render standalone email bodies with no
``request`` object. Autoescaping is on for all ``.html`` templates; pre-built
markup (e.g. the inline SVG chart) is passed through explicitly with the
``safe`` filter at the call site.
"""

from typing import Any, Callable, cast

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import ALERT_TEMPLATES_DIR

email_templates = Environment(
    loader=FileSystemLoader(str(ALERT_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def get_macro(template_name: str, macro_name: str) -> Callable[..., str]:
    """Return a template's macro as a directly-callable ``**kwargs`` function.

    ``Template.module`` exposes macros via dynamic attribute access, which
    static type checkers can't see — this centralises the one
    ``getattr``/cast needed to call a macro outside of a ``{% include %}``.
    """
    module: Any = email_templates.get_template(template_name).module
    return cast(Callable[..., str], getattr(module, macro_name))
