"""`python -m harness_control` — the dev entrypoint.

Production runs uvicorn directly against `harness_control.app:app` (SPEC §5.6);
this exists so a developer can start the server without remembering that string.
"""

import uvicorn

from harness_control.app import create_app
from harness_control.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=8080,
        log_config=None,  # logging_config.py already owns this
    )


if __name__ == "__main__":
    main()
