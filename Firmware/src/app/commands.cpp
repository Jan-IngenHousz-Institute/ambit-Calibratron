#include <Wire.h>
#include "app/commands.h"
#include "app/debug_api.h"
#include "app/spectrometer_api.h"
#include "app/as7341_api.h"

#include <ArduinoJson.h>


// --- Command dispatch system ---
// find command by name and call it with optional argument(s).
constexpr size_t CMD_BUF_LEN = 96; // max command text length
constexpr int MAX_ARGS = 8;        // max args of function

using CmdFn = bool (*)(int argc, const char *argv[]);

struct CmdEntry {
  const char *name;
  CmdFn fn;
};

static const CmdEntry kCmds[] = {
    // {"read_spectro", as7341_readAll},
    // {"set_ATIME", as7341_setAtIME},
    // {"set_ASTEP", cmd_set_as7341_astep},
    // {"read_all", cmd_read_all},
};

static CmdFn findCommand(const char *name) { // find command by name
  for (auto &e : kCmds) {
    if (strcmp(e.name, name) == 0)
      return e.fn;
  }
  return nullptr;
}


bool commandExists(const char *cmdText) {
  // Use to check existence of a command before printing a comma separator
  if (!cmdText || !*cmdText)
    return false;

  char buf[CMD_BUF_LEN];
  strncpy(buf, cmdText, sizeof(buf));
  buf[sizeof(buf) - 1] = '\0';

  char *saveptr = nullptr;
  char *name = strtok_r(buf, ",", &saveptr); // take text before first comma
  if (!name || !*name)
    return false;

  return findCommand(name) != nullptr;
}

bool dispatchCommand(const char *cmdText) { // dispatch command string
  if (!cmdText || !*cmdText)
    return false;

  char buf[CMD_BUF_LEN];
  strncpy(buf, cmdText, sizeof(buf));
  buf[sizeof(buf) - 1] = '\0';

  // Tokenize in-place
  const char *argv[MAX_ARGS];
  int argc = 0;

  char *saveptr = nullptr;
  char *name = strtok_r(buf, ",", &saveptr);
  if (!name || !*name)
    return false;

  // remaining tokens become argv[]
  while (argc < MAX_ARGS) {
    char *tok = strtok_r(nullptr, ",", &saveptr);
    if (!tok)
      break;
    // Optional: skip empty tokens
    if (*tok == '\0')
      continue;
    argv[argc++] = tok;
  }

  CmdFn fn = findCommand(name);
  if (!fn) {
    Serial.print(F("command not found: "));
    Serial.println(name);
    return false;
  }
  return fn(argc, argv); // Call the function with args
}


static bool serial_string_init() {
  Serial.print("{\"device_name\":\"PAR meter\",\"device_version\":\"1\",\"device_id\":\"esp32-c3\"");
  Serial.print("\",\"device_battery\":\"NaN\",\"device_firmware\":1.001,\"sample\":[{\"protocol_id\":\"NaN\",\"set\":[");
  return true;
}

static bool serial_string_end() {
  Serial.print("]}]}7A1E3AA1");
  Serial.print("\n");
  return true;
}

static bool HandleJson(const String &json) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json);

  bool handledAny = false;
  bool firstOut = true; // controls commas

  if (err) {
    Serial.print(F("JSON parse error: "));
    Serial.println(err.c_str());
    return false;
  }

  serial_string_init();

  auto processProtocolSet = [&](JsonArray proto) {
    for (JsonVariant setV : proto) {
      JsonObject setObj = setV.as<JsonObject>();
      if (setObj.isNull())
        continue;

      const char *cmd = setObj["label"]; // OPENJII ASK TO IMPLEMENT CMD rather than label
      if (!cmd)
        continue;

      uint16_t repeats = setObj["protocol_repeats"] | 1;
      if (repeats == 0)
        repeats = 1;

      for (uint16_t i = 0; i < repeats; i++) {
        if (!commandExists(cmd)) {
          Serial.print(F("command not found: "));
          Serial.println(cmd);
          continue;
        }

        // Print comma BEFORE the next dispatched command
        if (!firstOut)
          Serial.print(',');
        firstOut = false;

        if (dispatchCommand(cmd))
          handledAny = true;
      }
    }

    serial_string_end();
  };

  if (doc.is<JsonArray>()) {
    for (JsonVariant v : doc.as<JsonArray>()) {
      JsonObject obj = v.as<JsonObject>();
      if (obj.isNull())
        continue;
      JsonArray proto = obj["_protocol_set_"].as<JsonArray>();
      if (!proto.isNull())
        processProtocolSet(proto);
    }
  } else if (doc.is<JsonObject>()) {
    JsonArray proto = doc.as<JsonObject>()["_protocol_set_"].as<JsonArray>();
    if (!proto.isNull())
      processProtocolSet(proto);
  } else {
    return false;
  }

  return handledAny;
}





void handleCommandText(const String &cmd) {
  if (cmd == "hello") {
    Serial.println(F("MiniPAR,V1.1"));
    
  } else if (cmd == "battery") {
    Serial.println(
        F("\"battery\":\"NaN\"")); // Placeholder, implement actual battery reading if available

  } else if (cmd == "i2c_scan") {
    i2c_scan();

  } else if (cmd == "spec") {
    spectrometer_read();

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
    float par_raw =0;
    if (cmd_get_par_raw(&par_raw)) {
      Serial.println(par_raw);
    } else {
      Serial.println(F("error_getting_par_raw"));
    }

  } else if (cmd == "par") {
    float par = 0;
    if (cmd_get_par(&par)) {
      Serial.println(par);
    } else {
      Serial.println(F("error_getting_par"));
    } 
  
  } else if (cmd.startsWith("cal_par_slope")) {
    // Example command: cal_par_slope,0.5
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      const char *arg = cmd.c_str() + comma + 1;
      set_calibration_slope(1, &arg);
    } else {
      Serial.println(F("error_missing_calibration_args"));
    }

  } else if (cmd.startsWith("cal_par_intercept")) {
    // Example command: cal_par_intercept,0.2
    int comma = cmd.indexOf(',');
    if (comma > 0) {
      const char *arg = cmd.c_str() + comma + 1;
      set_calibration_intercept(1, &arg);
    } else {
      Serial.println(F("error_missing_calibration_args"));
    }
  } else if (cmd == "reboot") {
    cmd_reboot();

  } else if (cmd.length() > 0) {
    Serial.println(F("unknown command"));

  }
}
