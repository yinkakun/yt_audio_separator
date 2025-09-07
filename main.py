import uvicorn
from app import create_app
from app.config.config import Config

app = create_app()


if __name__ == "__main__":
    server_settings = Config()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=server_settings.server.port,
        reload=server_settings.server.debug,
        log_level="info",
        access_log=True,
    )
