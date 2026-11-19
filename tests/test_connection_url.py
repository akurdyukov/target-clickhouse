"""Unit tests for building the connection URL from separate settings (no database).

Each URL is also passed through the driver's own URL parsing, to check that the
driver ends up with the intended options rather than just a well-formed string.
"""

from __future__ import annotations

import pytest
from clickhouse_driver import Client
from clickhouse_sqlalchemy.drivers.http.base import ClickHouseDialect_http
from sqlalchemy.engine import make_url

from target_clickhouse.connectors import ClickhouseConnector

BASE_CONFIG = {
    "driver": "http",
    "username": "user",
    "password": "secret",
    "host": "ch.example.com",
    "port": 8443,
    "database": "analytics",
    "secure": False,
    "verify": True,
}


def _url(**overrides) -> str:
    config = {**BASE_CONFIG, **overrides}
    return ClickhouseConnector(config=config).get_sqlalchemy_url(config)


def _http_connect_args(url: str) -> tuple[str, dict]:
    """Return (server URL, transport kwargs) as the http driver would see them."""
    (server_url, *_), kwargs = ClickHouseDialect_http().create_connect_args(
        make_url(url),
    )
    return server_url, kwargs


def test_sqlalchemy_url_takes_precedence():
    """An explicit sqlalchemy_url is used as-is, ignoring separate settings."""
    explicit = "clickhouse+native://other:pw@otherhost:9000/otherdb"
    assert _url(sqlalchemy_url=explicit) == explicit


def test_http_plain():
    """Plain http: credentials, host, port and database all reach the driver."""
    url = _url()
    parsed = make_url(url)
    assert parsed.drivername == "clickhouse+http"
    assert (parsed.username, parsed.password) == ("user", "secret")
    assert (parsed.host, parsed.port, parsed.database) == (
        "ch.example.com",
        8443,
        "analytics",
    )
    server_url, _ = _http_connect_args(url)
    assert server_url.startswith("http://ch.example.com:8443")


def test_http_secure_verifies_certificates_by_default():
    """secure=True uses https and keeps certificate verification on."""
    server_url, kwargs = _http_connect_args(_url(secure=True))
    assert server_url.startswith("https://")
    # requests treats a *string* verify value as a CA bundle path, so the
    # transport must receive the boolean True (or no verify option at all).
    assert kwargs.get("verify", True) is True


def test_http_secure_without_verification():
    """secure=True, verify=False turns certificate verification off."""
    server_url, kwargs = _http_connect_args(_url(secure=True, verify=False))
    assert server_url.startswith("https://")
    assert kwargs.get("verify") is False


@pytest.mark.parametrize(
    ("secure", "verify"),
    [(False, True), (True, True), (True, False)],
)
def test_native_driver_passes_secure_and_verify(secure, verify):
    """The native driver's connection gets the configured secure/verify flags."""
    url = make_url(_url(driver="native", port=9440, secure=secure, verify=verify))
    assert url.drivername == "clickhouse+native"
    # The native dialect hands the URL (as clickhouse://) to clickhouse_driver.
    client = Client.from_url(
        url.set(drivername="clickhouse").render_as_string(hide_password=False),
    )
    assert client.connection.secure_socket is secure
    assert client.connection.verify_cert is verify
