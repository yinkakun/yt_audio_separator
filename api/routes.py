from fastapi import FastAPI

from api.factory import configure_middleware, create_base_app, create_lifespan_manager
from api.handlers import register_error_handlers, register_routes
from config.logger import get_logger

logger = get_logger(__name__)


def create_fastapi_app(config, storage) -> FastAPI:
    """Create and configure the FastAPI application"""

    app = create_base_app(config)

    app.router.lifespan_context = create_lifespan_manager(config, storage)

    configure_middleware(app, config)

    register_error_handlers(app)

    register_routes(app, config, storage)

    return app
