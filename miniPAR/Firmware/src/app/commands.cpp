#include <Wire.h>
#include "app/commands.h"
#include "HWCDC.h"
#include "app/debug_api.h"
#include "app/spectrometer_api.h"

#include <ArduinoJson.h>


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static void printJsonSafeString(const char *s) {
  while (*s) {
    char c = *s++;
    if (c == '"' || c == '\\') {
      Serial.print('\\');
      Serial.print(c);
    } else if (c < 0x20) {
      // skip control characters
    } else {
      Serial.print(c);
    }
  }
}

// ---------------------------------------------------------------------------
// JSON protocol wrapper
// ---------------------------------------------------------------------------

static bool serial_string_init() {
  Serial.print(F("{\"device_name\":\"PAR meter\",\"device_version\":\"1\",\"device_id\":\"esp32-c3\""));
  Serial.print(F(",\"device_battery\":\"NaN\",\"device_firmware\":1.001,\"sample\":[{\"protocol_id\":\"NaN\",\"set\":["));
  return true;
}

static bool serial_string_end() {
  Serial.print(F("]}]}7A1E3AA1"));
  Serial.print('\n');
  return true;
}

bool handleJsonProtocol(const String &json) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json);

  if (err) {
    Serial.print(F("{\"error\":\"json_parse_error\",\"detail\":\""));
    Serial.print(err.c_str());
    Serial.println(F("\"}"));
    return false;
  }

  bool handledAny = false;
  bool firstOut = true;

  serial_string_init();

  auto processProtocolSet = [&](JsonArray proto) {
    for (JsonVariant setV : proto) {
      JsonObject setObj = setV.as<JsonObject>();
      if (setObj.isNull())
        continue;

      const char *cmd = setObj["label"];
      if (!cmd)
        continue;

      uint16_t repeats = setObj["protocol_repeats"] | 1;
      if (repeats == 0)
        repeats = 1;

      for (uint16_t i = 0; i < repeats; i++) {
        if (!firstOut)
          Serial.print(',');
        firstOut = false;

        handleCommandText(String(cmd));
        handledAny = true;
      }
    }
  };

  if (doc.is<JsonArray>()) {
    JsonArray arr = doc.as<JsonArray>();
    // Try: array of objects each containing a "set" key
    for (JsonVariant v : arr) {
      JsonObject obj = v.as<JsonObject>();
      if (obj.isNull())
        continue;
      JsonArray proto = obj["set"].as<JsonArray>();
      if (!proto.isNull())
        processProtocolSet(proto);
    }
    // Fallback: treat the array itself as the command list
    if (!handledAny)
      processProtocolSet(arr);
  } else if (doc.is<JsonObject>()) {
    // Iterate all members; process any array value as the command list
    for (JsonPair kv : doc.as<JsonObject>()) {
      JsonArray proto = kv.value().as<JsonArray>();
      if (!proto.isNull()) {
        processProtocolSet(proto);
        break;
      }
    }
  } else {
    serial_string_end();
    return false;
  }

  serial_string_end();
  return handledAny;
}





bool handleCommandText(const String &cmd) {
  if (cmd == "hello") {
    Serial.print(F("{\"device\":\"MiniPAR\",\"version\":\"1.1\"}"));
  } else if (cmd == "battery") {
    Serial.print(F("{\"battery\":\"NaN\"}"));

  } else if (cmd == "i2c_scan") {
    i2c_scan();

  } else if (cmd == "spec") {
    spectrometer_read();

  } else if (cmd == "spec_raw") {
    spectrometer_read_raw();

  } else if (cmd.startsWith("set_led")) {
    int ledCurrent = 10; // default LED current in mA
    int comma = cmd.indexOf(',');
    if (comma > 0) {// If there's a comma, parse the LED current argument
      String arg = cmd.substring(comma + 1);
      arg.trim();
      ledCurrent = arg.toInt();
    }
    spectrometer_set_led_current(static_cast<uint16_t>(ledCurrent));

  } else if (cmd.startsWith("spec_flash")) {
    // print the AS7341 read with LED off and on, and the difference.
    int ledCurrent = 10; // default LED current in mA
    int comma = cmd.indexOf(',');

    if (comma > 0) {// If there's a comma, parse the LED current argument
      String arg = cmd.substring(comma + 1);
      arg.trim();
      ledCurrent = arg.toInt();
    }

    spectrometer_read_flash(static_cast<uint16_t>(ledCurrent));
  
  } else if (cmd.startsWith("spec_set_atime")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_atime(comma > 0 ? 1 : 0, &arg);

  } else if (cmd.startsWith("spec_set_astep")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_astep(comma > 0 ? 1 : 0, &arg);

  } else if (cmd.startsWith("spec_set_gain")) {
    int comma = cmd.indexOf(',');
    const char *arg = (comma > 0) ? cmd.c_str() + comma + 1 : "";
    cmd_spectrometer_set_gain(comma > 0 ? 1 : 0, &arg);

  } else if (cmd == "spec_status") {
    cmd_spectrometer_status();

  } else if (cmd == "status") {
    cmd_spectrometer_status();

  } else if (cmd == "par_raw") {
    float par_raw = 0;
    if (cmd_get_par_raw(&par_raw)) {
      Serial.print(F("{\"par_raw\":"));
      Serial.print(par_raw);
      Serial.print(F("}"));
    }

  } else if (cmd == "par") {
    float par = 0;
    if (cmd_get_par(&par)) {
      Serial.print(F("{\"par\":"));
      Serial.print(par);
      Serial.print(F("}"));
    }
  
  } else if (cmd.startsWith("cal_par_slope")) {
    // Example command: cal_par_slope,0.5
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      const char *arg = cmd.c_str() + comma + 1;
      set_calibration_slope(1, &arg);
    } else {
      Serial.print(F("{\"calibration\":{\"error\":\"missing_args\"}}"));
    }

  } else if (cmd.startsWith("cal_par_intercept")) {
    // Example command: cal_par_intercept,0.2
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      const char *arg = cmd.c_str() + comma + 1;
      set_calibration_intercept(1, &arg);
    } else {
      Serial.print(F("{\"calibration\":{\"error\":\"missing_args\"}}"));
    }

  } else if (cmd.startsWith("set_name")) {

    int comma = cmd.indexOf(',');
    if (comma > 0) {
      const char *arg = cmd.c_str() + comma + 1;
      cmd_set_dev_name(1, &arg);
      Serial.print(F("{\"device_name\":\""));
      printJsonSafeString(cmd_get_dev_name());
      Serial.print(F("\"}"));

    } else {
      Serial.print(F("{\"error\":\"missing_args\"}"));
    }
  } else if (cmd.startsWith("get_name")) {
    Serial.print(F("{\"device_name\":\""));
    printJsonSafeString(cmd_get_dev_name());
    Serial.print(F("\"}"));
  
  
  } else if (cmd == "reboot") {
    cmd_reboot();

  } else if (cmd.length() > 0) {
    Serial.print(F("{\"error\":\"unknown_command\"}"));
    return false;
  }
  return true;
}
