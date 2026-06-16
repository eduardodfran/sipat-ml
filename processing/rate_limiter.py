import hashlib
import os

from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request):
    auth = request.headers.get("authorization", "")
    if auth:
        return hashlib.sha256(auth.encode()).hexdigest()[:16]
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)

UPLOAD_LIMIT = os.getenv("RATE_LIMIT_UPLOAD", "10/hour")
PROCESS_LIMIT = os.getenv("RATE_LIMIT_PROCESS", "5/hour")
READ_LIMIT = os.getenv("RATE_LIMIT_READ", "60/minute")
DELETE_LIMIT = os.getenv("RATE_LIMIT_DELETE", "10/hour")
HEALTH_LIMIT = os.getenv("RATE_LIMIT_HEALTH", "60/minute")
