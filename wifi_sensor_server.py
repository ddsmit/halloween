#!/usr/bin/env python3
"""Receive ESP8266 sensor events and control a Shelly plug."""

import argparse
import json
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shelly_plug import set_plug


VOICE = "Bad News"
ANNOUNCEMENT = "Nice shot Yolonda"
VOICE_ENABLED = False


def announce_hit():
    """Play the local Halloween announcement before activating the plug."""
    if not VOICE_ENABLED:
        return
    try:
        subprocess.run(
            ["say", "-v", VOICE, ANNOUNCEMENT],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Could not play announcement: {error}", flush=True)


class AppState:
    def __init__(self, config_path, shelly_ip, shelly_generation):
        self.lock = threading.Lock()
        self.event = None
        self.config_path = Path(config_path)
        self.sensors = {}
        self.sensor_configs = {}
        self.outputs = {
            "drill": {"name": "Drill", "ip": shelly_ip, "generation": shelly_generation}
        }
        self.settings = {"drill_max_seconds": 4.0, "sensor_hold_ms": 1}
        self.shelly_on = False
        self.shelly_blocked = False
        self.load_configuration()

    def load_configuration(self):
        try:
            saved = json.loads(self.config_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.sensor_configs.update(saved.get("sensor_configs", {}))
        self.outputs.update(saved.get("outputs", {}))
        self.settings.update(saved.get("settings", {}))

    def save_configuration(self):
        data = {
            "sensor_configs": self.sensor_configs,
            "outputs": self.outputs,
            "settings": self.settings,
        }
        self.config_path.write_text(json.dumps(data, indent=2) + "\n")

    def snapshot(self):
        with self.lock:
            return {
                "event": self.event,
                "sensors": dict(self.sensors),
                "sensor_configs": dict(self.sensor_configs),
                "outputs": dict(self.outputs),
                "settings": dict(self.settings),
                "shelly_on": self.shelly_on,
                "shelly_blocked": self.shelly_blocked,
            }

    def update_event(self, event):
        with self.lock:
            self.event = event
            device_id = event.get("device_id", "legacy")
            sensor = event.get("sensor") or {4: "shot1", 5: "shot2", 12: "gutter"}.get(event.get("pin"), "input")
            key = f"{device_id}:{sensor}"
            event["device_id"] = device_id
            event["sensor"] = sensor
            event["key"] = key
            self.sensors[key] = event
            if key not in self.sensor_configs:
                role = "gutter" if "gutter" in sensor.lower() else ("shot" if "shot" in sensor.lower() else "monitor")
                self.sensor_configs[key] = {"name": f"{device_id} {sensor}", "role": role}
                self.save_configuration()

    def role_for_event(self, event):
        key = event.get("key") or f"{event.get('device_id', 'legacy')}:{event.get('sensor', 'input')}"
        return self.sensor_configs.get(key, {}).get("role", "monitor")

    def update_configuration(self, values):
        with self.lock:
            for key, config in values.get("sensor_configs", {}).items():
                if key in self.sensors and isinstance(config, dict):
                    self.sensor_configs[key] = {
                        "name": str(config.get("name", key))[:80],
                        "role": config.get("role", "monitor") if config.get("role") in ("shot", "gutter", "monitor") else "monitor",
                    }
            for key, output in values.get("outputs", {}).items():
                if isinstance(output, dict) and output.get("ip"):
                    self.outputs[key] = {
                        "name": str(output.get("name", key))[:80],
                        "ip": str(output["ip"]),
                        "generation": output.get("generation", "gen2"),
                    }
            self.save_configuration()

    def output(self, key="drill"):
        with self.lock:
            return dict(self.outputs[key])

    def update_settings(self, values):
        with self.lock:
            if "drill_max_seconds" in values:
                self.settings["drill_max_seconds"] = min(
                    3600.0, max(0.1, float(values["drill_max_seconds"]))
                )
            if "sensor_hold_ms" in values:
                self.settings["sensor_hold_ms"] = min(
                    60000, max(0, int(values["sensor_hold_ms"]))
                )
            self.save_configuration()

    def set_shelly_state(self, is_on):
        with self.lock:
            self.shelly_on = is_on

    def set_shelly_blocked(self, blocked):
        with self.lock:
            self.shelly_blocked = blocked


class ShellyTrigger:
    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()
        self.armed = True
        self.timer = None
        self.manual_timer = None
        self.manual_active = False

    def set_state(self, is_on):
        output = self.state.output()
        set_plug(output["ip"], "on" if is_on else "off", output["generation"])
        self.state.set_shelly_state(is_on)

    def handle(self, event):
        role = self.state.role_for_event(event)
        active = bool(event.get("active", event.get("value") == 0))

        if role == "gutter":
            if active:
                with self.lock:
                    if self.timer:
                        self.timer.cancel()
                        self.timer = None
                    self.manual_active = False
                print("Gutter sensor active; turning drill off", flush=True)
                self.turn_off()
            return

        if not active:
            snapshot = self.state.snapshot()
            shot_active = any(
                config.get("role") == "shot" and event_data.get("active", False)
                for key, event_data in snapshot["sensors"].items()
                for config in [snapshot["sensor_configs"].get(key, {})]
            )
            if not shot_active:
                with self.lock:
                    self.armed = True
                print("Shot sensors inactive; trigger re-armed", flush=True)
            return

        if role != "shot":
            return

        if self.state.snapshot()["shelly_blocked"]:
            print("Shelly blocked; recording shot sensor without activating plug", flush=True)
            return

        with self.lock:
            if self.manual_active:
                return
            if not self.armed:
                print("Sensor still active; ignoring repeated trigger", flush=True)
                return
            self.armed = False

        try:
            announce_hit()
            self.set_state(True)
            seconds = self.state.snapshot()["settings"]["drill_max_seconds"]
            print(f"Shelly plug on for {seconds:g} seconds", flush=True)
        except Exception as error:
            with self.lock:
                self.armed = True
            print(f"Could not turn Shelly plug on: {error}", flush=True)
            return

        self.timer = threading.Timer(seconds, self.turn_off)
        self.timer.daemon = True
        self.timer.start()

    def manual_on(self):
        if self.state.snapshot()["shelly_blocked"]:
            return False
        with self.lock:
            self.manual_active = True
            if self.timer:
                self.timer.cancel()
            if self.manual_timer:
                self.manual_timer.cancel()
        self.set_state(True)
        # Prevent a lost browser connection from leaving the plug on forever.
        self.manual_timer = threading.Timer(30, self.manual_off)
        self.manual_timer.daemon = True
        self.manual_timer.start()
        return True

    def manual_off(self):
        with self.lock:
            self.manual_active = False
            if self.manual_timer and self.manual_timer is not threading.current_thread():
                self.manual_timer.cancel()
            self.manual_timer = None
        self.set_state(False)

    def turn_off(self):
        try:
            self.set_state(False)
            print("Shelly plug off", flush=True)
        except Exception as error:
            print(f"Could not turn Shelly plug off: {error}", flush=True)

PAGE = """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Halloween Sensor</title>
<style>
body{font:16px system-ui;max-width:560px;margin:2rem auto;padding:0 1rem;background:#17131c;color:#f4edf8}
main{background:#261d2d;padding:1.5rem;border-radius:16px} h1{margin-top:0;color:#ffad42}
.value{font-size:2rem;margin:.5rem 0 1.5rem}.sensor-card{display:flex;align-items:center;gap:.6rem;margin:.7rem 0}.indicator{width:1rem;height:1rem;border-radius:50%;background:#75677d;box-shadow:0 0 0 4px #75677d33}.indicator.active{background:#ff553d;box-shadow:0 0 0 4px #ff553d44;animation:pulse 1s infinite}.indicator.inactive{background:#54c27a;box-shadow:0 0 0 4px #54c27a33}@keyframes pulse{50%{opacity:.45}} button{font:inherit;padding:.7rem 1rem;margin:.25rem;border:0;border-radius:9px;cursor:pointer}
 .on{background:#ff8b38}.off{background:#b8a9c9}.block{background:#e85d75;color:#fff}.setting{margin:1.4rem 0}.setting label{display:block;margin-bottom:.4rem}.tabs{display:flex;gap:.5rem;margin-bottom:1rem}.tabs button{background:#49344f;color:#fff}.tabs button.selected{background:#ff8b38}.panel{display:none}.panel.selected{display:block}.row{border-top:1px solid #49344f;padding:.7rem 0}.row input,.row select{width:100%;box-sizing:border-box;margin:.3rem 0;padding:.45rem}
input{width:100%}#message{min-height:1.4em;color:#cdbbd8}
</style></head><body><main><h1>Halloween Sensor</h1>
 <div class="tabs"><button id="mainTab" class="selected" onclick="tab('main')">Control</button><button id="settingsTab" onclick="tab('settings')">Settings</button></div>
 <section id="mainPanel" class="panel selected"><div class="value">Sensors</div><div id="sensorList"></div><div> Shelly: <strong id="plug">unknown</strong></div>
 <p><button class="on" id="dispense" onpointerdown="startDispense(event)" onpointerup="stopDispense(event)" onpointercancel="stopDispense(event)" onpointerleave="stopDispense(event)">Dispense Candy</button> <button class="block" id="blockButton" onclick="toggleBlock()">Block Shelly</button></p>
 <div class="setting"><label for="drillTime">Drill maximum on time (seconds)</label><input id="drillTime" type="number" min="0.1" max="3600" step="0.1" onfocus="settingsDirty=true"></div>
 <div class="setting"><label for="holdTime">Sensor hold time (milliseconds)</label><input id="holdTime" type="number" min="0" max="60000" step="50" onfocus="settingsDirty=true"></div>
 <button class="off" onclick="save()">Save settings</button></section>
 <section id="settingsPanel" class="panel"><h2>Inputs</h2><div id="sensorSettings"></div><h2>Outputs</h2><div id="outputSettings"></div><button class="off" onclick="addOutput()">Add output</button><button class="off" onclick="saveConfiguration()">Save names and roles</button></section>
 <div id="message"></div></main>
<script>
 let settingsDirty=false;
 function esc(v){return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');}
 function tab(name){for(let n of ['main','settings']){document.getElementById(n+'Panel').classList.toggle('selected',n===name);document.getElementById(n+'Tab').classList.toggle('selected',n===name);}}
 function renderSensors(s){let keys=Object.keys(s.sensors);sensorList.innerHTML=keys.length?'':'No sensors seen yet';for(let key of keys){let e=s.sensors[key],c=s.sensor_configs[key]||{name:key},active=!!e.active,row=document.createElement('div');row.className='sensor-card';row.innerHTML='<span class="indicator '+(active?'active':'inactive')+'"></span><strong>'+esc(c.name)+'</strong><span>'+(active?'ACTIVE':'inactive')+'</span>';sensorList.appendChild(row);}}
 function renderConfiguration(s){sensorSettings.innerHTML=Object.keys(s.sensor_configs).map(k=>{let c=s.sensor_configs[k];return '<div class="row"><b>'+esc(k)+'</b><input class="sensor-name" data-key="'+esc(k)+'" value="'+esc(c.name)+'" placeholder="Sensor name"><select class="sensor-role" data-key="'+esc(k)+'"><option '+(c.role==='shot'?'selected':'')+'>shot</option><option '+(c.role==='gutter'?'selected':'')+'>gutter</option><option '+(c.role==='monitor'?'selected':'')+'>monitor</option></select></div>';}).join('')||'No sensors seen yet';outputSettings.innerHTML=Object.keys(s.outputs).map(k=>{let o=s.outputs[k];return '<div class="row"><b>'+esc(k)+'</b><input class="output-name" data-key="'+esc(k)+'" value="'+esc(o.name)+'" placeholder="Output name"><input class="output-ip" data-key="'+esc(k)+'" value="'+esc(o.ip)+'" placeholder="Shelly hostname or IP"><select class="output-generation" data-key="'+esc(k)+'"><option '+(o.generation==='gen1'?'selected':'')+'>gen1</option><option '+(o.generation==='gen2'?'selected':'')+'>gen2</option></select></div>';}).join('');}
 function addOutput(){let key=prompt('Output ID (for example jump-scare)');if(!key)return;outputSettings.insertAdjacentHTML('beforeend','<div class="row"><b>'+esc(key)+'</b><input class="output-name" data-key="'+esc(key)+'" value="" placeholder="Output name"><input class="output-ip" data-key="'+esc(key)+'" value="" placeholder="Shelly hostname or IP"><select class="output-generation" data-key="'+esc(key)+'"><option>gen2</option><option>gen1</option></select></div>');}
 async function refresh(){let s=await (await fetch('/api/state')).json();renderSensors(s);renderConfiguration(s);plug.textContent=s.shelly_blocked?'BLOCKED':(s.shelly_on?'ON':'off');blockButton.textContent=s.shelly_blocked?'Unblock Shelly':'Block Shelly';if(!settingsDirty){drillTime.value=s.settings.drill_max_seconds;holdTime.value=s.settings.sensor_hold_ms;}}
 async function saveConfiguration(){let sensor_configs={};document.querySelectorAll('.sensor-name').forEach(e=>sensor_configs[e.dataset.key]={name:e.value,role:document.querySelector('.sensor-role[data-key="'+e.dataset.key+'"]').value});let outputs={};document.querySelectorAll('.output-name').forEach(e=>outputs[e.dataset.key]={name:e.value,ip:document.querySelector('.output-ip[data-key="'+e.dataset.key+'"]').value,generation:document.querySelector('.output-generation[data-key="'+e.dataset.key+'"]').value});let r=await fetch('/api/configuration',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sensor_configs,outputs})});message.textContent=r.ok?'Names and roles saved':'Could not save configuration';if(r.ok)refresh();}
 async function save(){let r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({drill_max_seconds:+drillTime.value,sensor_hold_ms:+holdTime.value})}); message.textContent=r.ok?'Settings saved':'Could not save settings'; if(r.ok)settingsDirty=false;}
 async function plug(state){let r=await fetch('/api/shelly',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state})}); message.textContent=r.ok?'Shelly updated':'Shelly request failed'; refresh();}
 async function toggleBlock(){let s=await (await fetch('/api/state')).json();let r=await fetch('/api/shelly-block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({blocked:!s.shelly_blocked})});message.textContent=r.ok?'Shelly block updated':'Could not update Shelly block';refresh();}
function startDispense(event){event.preventDefault(); event.currentTarget.setPointerCapture?.(event.pointerId); plug('on');}
function stopDispense(event){event.preventDefault(); plug('off');}
window.addEventListener('blur',()=>plug('off'));
 refresh(); setInterval(refresh,1000);
</script></body></html>"""


def make_handler(state, trigger):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value, status=200):
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))

        def do_GET(self):  # noqa: N802
            if self.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/config":
                self.send_json({"sensor_hold_ms": state.snapshot()["settings"]["sensor_hold_ms"]})
            elif self.path in ("/api/state", "/state", "/health"):
                self.send_json(state.snapshot())
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            try:
                values = self.read_json()
                if self.path == "/event":
                    if not isinstance(values, dict) or "pin" not in values or "value" not in values:
                        raise ValueError("event requires pin and value")
                    state.update_event(values)
                    print(f"Sensor event: {json.dumps(values, sort_keys=True)}", flush=True)
                    trigger.handle(values)
                    self.send_json({"ok": True})
                elif self.path == "/api/configuration":
                    state.update_configuration(values)
                    self.send_json(state.snapshot())
                elif self.path == "/api/settings":
                    state.update_settings(values)
                    self.send_json(state.snapshot()["settings"])
                elif self.path == "/api/shelly":
                    if values.get("state") not in ("on", "off"):
                        raise ValueError("state must be on or off")
                    if values["state"] == "on":
                        if not trigger.manual_on():
                            self.send_json({"error": "Shelly is blocked"}, 423)
                            return
                    else:
                        trigger.manual_off()
                    self.send_json({"ok": True})
                elif self.path == "/api/shelly-block":
                    blocked = bool(values.get("blocked"))
                    state.set_shelly_blocked(blocked)
                    if blocked:
                        trigger.turn_off()
                    self.send_json({"shelly_blocked": blocked})
                else:
                    self.send_error(404)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(400, str(error))
            except Exception as error:
                self.send_error(502, str(error))

        def log_message(self, format_string, *args):
            print(f"HTTP: {format_string % args}", flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--shelly-ip", required=True)
    parser.add_argument("--shelly-generation", choices=("gen1", "gen2"), default="gen1")
    parser.add_argument("--config", default="server_config.json")
    args = parser.parse_args()

    state = AppState(args.config, args.shelly_ip, args.shelly_generation)
    trigger = ShellyTrigger(state)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state, trigger))
    print(f"Open http://127.0.0.1:{args.port}/ in a browser")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping sensor server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
