#!/usr/bin/env python3
"""Read an ESP8266 sensor over serial and expose its state over HTTP."""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial


READ_RE = re.compile(r"^GPIO (\d+) = ([01])$")


class SensorApp:
    def __init__(self, port, pin, mode, interval):
        self.pin = pin
        self.mode = mode
        self.interval = interval
        self.serial = serial.Serial(port, 115200, timeout=0.2, write_timeout=1)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = None
        self.last_change = None
        # Opening the USB serial port commonly resets the ESP8266.
        time.sleep(2)

    def send_command(self, command, expected=None):
        """Send one console command and return its response line."""
        self.serial.write((command + "\n").encode("ascii"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            line = self.serial.readline().decode("utf-8", errors="replace").strip()
            line = line.lstrip("> ").strip()
            if not line:
                continue
            if line.startswith("ERR"):
                raise RuntimeError(line)
            if expected is None or expected(line):
                return line
        raise TimeoutError(f"No response to {command!r}")

    def read_sensor(self):
        line = self.send_command(
            f"read {self.pin}",
            expected=lambda value: READ_RE.match(value) is not None,
        )
        match = READ_RE.match(line)
        if match is None or int(match.group(1)) != self.pin:
            raise RuntimeError(f"Unexpected sensor response: {line!r}")
        return int(match.group(2))

    def snapshot(self):
        with self.lock:
            return {
                "pin": self.pin,
                "mode": self.mode,
                "value": self.state,
                "active": self.state == 0 if self.mode == "pullup" else self.state == 1,
                "last_change": self.last_change,
            }

    def on_sensor_change(self, old_value, new_value):
        """Customize the system response here."""
        print(f"Sensor GPIO {self.pin}: {old_value} -> {new_value}", flush=True)
        # Add application behavior here, for example an HTTP request, logging,
        # or a command sent to another GPIO.

    def run(self):
        self.send_command(
            f"mode {self.pin} {self.mode}",
            expected=lambda value: value == f"GPIO {self.pin} mode = {self.mode}",
        )
        previous = self.read_sensor()
        with self.lock:
            self.state = previous
            self.last_change = time.time()
        print(json.dumps(self.snapshot()), flush=True)

        while not self.stop_event.wait(self.interval):
            current = self.read_sensor()
            if current == previous:
                continue
            with self.lock:
                self.state = current
                self.last_change = time.time()
            self.on_sensor_change(previous, current)
            previous = current

    def close(self):
        self.stop_event.set()
        self.serial.close()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
            if self.path not in ("/", "/state", "/health"):
                self.send_error(404)
                return
            body = json.dumps(app.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string, *args):
            print(f"HTTP: {format_string % args}", flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/cu.usbserial-0001", help="serial device")
    parser.add_argument("--pin", type=int, default=4, help="sensor GPIO number")
    parser.add_argument(
        "--mode",
        choices=("pullup", "in"),
        default="pullup",
        help="sensor input mode; pullup is for active-low sensors",
    )
    parser.add_argument("--interval", type=float, default=0.1, help="poll interval in seconds")
    parser.add_argument("--http-port", type=int, default=8000, help="HTTP port")
    args = parser.parse_args()

    app = SensorApp(args.port, args.pin, args.mode, args.interval)
    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Sensor server listening at http://127.0.0.1:{args.http_port}/state", flush=True)

    try:
        app.run()
    except KeyboardInterrupt:
        print("Stopping sensor server", flush=True)
    finally:
        server.shutdown()
        app.close()


if __name__ == "__main__":
    main()
