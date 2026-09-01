"""HTTP entry point for the interface. The call sheet, with findings attached.

    from services.api import create_app
    app = create_app()

Thin by design: it holds the MCP connection and hands the Gate's own output
through. Detection, ranking and SQL live in `services/gate`.
"""

from services.api.app import Settings, create_app
from services.api.browse import Browser

__all__ = ["Browser", "Settings", "create_app"]
