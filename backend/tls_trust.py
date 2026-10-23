"""Which certificate authorities outbound HTTPS trusts: certifi's bundle, everywhere.

The broker SDK (requests) already used certifi, but the raw-REST order path
(urllib), aiohttp and the earnings calendar used macOS's /etc/ssl/cert.pem. In
October 2026 eight intermediates under `emSign ECC Root CA - G3` had their keys
compromised (PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2); certifi 2026.10.19 drops
that root, while the macOS file still carried it. One bundle, updated with
`pip`, means a revoked root leaves every path at once.
"""
import os
import ssl

import certifi

CA_FILE = certifi.where()


def context() -> ssl.SSLContext:
    """A verifying client context (hostname checked) on certifi's bundle."""
    return ssl.create_default_context(cafile=CA_FILE)


def use_certifi_by_default() -> None:
    """Make contexts created without an explicit cafile (aiohttp, websockets)
    use certifi too. Must run before those libraries build their defaults; an
    SSL_CERT_FILE already set by the operator is left alone."""
    os.environ.setdefault("SSL_CERT_FILE", CA_FILE)
