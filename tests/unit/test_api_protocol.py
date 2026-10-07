import socket
from pathlib import Path

from apps.api.protocol import NoDelayHttpToolsProtocol


def test_protocol_sets_tcp_nodelay_on_accepted_socket() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # proto=0, like uvicorn's pre-bound socket
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    cli = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()
    assert conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 0

    class T:
        def get_extra_info(self, name, default=None):
            return conn if name == "socket" else default

    p = NoDelayHttpToolsProtocol.__new__(NoDelayHttpToolsProtocol)
    try:
        p.connection_made(T())  # type: ignore[arg-type]
    except AttributeError:  # base class needs a real loop/config; only the option matters here
        pass
    assert conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    for s in (srv, cli, conn):
        s.close()


def test_api_dockerfile_uses_nodelay_protocol() -> None:
    cmd = (Path(__file__).resolve().parents[2] / "infrastructure/docker/api.Dockerfile").read_text()
    assert '"--http", "apps.api.protocol:NoDelayHttpToolsProtocol"' in cmd
