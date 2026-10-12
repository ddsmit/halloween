#!/usr/bin/env python3
"""Turn a Shelly Smart Plug on or off over the local network."""

import argparse
import base64
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def set_plug(ip, state, generation, username=None, password=None, timeout=5):
    """Set relay 0 to the requested state and return Shelly's response."""
    if generation == "gen1":
        path = "/relay/0?turn=" + state
    else:
        enabled = "true" if state == "on" else "false"
        path = f"/rpc/Switch.Set?id=0&on={enabled}"

    request = Request(f"http://{ip}{path}", method="GET")
    if username is not None:
        credentials = f"{username}:{password or ''}".encode()
        token = base64.b64encode(credentials).decode()
        request.add_header("Authorization", f"Basic {token}")

    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip", help="Shelly plug IP address or hostname")
    parser.add_argument("state", choices=("on", "off"))
    parser.add_argument(
        "--generation",
        choices=("gen1", "gen2"),
        default="gen1",
        help="Shelly API generation (default: gen1)",
    )
    parser.add_argument("--username", help="Local Shelly username, if enabled")
    parser.add_argument("--password", help="Local Shelly password, if enabled")
    parser.add_argument("--timeout", type=float, default=5, help="Request timeout in seconds")
    args = parser.parse_args()

    try:
        response = set_plug(
            args.ip,
            args.state,
            args.generation,
            args.username,
            args.password,
            args.timeout,
        )
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Could not reach Shelly plug: {error}", file=sys.stderr)
        return 1

    print(json.dumps(response) if isinstance(response, dict) else response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
