import uvicorn
from src import create_app
from src.config.config import Config

app = create_app()


if __name__ == "__main__":
    config = Config()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.server.port,
        reload=config.server.debug,
        log_level="info",
        access_log=True,
    )
