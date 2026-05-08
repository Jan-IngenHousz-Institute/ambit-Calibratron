#include <Wire.h>
#include <Arduino.h>
#include <esp_system.h>

#include "app/debug_api.h"

void i2c_scan() {
  Serial.print(F("{\"i2c_scan\":{\"devices\":["));
  bool first = true;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      if (!first) Serial.print(',');
      first = false;
      Serial.print(F("\"0x"));
      if (addr < 0x10) Serial.print('0');
      Serial.print(addr, HEX);
      Serial.print('"');
    }
  }
  Serial.print(F("]}}"));
}

void cmd_reboot() {
  Serial.println(F("{\"reboot\":\"initiated\"}"));
  Serial.flush();
  esp_restart();
}
