#include <Arduino.h>
#include <Wire.h>
#include "app/commands.h"
#include "app/spectrometer_api.h"


String line;
enum class RxMode { UNKNOWN, LINE, JSON };
String rx;
RxMode mode = RxMode::UNKNOWN;
int braceDepth = 0;   // {}
int bracketDepth = 0; // []
bool inString = false;
char prev = 0;

void resetRx() {
  rx = "";
  mode = RxMode::UNKNOWN;
  braceDepth = 0;
  bracketDepth = 0;
  inString = false;
  prev = 0;
}

void setup() {
  
  Serial.begin(115200);

  Wire.begin(3, 4);
  
  loadpref();
  // Spectrometer detection and backend initialization
  initSpectrometer();
  Serial.println(F("Ready"));

  

}





void loop() {
while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r')
      continue;

    rx += c;

    // Decide mode once we see the first non-whitespace char
    if (mode == RxMode::UNKNOWN) {
      // find first non-space char in the current buffer
      int i = 0;
      while (i < (int)rx.length() && isspace((unsigned char)rx[i]))
        i++;
      if (i < (int)rx.length()) {
        char first = rx[i];
        if (first == '{' || first == '[') {
          mode = RxMode::JSON; // start JSON mode (typically openJII app series of commands)
        } else {
          mode = RxMode::LINE; // start LINE mode (simple single-line commands)
        }
      }
    }

    // LINE mode: only process on newline
    if (mode == RxMode::LINE) {
      if (c == '\n') {
        rx.trim();
        if (rx.length() > 0)
          handleCommandText(rx);
        Serial.println();
        resetRx();
      }
      continue;
    }

    // JSON mode: track braces/brackets until outermost closes
    if (mode == RxMode::JSON) {
      if (c == '"' && prev != '\\')
        inString = !inString;

      if (!inString) {
        if (c == '{')
          braceDepth++;
        else if (c == '}')
          braceDepth--;
        else if (c == '[')
          bracketDepth++;
        else if (c == ']')
          bracketDepth--;
      }

      prev = c;

      // Only declare "complete" after the top-level JSON object/array closes.
      if (!inString && braceDepth == 0 && bracketDepth == 0) {
        rx.trim();
        bool ok = handleJsonProtocol(rx);
        if (!ok)
          Serial.println(F("{\"error\":\"json_dispatch_failed\"}"));
        resetRx();
      }
      continue;
    }

    // Safety: prevent runaway buffer
    if (rx.length() > 2048) {
      Serial.println("rx_overflow");
      resetRx();
    }
  }
}






