from pathlib import Path


def api_token() -> str:
    """Load the API token from the local credentials file at startup."""
    return Path("credentials").read_text(encoding="utf-8").strip()
