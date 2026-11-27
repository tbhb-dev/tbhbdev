"""What the command's modules share: its one error and its HTTPS call."""

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

# A request: method, URL, headers and body. It answers with the status and the
# body. A test replaces it, so no test reaches a network.
type Http = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]
# A command's argv. It answers with the output, and raises CliError when the
# command fails.
type Run = Callable[[list[str]], str]

TIMEOUT = 60  # pragma: no mutate  # seconds; any bound ends a hung call


class CliError(Exception):
    """A problem the command reports and stops on.

    Its message is safe to print: no source builds one from a secret, a URL or
    a response body.
    """


def https_request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """Send a request over HTTPS and return the status and the body.

    A failure to connect raises CliError with the exception's type only,
    because the exception's text can hold the URL.
    """
    if not url.startswith("https://"):
        message = "refusing to send a request over anything but https"
        raise CliError(message)
    request = urllib.request.Request(  # noqa: S310  # https only, checked above
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310  # nosec B310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except OSError as exc:
        message = f"a request failed: {type(exc).__name__}"
        raise CliError(message) from None
