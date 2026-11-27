# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Exercise the OTLP HTTP transport against loopback listeners."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override
from unittest.mock import patch

from tbhb_telemetry import exporter


class ExporterTest(unittest.TestCase):
    def export_to_loopback(self, host, family):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: V105 -- HTTP server dispatches by method name.
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.command, self.path))
                self.send_response(200)
                self.end_headers()

            @override
            def log_message(self, format, *args):
                del format, args

        class Server(ThreadingHTTPServer):
            address_family = family  # noqa: V107 -- Read by the base server.

        try:
            server = Server((host, 0), Handler)
        except OSError as error:
            if family == socket.AF_INET6:
                self.skipTest(f"IPv6 loopback ::1 cannot be bound: {error}")
            raise
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            authority = f"[{host}]" if family == socket.AF_INET6 else host
            exporter.export([], f"http://{authority}:{server.server_port}/")
            self.assertEqual(requests, [("POST", "/v1/traces")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_export_ipv6_loopback(self):
        self.export_to_loopback("::1", socket.AF_INET6)

    def test_export_ipv4_loopback(self):
        self.export_to_loopback("127.0.0.1", socket.AF_INET)

    def test_export_default_ports(self):
        for scheme, host, port, constructor in (
            ("http", "2001:db8::1", 80, "HTTPConnection"),
            ("https", "fd00::1:10", 443, "HTTPSConnection"),
        ):
            with (
                self.subTest(scheme=scheme),
                patch.object(exporter.http.client, constructor) as connection,
            ):
                connection.return_value.getresponse.return_value.status = 200
                exporter.export([], f"{scheme}://[{host}]/")
                connection.assert_called_once_with(host, port, timeout=0.25)


if __name__ == "__main__":
    unittest.main()
