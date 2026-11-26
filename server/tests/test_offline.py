import os
import socket

import pytest

# TEST-NET-1 (RFC 5737): reserved for documentation, never routed.
PUBLIC_ADDRESS = ("192.0.2.1", 443)


def test_connecting_to_a_public_address_is_blocked() -> None:
    with socket.socket() as client:
        with pytest.raises(RuntimeError, match="Tests must run offline"):
            client.connect(PUBLIC_ADDRESS)
        with pytest.raises(RuntimeError, match="Tests must run offline"):
            client.connect_ex(PUBLIC_ADDRESS)


def test_connecting_to_loopback_still_works() -> None:
    with (
        socket.create_server(("127.0.0.1", 0)) as server,
        socket.create_connection(server.getsockname()) as client,
    ):
        assert client.getpeername() == server.getsockname()


def test_tests_run_in_replay_mode_without_an_api_key() -> None:
    assert os.environ["REPLAY"] == "1"
    assert "MODEL_API_KEY" not in os.environ
