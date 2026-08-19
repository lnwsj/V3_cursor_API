from __future__ import annotations

import os
import tempfile


# Both FastAPI modules create their data directories during import.  Keep test
# imports isolated from /var/lib and from any developer data directory.
os.environ.setdefault("WORKER_DATA_DIR", tempfile.mkdtemp(prefix="v3-worker-tests-"))
os.environ.setdefault("GATEWAY_DATA_DIR", tempfile.mkdtemp(prefix="v3-gateway-tests-"))
os.environ.setdefault("CUTDEE_INTERNAL_TOKEN", "test-internal-token")
os.environ.setdefault("CUTDEE_API_KEYS", "test-api-key")
