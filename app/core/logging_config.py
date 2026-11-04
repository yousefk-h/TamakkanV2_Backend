"""Configures loguru's default sink once at import time.

Left unconfigured, loguru's default handler renders any `logger.opt(exception
=True)` call (e.g. `TranscriptPipelineService.run_generation`'s failure log)
with `diagnose=True` - which prints every local variable at every stack
frame, all the way down through Starlette/uvicorn's internals for a
background-task exception. That produces tracebacks hundreds of lines long
for an ordinary, expected failure (a blocked YouTube fetch, a Gemini quota
error), and `diagnose=True` is explicitly called out in loguru's own docs as
unsafe for production - a stack frame can easily contain a token, password,
or API key as a local variable, which this would print in full.
"""

import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, diagnose=False)
