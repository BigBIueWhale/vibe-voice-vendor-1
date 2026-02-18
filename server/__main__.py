import uvicorn

from server.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "server.app:create_app",
        factory=True,
        host=settings.server_host,
        port=settings.server_port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
