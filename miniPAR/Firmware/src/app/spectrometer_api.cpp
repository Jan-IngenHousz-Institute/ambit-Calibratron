#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>

#include "app/as7341_api.h"
#include "app/as7343_api.h"
#include "app/spectrometer_api.h"

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
bool spectrometer_available = false;
SpectrometerModel spectrometer_model = SpectrometerModel::None;

// Transitional shim: keep as7341_available for the Phase 2 if-else command path.
// Removed in Phase 3 once the dispatch table takes over.
bool as7341_available = false;

// ---------------------------------------------------------------------------
// Board-level LED policy
// ---------------------------------------------------------------------------
constexpr uint16_t kSpectrometerLedBoardMaxMa = 20;
constexpr uint16_t kLedSetFailed = 0xFFFF;

// ---------------------------------------------------------------------------
// Channel name tables
// ---------------------------------------------------------------------------
static const char * const kAs7341ChannelNames[10] = {
  "f1_415", "f2_445", "f3_480", "f4_515",
  "f5_555", "f6_590", "f7_630", "f8_680",
  "clear",  "nir"
};

// AS7343 bringup names — 18 entries matching Adafruit as7343_channel_t DATA register order.
// Used when channel_count == 18 (AS7343_BRINGUP_RAW_DUMP active).
// DELETE together with the bringup path once the semantic table is frozen.
static const char * const kAs7343BringupNames[18] = {
  "fz_450",    // DATA[0]  FZ  450nm
  "fy_555",    // DATA[1]  FY  555nm
  "fxl_600",   // DATA[2]  FXL 600nm
  "nir",       // DATA[3]  NIR 855nm
  "vis_tl_0",  // DATA[4]  VIS clear cycle 1 top-left
  "vis_br_0",  // DATA[5]  VIS clear cycle 1 both-right
  "f2_425",    // DATA[6]  F2  425nm
  "f3_475",    // DATA[7]  F3  475nm
  "f4_515",    // DATA[8]  F4  515nm
  "f6_640",    // DATA[9]  F6  640nm
  "vis_tl_1",  // DATA[10] VIS clear cycle 2 top-left
  "vis_br_1",  // DATA[11] VIS clear cycle 2 both-right
  "f1_405",    // DATA[12] F1  405nm
  "f7_690",    // DATA[13] F7  690nm
  "f8_745",    // DATA[14] F8  745nm
  "f5_550",    // DATA[15] F5  550nm
  "vis_tl_2",  // DATA[16] VIS clear cycle 3 top-left
  "vis_br_2",  // DATA[17] VIS clear cycle 3 both-right
};

// AS7343 semantic names — 13 entries for production mode (channel_count == 13).
// Channels sorted wavelength-ascending; clear derived from VIS averages; nir last.
// TODO: confirm this ordering from bring-up evidence before switching off bringup mode.
static const char * const kAs7343ChannelNames[13] = {
  "f1_405",   // F1  405nm
  "f2_425",   // F2  425nm
  "fz_450",   // FZ  450nm
  "f3_475",   // F3  475nm
  "f4_515",   // F4  515nm
  "f5_550",   // F5  550nm
  "fy_555",   // FY  555nm
  "fxl_600",  // FXL 600nm
  "f6_640",   // F6  640nm
  "f7_690",   // F7  690nm
  "f8_745",   // F8  745nm
  "clear",    // derived from VIS averages
  "nir",      // NIR 855nm
};


//float par_coefficients[18] = {0}; // per-channel PAR conversion coefficients, indexed 0..channel_count-1. 18 = AS7343 bringup max
// default values for AS7341 from datasheet
float par_coefficients[18] = {
  1.0/55.0, // F1
  1.0/110.0, // F2
  1.0/210.0, // F3
  1.0/390.0, // F4
  1.0/590.0, // F5
  1.0/840.0, // F6
  1.0/1350.0, // F7
  1.0/1070.0, // F8
  1.0/1750.0,  // clear
  1.0/112.0,  // nir
  0,0,0,0,0,0,0,0}; // per-channel PAR conversion coefficients, indexed 0..channel_count-1. 18 = AS7343 bringup max

  Preferences preferences; // for storing persistent settings like PAR coefficients

float slope = 0;
float intercept = 0;
char dev_name[20] = "NoName";

void loadpref() {
  preferences.begin("par_coeffs", true);
  
  // for a future phase: if we want to support per-channel PAR coefficients, we can store them in preferences with keys like "ch0", "ch1", etc. For now, just store slope and intercept for a single linear conversion. 
  //for (int i = 0; i < 18; i++) {
  //  char key[6];
  //  snprintf(key, sizeof(key), "ch%d", i);
  //  par_coefficients[i] = preferences.getFloat(key, par_coefficients[i], 0.0);
  //}
  
  slope = preferences.getFloat("slope", 1.0f);
  intercept = preferences.getFloat("intercept", 0.0f);
  if(preferences.isKey("name"))
  {
    preferences.getString("name", dev_name, 20);
    dev_name[sizeof(dev_name) - 1] = '\0';
  } 
  preferences.end();
}

namespace {

// ---------------------------------------------------------------------------
// Detection helpers
// ---------------------------------------------------------------------------
constexpr uint8_t  kSpectrometerI2cAddress    = 0x39;
constexpr uint32_t kProbeRetryWindowUs         = 5000;
constexpr uint32_t kProbeRetryIntervalUs        = 250;
constexpr bool     kSpectrometerDetectionDebug  = true;

struct DetectionResult {
  SpectrometerModel model = SpectrometerModel::None;
  bool saw_ack = false;
};

struct DetectionDebugInfo {
  uint32_t attempts = 0;
  bool saw_ack = false;
  bool as7343_id_read_ok = false;
  uint8_t as7343_chip_id = 0;
  bool as7341_id_read_ok = false;
  uint8_t as7341_chip_id = 0;
};

void syncLegacyAvailability() {
  as7341_available =
      spectrometer_available && spectrometer_model == SpectrometerModel::AS7341;
}

void setSpectrometerState(SpectrometerModel model, bool available) {
  spectrometer_model    = model;
  spectrometer_available = available;
  syncLegacyAvailability();
}

bool spectrometerAddressAcks() {
  Wire.beginTransmission(kSpectrometerI2cAddress);
  return Wire.endTransmission() == 0;
}

void printHexByte(uint8_t value) {
  if (value < 0x10) Serial.print('0');
  Serial.print(value, HEX);
}

void printDetectionDebug(const char *reason, const DetectionDebugInfo &debug,
                         SpectrometerModel result_model, bool initialized) {
  if (!kSpectrometerDetectionDebug) return;
  Serial.print(F("[spectrometer-debug] reason="));
  Serial.print(reason);
  Serial.print(F(" attempts="));
  Serial.print(debug.attempts);
  Serial.print(F(" ack="));
  Serial.print(debug.saw_ack ? F("1") : F("0"));
  Serial.print(F(" as7343_id_ok="));
  Serial.print(debug.as7343_id_read_ok ? F("1") : F("0"));
  Serial.print(F(" as7343_id=0x"));
  printHexByte(debug.as7343_chip_id);
  Serial.print(F(" as7341_id_ok="));
  Serial.print(debug.as7341_id_read_ok ? F("1") : F("0"));
  Serial.print(F(" as7341_id=0x"));
  printHexByte(debug.as7341_chip_id);
  Serial.print(F(" result="));
  Serial.print(spectrometerModelName(result_model));
  Serial.print(F(" init_ok="));
  Serial.println(initialized ? F("1") : F("0"));
}

DetectionResult detectSpectrometerWithinRetryWindow(DetectionDebugInfo *debug) {
  DetectionResult result;
  const uint32_t started_at = micros();

  while ((micros() - started_at) < kProbeRetryWindowUs) {
    if (debug) debug->attempts++;

    if (spectrometerAddressAcks()) {
      result.saw_ack = true;
      if (debug) debug->saw_ack = true;

      // Probe AS7343 first (bank-switch write to 0xBF is safe on AS7341)
      uint8_t as7343_chip_id = 0;
      const bool as7343_read_ok = as7343_readChipId(&as7343_chip_id);
      if (debug) {
        debug->as7343_id_read_ok = as7343_read_ok;
        if (as7343_read_ok) debug->as7343_chip_id = as7343_chip_id;
      }
      if (as7343_read_ok && as7343_chip_id == 0x81) {
        result.model = SpectrometerModel::AS7343;
        return result;
      }

      // Probe AS7341
      uint8_t as7341_chip_id = 0;
      const bool as7341_matched = as7341_readAndValidateChipId(&as7341_chip_id);
      if (debug) {
        debug->as7341_id_read_ok = as7341_matched;
        debug->as7341_chip_id    = as7341_chip_id;
      }
      if (as7341_matched) {
        result.model = SpectrometerModel::AS7341;
        return result;
      }
    }

    delayMicroseconds(kProbeRetryIntervalUs);
  }

  result.model = result.saw_ack ? SpectrometerModel::ProbePendingAt0x39
                                : SpectrometerModel::None;
  return result;
}

bool initializeDetectedSpectrometer(SpectrometerModel model) {
  switch (model) {
  case SpectrometerModel::AS7341: return initAS7341();
  case SpectrometerModel::AS7343: return initAS7343();
  default: return false;
  }
}

bool detectAndInitialize(bool promote_pending_to_unknown, const char *reason) {
  DetectionDebugInfo debug;
  const DetectionResult detection = detectSpectrometerWithinRetryWindow(&debug);

  if (detection.model == SpectrometerModel::AS7341 ||
      detection.model == SpectrometerModel::AS7343) {
    const bool initialized = initializeDetectedSpectrometer(detection.model);
    setSpectrometerState(detection.model, initialized);
    printDetectionDebug(reason, debug, spectrometer_model, initialized);
    return initialized;
  }

  if (promote_pending_to_unknown &&
      detection.model == SpectrometerModel::ProbePendingAt0x39) {
    setSpectrometerState(SpectrometerModel::UnknownAt0x39, false);
    printDetectionDebug(reason, debug, spectrometer_model, false);
    return false;
  }

  setSpectrometerState(detection.model, false);
  printDetectionDebug(reason, debug, spectrometer_model, false);
  return false;
}

// ---------------------------------------------------------------------------
// Internal measurement helpers
// ---------------------------------------------------------------------------

// Populates *out from the active backend.  No printing, no guard checks.
bool spectrometerReadInto(SpectrometerResult *out) {
  if (!out) return false;
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_readInto(out);
  case SpectrometerModel::AS7343: return as7343_readInto(out);
  default: return false;
  }
}

// Sets LED current on the active backend, bypassing facade JSON output.
// Returns quantized actual mA, 0 if disabled, 0xFFFF on error.
uint16_t spectrometerSetLedCurrentSilent(uint16_t led_current_ma) {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_setLEDCurrent(led_current_ma);
  case SpectrometerModel::AS7343: return as7343_setLEDCurrent(led_current_ma);
  default: return kLedSetFailed;
  }
}

uint8_t spectrometerGetAtIME() {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_getAtIME();
  case SpectrometerModel::AS7343: return as7343_getAtIME();
  default: return 100;
  }
}

uint16_t spectrometerGetAStep() {
  switch (spectrometer_model) {
  case SpectrometerModel::AS7341: return as7341_getAStep();
  case SpectrometerModel::AS7343: return as7343_getAStep();
  default: return 999;
  }
}

// Settling delay for LED stabilisation before a lit read.
// Formula: ceil((ATIME+1)*(ASTEP+1)*2.78 µs / 1000) + 2 ms, floor 5 ms.
// Uses integer arithmetic to avoid float and uint16_t overflow.
uint32_t computeSettlingDelayMs(uint8_t atime, uint16_t astep) {
  const uint32_t t_ms =
      ((uint32_t)(atime + 1) * (uint32_t)(astep + 1) * 278ul + 99999ul) / 100000ul + 2ul;
  return t_ms < 5u ? 5u : t_ms;
}

// Resolves the channel name table and whether to use indexed names ("d0"…)
// for the given result (bringup mode / unexpected count).
const char * const *resolveChannelNames(const SpectrometerResult &r, bool *use_index_names) {
  *use_index_names = false;
  if (r.model == SpectrometerModel::AS7341 && r.channel_count == 10) {
    return kAs7341ChannelNames;
  }
  if (r.model == SpectrometerModel::AS7343 && r.channel_count == 13) {
    return kAs7343ChannelNames;
  }
  if (r.model == SpectrometerModel::AS7343 && r.channel_count == 18) {
    return kAs7343BringupNames;
  }
  // Unexpected channel count — fall back to indexed names
  *use_index_names = true;
  return nullptr;
}

// Prints {"model":"...","channels":{...}} — the inner object used by both the
// single-read and the flash-read (dark/lit/diff) output paths.
void printChannelsObject(const SpectrometerResult &r) {
  bool use_index_names = false;
  const char * const *names = resolveChannelNames(r, &use_index_names);

  Serial.print(F("{\"model\":\""));
  Serial.print(spectrometerModelName(r.model));
  Serial.print(F("\",\"channels\":{"));
  for (uint8_t i = 0; i < r.channel_count; i++) {
    if (i > 0) Serial.print(',');
    Serial.print('"');
    if (use_index_names) {
      Serial.print('d');
      Serial.print(i);
    } else {
      Serial.print(names[i]);
    }
    Serial.print(F("\":"));
    Serial.print(r.channels[i]);
  }
  Serial.print(F("}}"));
}

} // namespace

// ---------------------------------------------------------------------------
// Public API — init and error printers
// ---------------------------------------------------------------------------

const char *spectrometerModelName(SpectrometerModel model) {
  switch (model) {
  case SpectrometerModel::AS7341:            return "AS7341";
  case SpectrometerModel::AS7343:            return "AS7343";
  case SpectrometerModel::ProbePendingAt0x39: return "ProbePendingAt0x39";
  case SpectrometerModel::UnknownAt0x39:     return "UnknownAt0x39";
  case SpectrometerModel::None:
  default:                                   return "None";
  }
}

bool initSpectrometer() {
  const bool initialized = detectAndInitialize(false, "boot");

  switch (spectrometer_model) {
  case SpectrometerModel::AS7341:
    Serial.println(F("Spectrometer detected: AS7341"));
    break;
  case SpectrometerModel::AS7343:
    Serial.println(F("Spectrometer detected: AS7343"));
    break;
  case SpectrometerModel::ProbePendingAt0x39:
    Serial.println(F("Unidentified spectrometer at 0x39, will retry on first command"));
    break;
  case SpectrometerModel::None:
  case SpectrometerModel::UnknownAt0x39:
  default:
    Serial.println(F("No spectrometer detected at 0x39"));
    break;
  }
  return initialized;
}

void spectrometerPrintNotAvailableError() {
  Serial.print(F("{\"spectrometer\":{\"error\":\"not_available\"}}"));
}

void spectrometerPrintUnsupportedDeviceError() {
  Serial.print(F("{\"spectrometer\":{\"error\":\"unsupported_device_at_0x39\"}}"));
}

void spectrometerPrintNotYetImplementedError() {
  Serial.print(F("{\"spectrometer\":{\"error\":\"not_yet_implemented\"}}"));
}

bool spectrometerPrepareLegacyCommand() {
  if (spectrometer_model == SpectrometerModel::ProbePendingAt0x39) {
    detectAndInitialize(true, "legacy_cmd_retry");
  }
  if (spectrometer_model == SpectrometerModel::UnknownAt0x39) {
    spectrometerPrintUnsupportedDeviceError();
    return false;
  }
  if (!spectrometer_available) {
    spectrometerPrintNotAvailableError();
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Public API — spectrometer commands
// ---------------------------------------------------------------------------

bool spectrometer_read_raw() {
  if (!spectrometerPrepareLegacyCommand()) return false;

  SpectrometerResult result;
  if (!spectrometerReadInto(&result)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"read_failed\"}}"));
    return false;
  }

  Serial.print(F("{\"spectrometer\":"));
  printChannelsObject(result);
  Serial.print(F("}"));
  return true;
}

bool spectrometer_read() {
  if (!spectrometerPrepareLegacyCommand()) return false;

  SpectrometerResult result;
  if (!spectrometerReadInto(&result)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"read_failed\"}}"));
    return false;
  }

  // Apply per-channel sensitivity correction
  bool use_index_names = false;
  const char * const *names = resolveChannelNames(result, &use_index_names);

  Serial.print(F("{\"spectrometer\":{\"model\":\""));
  Serial.print(spectrometerModelName(result.model));
  Serial.print(F("\",\"channels\":{"));
  for (uint8_t i = 0; i < result.channel_count; i++) {
    if (i > 0) Serial.print(',');
    Serial.print('"');
    if (use_index_names) {
      Serial.print('d');
      Serial.print(i);
    } else {
      Serial.print(names[i]);
    }
    Serial.print(F("\":"));
    float corrected = (float)result.channels[i] * par_coefficients[i];
    Serial.print(corrected, 4);
  }
  Serial.print(F("}}}"));
  return true;
}

bool spectrometer_set_led_current(uint16_t led_current_ma) {
  if (!spectrometerPrepareLegacyCommand()) return false;

  // Silent clamp to board maximum
  if (led_current_ma > kSpectrometerLedBoardMaxMa) {
    led_current_ma = kSpectrometerLedBoardMaxMa;
  }

  const uint16_t actual_ma = spectrometerSetLedCurrentSilent(led_current_ma);
  if (actual_ma == kLedSetFailed) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"led_set_failed\"}}"));
    return false;
  }

  Serial.print(F("{\"spectrometer\":{\"led_current_ma\":"));
  Serial.print(actual_ma);
  Serial.print(F("}}"));
  return true;
}

bool spectrometer_read_flash(uint16_t led_current_ma) {
  if (!spectrometerPrepareLegacyCommand()) return false;

  // Silent clamp to board maximum
  if (led_current_ma > kSpectrometerLedBoardMaxMa) {
    led_current_ma = kSpectrometerLedBoardMaxMa;
  }

  // Dark read
  SpectrometerResult dark, lit;
  if (!spectrometerReadInto(&dark)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"dark_read_failed\"}}"));
    return false;
  }

  // Enable LED and wait for settling
  const uint16_t actual_led_ma = spectrometerSetLedCurrentSilent(led_current_ma);
  if (actual_led_ma == kLedSetFailed) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"led_set_failed\"}}"));
    return false;
  }

  const uint8_t  atime     = spectrometerGetAtIME();
  const uint16_t astep     = spectrometerGetAStep();
  const uint32_t settle_ms = computeSettlingDelayMs(atime, astep);
  delay(settle_ms);

  // Lit read
  if (!spectrometerReadInto(&lit)) {
    spectrometerSetLedCurrentSilent(0);
    Serial.print(F("{\"spectrometer\":{\"error\":\"lit_read_failed\"}}"));
    return false;
  }

  // Disable LED
  spectrometerSetLedCurrentSilent(0);

  // Compute diff per channel
  SpectrometerResult diff;
  diff.model         = dark.model;
  diff.channel_count = dark.channel_count;
  diff.sat_mask      = dark.sat_mask | lit.sat_mask;
  for (uint8_t i = 0; i < dark.channel_count; i++) {
    diff.channels[i] =
        lit.channels[i] > dark.channels[i] ? lit.channels[i] - dark.channels[i] : 0;
  }

  // Single combined JSON object
  Serial.print(F("{\"spectrometer_dark\":"));
  printChannelsObject(dark);
  Serial.print(F(",\"spectrometer_lit\":"));
  printChannelsObject(lit);
  Serial.print(F(",\"spectrometer_diff\":"));
  printChannelsObject(diff);
  Serial.print(F("}"));
  return true;
}

void cmd_spectrometer_status() {
  Serial.print(F("{\"spectrometer_status\":{\"model\":\""));
  Serial.print(spectrometerModelName(spectrometer_model));
  Serial.print(F("\",\"available\":"));
  Serial.print(spectrometer_available ? F("true") : F("false"));

  if (spectrometer_available) {
    const uint8_t  atime = spectrometerGetAtIME();
    const uint16_t astep = spectrometerGetAStep();
    uint8_t gain = 0;
    if (spectrometer_model == SpectrometerModel::AS7341) {
      gain = as7341_getGain();
    } else if (spectrometer_model == SpectrometerModel::AS7343) {
      gain = as7343_getGain();
    }
    Serial.print(F(",\"atime\":"));
    Serial.print(atime);
    Serial.print(F(",\"astep\":"));
    Serial.print(astep);
    Serial.print(F(",\"gain\":"));
    Serial.print(gain);
  }

  Serial.print(F("}}"));
}

// ---------------------------------------------------------------------------
// Integration parameter setters
// Command syntax:  spectrometer_set_atime,<0-255>
//                  spectrometer_set_astep,<0-65534>
//                  spectrometer_set_gain,<0-12>   (AS7341 enum ordinal or AS7343 register value)
// Response: {"spectrometer_config":{"atime":N,"astep":N,"gain":N}} on success
//           {"spectrometer_config":{"error":"..."}} on failure
// ---------------------------------------------------------------------------

static void printConfigResponse() {
  Serial.print(F("{\"spectrometer_config\":{\"atime\":"));
  Serial.print(spectrometerGetAtIME());
  Serial.print(F(",\"astep\":"));
  Serial.print(spectrometerGetAStep());
  Serial.print(F(",\"gain\":"));
  uint8_t gain = 0;
  if (spectrometer_model == SpectrometerModel::AS7341) gain = as7341_getGain();
  else if (spectrometer_model == SpectrometerModel::AS7343) gain = as7343_getGain();
  Serial.print(gain);
  Serial.print(F("}}"));
}

void cmd_spectrometer_set_atime(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    return;
  }
  const int val = atoi(argv[0]);
  if (val < 0 || val > 255) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"atime_out_of_range\"}}"));
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = as7341_setAtIME(static_cast<uint8_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setAtIME(static_cast<uint8_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); return; }
  printConfigResponse();
}

void cmd_spectrometer_set_astep(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    return;
  }
  const long val = atol(argv[0]);
  if (val < 0 || val > 65534) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"astep_out_of_range\"}}"));
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = as7341_setAStep(static_cast<uint16_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setAStep(static_cast<uint16_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); return; }
  printConfigResponse();
}

void cmd_spectrometer_set_gain(int argc, const char *argv[]) {
  if (!spectrometerPrepareLegacyCommand()) return;
  if (argc < 1) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"missing_arg\"}}"));
    return;
  }
  const int val = atoi(argv[0]);
  // AS7341 gain enum: 0=0.5x, 1=1x, 2=2x, ..., 10=512x (10 max)
  // AS7343 gain register: 0=0.5x, 1=1x, ..., 12=2048x (12 max)
  const int max_gain = (spectrometer_model == SpectrometerModel::AS7341) ? 10 : 12;
  if (val < 0 || val > max_gain) {
    Serial.print(F("{\"spectrometer_config\":{\"error\":\"gain_out_of_range_0_"));
    Serial.print(max_gain);
    Serial.print(F("\"}}"));
    return;
  }
  bool ok = false;
  if (spectrometer_model == SpectrometerModel::AS7341)
    ok = as7341_setGain(static_cast<as7341_gain_t>(val));
  else if (spectrometer_model == SpectrometerModel::AS7343)
    ok = as7343_setGain(static_cast<uint8_t>(val));
  if (!ok) { Serial.print(F("{\"spectrometer_config\":{\"error\":\"set_failed\"}}")); return; }
  printConfigResponse();
}

bool cmd_get_par_raw(float *out_par)
{
  if (!spectrometerPrepareLegacyCommand()) return false;

  SpectrometerResult result;
  if (!spectrometerReadInto(&result)) {
    Serial.print(F("{\"spectrometer\":{\"error\":\"read_failed\"}}"));
    return false;
  }
  float par = 0;
  for (uint8_t i = 0; i < result.channel_count; i++)  
  {
    par += result.channels[i] * par_coefficients[i];
  }
  *out_par = par;
  return true;  
}

bool cmd_get_par(float *out_par)
{
  float par_raw = 0;
  if (!cmd_get_par_raw(&par_raw)) {
    return false;
  }
  // Apply any additional scaling or calibration to par_raw here if needed.
  *out_par = par_raw * slope + intercept;
  return true;
}

bool set_calibration_slope(int argc, const char *argv[])
{
  if (argc < 1 ) {
    Serial.print(F("{\"calibration\":{\"error\":\"missing_args\"}}"));
    return false;
  }
  slope = atof(argv[0]);
  //store in preferences for persistence across reboots
  preferences.begin("par_coeffs", false);
  preferences.putFloat("slope", slope);
  preferences.end();
  Serial.print(F("{\"calibration\":{\"slope\":"));
  Serial.print(slope);
  Serial.print(F("}}"));
  return true;
}




bool set_calibration_intercept(int argc, const char *argv[])
{
  if (argc < 1 ) {
    Serial.print(F("{\"calibration\":{\"error\":\"missing_args\"}}"));
    return false;
  }
  intercept = atof(argv[0]);
  // store in preferences for persistence across reboots
  preferences.begin("par_coeffs", false);
  preferences.putFloat("intercept", intercept);
  preferences.end();
  Serial.print(F("{\"calibration\":{\"intercept\":"));
  Serial.print(intercept);
  Serial.print(F("}}"));
  return true;
}

void cmd_set_dev_name(int argc, const char* name[])
{
    if (argc < 1 ) {
      Serial.print(F("{\"error\":\"missing_args\"}"));
      return;
    }
    strncpy(dev_name, name[0], sizeof(dev_name) - 1);
    dev_name[sizeof(dev_name) - 1] = '\0';
    preferences.begin("par_coeffs", false);
    preferences.putString("name", dev_name);
    preferences.end();

}

char* cmd_get_dev_name()
{
  return dev_name;
}