"""Test package.

Environment is configured here, before any test imports ``app``, because
``app.py`` reads its settings at import time and starts a background janitor.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TEST_MEDIA_DIR = tempfile.mkdtemp(prefix="ydl-tests-")

os.environ["YTDL_MEDIA_DIR"] = TEST_MEDIA_DIR
os.environ["YTDL_SWEEP_ORPHANS"] = "0"
os.environ["YTDL_RATE_LIMIT_JOBS"] = "0"
os.environ["YTDL_RATE_LIMIT_PREVIEWS"] = "0"
os.environ["YTDL_RATE_LIMIT_GLOBAL"] = "0"
os.environ["YTDL_TRUSTED_PROXY_HOPS"] = "1"
os.environ["YTDL_REQUIRE_AUTH"] = "0"
os.environ["YTDL_GENERIC_IMPERSONATE"] = "0"
os.environ.pop("YTDL_ACCESS_TOKEN", None)
os.environ.pop("YTDL_PROXY", None)
