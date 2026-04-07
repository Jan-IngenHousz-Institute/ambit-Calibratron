#include <Wire.h>
#include <Arduino.h>
#include <esp_system.h>

#include "app/debug_api.h"

void i2c_scan() {
  Serial.println("Scanning...");
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    uint8_t err = Wire.endTransmission();
    if (err == 0) {
      Serial.print("Found I2C device at 0x");
      if (addr < 16) Serial.print("0");
      Serial.println(addr, HEX);
    }
  }
  Serial.println("Done.");
}

void cmd_reboot() {
  Serial.println(F("{\"reboot\":\"initiated\"}"));
  Serial.flush();
  esp_restart();
}
