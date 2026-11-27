# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Send a batch of spans over OTLP HTTP."""

import http.client
from urllib.parse import urlsplit


def endpoint_port(url):
    port = url.port
    return port if port is not None else (443 if url.scheme == "https" else 80)


def export(spans, endpoint):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import encode_spans

    url = urlsplit(endpoint)
    connection = (
        http.client.HTTPSConnection
        if url.scheme == "https"
        else http.client.HTTPConnection
    )(url.hostname, endpoint_port(url), timeout=0.25)
    try:
        connection.request(
            "POST",
            url.path.rstrip("/") + "/v1/traces",
            encode_spans(spans).SerializeToString(),
            {"Content-Type": "application/x-protobuf"},
        )
        if not 200 <= connection.getresponse().status < 300:
            raise RuntimeError("collector rejected batch")
    finally:
        connection.close()
