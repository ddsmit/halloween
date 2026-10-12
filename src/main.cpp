#include <Arduino.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>

#include "secrets.h"

namespace {
String line;
constexpr uint8_t sensorCount = 3;
const uint8_t sensorPins[sensorCount] = {4, 5};
const char *sensorNames[sensorCount] = {"shot1", "shot2"};
constexpr unsigned long sampleIntervalMs = 50;
unsigned long sensorHoldMs = 100;
unsigned long lastSampleMs = 0;
unsigned long lastWifiAttemptMs = 0;
unsigned long lastConfigPollMs = 0;
int stableValues[sensorCount] = {-1, -1, -1};
int candidateValues[sensorCount] = {-1, -1, -1};
unsigned long changedMs[sensorCount] = {0, 0, 0};

void publishSensor(uint8_t index, int value) {
  if (WiFi.status() != WL_CONNECTED) return;

  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, EVENT_SERVER_URL)) return;
  http.addHeader("Content-Type", "application/json");
  String body = "{\"sensor\":\"" + String(sensorNames[index]) + "\",\"pin\":";
    body += String(sensorPins[index]) + ",\"value\":" + String(value);
  body += ",\"active\":" + String(value == 0 ? "true" : "false") + "}";
  http.POST(body);
  http.end();
}

void pollConfig() {
  if (WiFi.status() != WL_CONNECTED || millis() - lastConfigPollMs < 2000) return;
  lastConfigPollMs = millis();

  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, CONFIG_SERVER_URL)) return;
  const int status = http.GET();
  if (status == HTTP_CODE_OK) {
    const String body = http.getString();
    const int key = body.indexOf("sensor_hold_ms");
    const int colon = body.indexOf(':', key);
    if (key >= 0 && colon >= 0) {
      const unsigned long value = body.substring(colon + 1).toInt();
      if (value <= 60000) {
        sensorHoldMs = value;
        Serial.printf("Sensor hold time: %lu ms\n", sensorHoldMs);
      }
    }
  }
  http.end();
}

void connectWiFi() {
  if (String(WIFI_SSID).startsWith("your-")) {
    Serial.println(F("Wi-Fi credentials are not configured"));
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print(F("Connecting to Wi-Fi"));
  const unsigned long deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print(F("Wi-Fi address: "));
    Serial.println(WiFi.localIP());
    Serial.println(F("Wi-Fi event publishing enabled"));
  } else {
    Serial.printf("Wi-Fi connection failed (status %d); continuing offline\n", WiFi.status());
  }
}

void sampleSensor() {
  const unsigned long now = millis();
  if (now - lastSampleMs < sampleIntervalMs) return;
  lastSampleMs = now;
  for (uint8_t index = 0; index < sensorCount; index++) {
    const int value = digitalRead(sensorPins[index]);
    if (value != candidateValues[index]) {
      candidateValues[index] = value;
      changedMs[index] = now;
    }
    if (value == stableValues[index] || now - changedMs[index] < sensorHoldMs) continue;
    stableValues[index] = value;
    Serial.printf("Sensor %s GPIO %d = %d\n", sensorNames[index], sensorPins[index], value);
    publishSensor(index, value);
  }
}

bool validPin(int pin) {
  // Excludes flash-connected pins and keeps the console suitable for common
  // NodeMCU ESP8266 boards. Confirm the board pinout before wiring anything.
  return pin >= 0 && pin <= 16 && !(pin >= 6 && pin <= 11);
}

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  mode <pin> in|pullup|out"));
  Serial.println(F("  read <pin>"));
  Serial.println(F("  write <pin> 0|1"));
  Serial.println(F("  help"));
  Serial.println(F("Pins default to INPUT; verify your board pinout first."));
}

void handleCommand(String command) {
  command.trim();
  if (command.isEmpty()) return;

  const int firstSpace = command.indexOf(' ');
  const String verb = (firstSpace < 0) ? command : command.substring(0, firstSpace);
  String args = (firstSpace < 0) ? "" : command.substring(firstSpace + 1);
  args.trim();

  if (verb == "help") {
    printHelp();
    return;
  }

  int pin = -1;
  int value = -1;
  char mode[12] = {};

  if (verb == "read" && sscanf(args.c_str(), "%d", &pin) == 1) {
    if (!validPin(pin)) {
      Serial.println(F("ERR invalid pin"));
      return;
    }
    Serial.printf("GPIO %d = %d\n", pin, digitalRead(pin));
    return;
  }

  if (verb == "write" && sscanf(args.c_str(), "%d %d", &pin, &value) == 2) {
    if (!validPin(pin) || (value != 0 && value != 1)) {
      Serial.println(F("ERR use: write <pin> 0|1"));
      return;
    }
    digitalWrite(pin, value);
    Serial.printf("GPIO %d <- %d\n", pin, value);
    return;
  }

  if (verb == "mode" && sscanf(args.c_str(), "%d %11s", &pin, mode) == 2) {
    if (!validPin(pin)) {
      Serial.println(F("ERR invalid pin"));
      return;
    }
    if (strcmp(mode, "in") == 0) pinMode(pin, INPUT);
    else if (strcmp(mode, "pullup") == 0) pinMode(pin, INPUT_PULLUP);
    else if (strcmp(mode, "out") == 0) pinMode(pin, OUTPUT);
    else {
      Serial.println(F("ERR mode is in, pullup, or out"));
      return;
    }
    Serial.printf("GPIO %d mode = %s\n", pin, mode);
    return;
  }

  Serial.println(F("ERR unknown command; type help"));
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  for (uint8_t index = 0; index < sensorCount; index++) {
    pinMode(sensorPins[index], INPUT_PULLUP);
    stableValues[index] = digitalRead(sensorPins[index]);
    candidateValues[index] = stableValues[index];
  }
  Serial.println();
  Serial.println(F("ESP8266 GPIO console ready"));
  printHelp();
  connectWiFi();
  for (uint8_t index = 0; index < sensorCount; index++) {
    publishSensor(index, stableValues[index]);
  }
  Serial.print(F("> "));
}

void loop() {
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiAttemptMs > 30000) {
    lastWifiAttemptMs = millis();
    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) {
      for (uint8_t index = 0; index < sensorCount; index++) {
        publishSensor(index, stableValues[index]);
      }
    }
  }
  pollConfig();
  sampleSensor();
  while (Serial.available()) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n' || character == '\r') {
      handleCommand(line);
      line = "";
      Serial.print(F("> "));
    } else if (line.length() < 80) {
      line += character;
    }
  }
}
