import sys
import time
import uuid

import pytest

from jev_git_graph.credential_cache import KeychainLease


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain integration")
def test_keychain_lease_expires_without_printing_or_persisting_secret():
    service = f"jev-git-graph-test-{uuid.uuid4()}".encode()
    lease = KeychainLease(service=service, account=b"synthetic")
    now = int(time.time())
    try:
        expiry = lease.store("synthetic-test-only", issued_at=now, hours=1)
        assert expiry == now + 3600
        assert lease.read(now=now + 1) == ("synthetic-test-only", expiry)
        assert lease.read(now=expiry) is None
        assert lease.read(now=now + 2) is None
    finally:
        lease.clear()
