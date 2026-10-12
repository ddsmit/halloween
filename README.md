# ESP8266 GPIO Console

This project provides a serial console for inspecting and driving GPIO pins on
an ESP8266 NodeMCU-style board. The connected board was detected as an ESP8266
through its CP2102 USB serial adapter.

## Commands

Open the serial monitor at 115200 baud and enter:

```text
mode 4 out
write 4 1
read 4
mode 5 pullup
read 5
```

Pins start as inputs. ESP8266 GPIOs are 3.3 V only. Do not connect a GPIO
directly to 3.3 V, 5 V, or ground
without confirming the board pinout and using appropriate current limiting.

## PlatformIO

```text
pio run
pio run -t upload
pio device monitor
```

## Python Sensor Server

The server owns the serial port, so close `pio device monitor` before starting
it. With a sensor connected to GPIO4/D2 and ground:

```text
.venv/bin/python sensor_server.py
```

The default `pullup` mode treats an untriggered sensor as `1` and a sensor
connected to ground as active (`0`). Check the current state from another
terminal:

```text
curl http://127.0.0.1:8000/state
```

For a different input pin:

```text
.venv/bin/python sensor_server.py --pin 5
```

Put the behavior you want on sensor transitions in
`SensorApp.on_sensor_change()` in `sensor_server.py`.

## Wi-Fi Mode

The ESP8266 can operate without the USB cable after it has been flashed. It
reads three inputs locally and sends named JSON events to the computer over
Wi-Fi:

- `shot1`: GPIO4/D2
- `shot2`: GPIO5/D1
- `gutter`: GPIO12/D6

Each board has a unique `DEVICE_ID` in `include/secrets.h`. Use a different
value for every additional ESP board before flashing it.

1. Start the computer receiver:

```text
.venv/bin/python wifi_sensor_server.py --shelly-ip SHELLY_IP --shelly-generation gen1
```

2. Find the computer's LAN IP address. On macOS, this is commonly shown by:

```text
ipconfig getifaddr en0
```

3. Edit `include/secrets.h`, replacing the Wi-Fi credentials and the computer
IP in both `EVENT_SERVER_URL` and `CONFIG_SERVER_URL`.

4. Upload the firmware while the USB cable is connected:

```text
.venv/bin/pio run -t upload --upload-port /dev/cu.usbserial-0001
```

5. After the board connects to Wi-Fi, the USB cable can be removed. View the
latest event with:

```text
curl http://127.0.0.1:8000/state
```

Open `http://127.0.0.1:8000/` in a browser for the control interface. It shows
each sensor independently, has a press-and-hold `Dispense Candy` button, and
adjusts the drill maximum on-time and how long a sensor must remain active
before the ESP8266 sends an event. Either shot sensor starts one made-shot
sequence; both shot sensors must become inactive before another sequence can
start. The gutter sensor turns the drill off early.
The ESP8266 polls the hold-time setting every two seconds. Both devices must be
on the same LAN, and the computer firewall must allow TCP port 8000.

Use the **Settings** tab to rename discovered sensors, assign their roles
(`shot`, `gutter`, or `monitor`), and name/configure Shelly outputs. The server
saves this registry in `server_config.json`.

The receiver turns the Shelly plug on for four seconds when the sensor becomes
active. It ignores repeated active events until an inactive event arrives, then
re-arms. Use `gen2` for newer Shelly devices.
