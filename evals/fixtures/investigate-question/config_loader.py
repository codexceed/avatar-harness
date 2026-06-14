import os


def load_config():
    """Read runtime settings from environment variables, with defaults."""
    return {
        "host": os.environ.get("APP_HOST", "localhost"),
        "port": int(os.environ.get("APP_PORT", "8080")),
        "debug": os.environ.get("APP_DEBUG") == "1",
    }
