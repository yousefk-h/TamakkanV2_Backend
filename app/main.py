"""Entry point the Dockerfile runs: `python -m app.main`.

For local dev, prefer `uvicorn app.core.application:app --reload` instead -
autoreload needs an import string, which `uvicorn.run(app)` with an app object
doesn't support.
"""

import uvicorn

from app.core.application import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4652)
