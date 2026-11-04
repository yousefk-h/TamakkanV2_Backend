"""Keep a developer's real `.env` (LiveKit, SpatialReal, OpenAI credentials) out of the agent tests: the settings loader is
pointed at a file that does not exist, so the tests see only the defaults and the variables they set themselves."""

import os

os.environ.setdefault("TAMAKKAN_ENV_FILE", os.path.join(os.path.dirname(__file__), "no-env-file-for-tests"))
