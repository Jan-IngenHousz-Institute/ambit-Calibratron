#include <Wire.h>
#include <Adafruit_AS7341.h>
#include "app/as7341_api.h"

namespace {

constexpr uint8_t kAs7341I2cAddress  = 0x39;
constexpr uint8_t kAs7341WhoamiReg   = 0x92;
// Chip ID check: (raw & 0xFC) == (0x09 << 2) == 0x24
constexpr uint8_t kAs7341ChipIdMask    = 0xFC;
constexpr uint8_t kAs7341ChipIdMasked  = 0x24;

// Adafruit channel index -> SpectrometerResult.channels[] index mapping.
// Indices 4 and 5 of the Adafruit array are SMUX pass-1 intermediates and must
// be skipped.  The 10 remaining channels map in order:
//   Adafruit[0..3]  -> channels[0..3]  (f1_415..f4_515)
//   Adafruit[6..11] -> channels[4..9]  (f5_555..nir)
constexpr uint8_t kAdafruitLen = 12;
constexpr uint8_t kResultLen   = 10;

} // namespace

static Adafruit_AS7341 as7341;

bool initAS7341() {
  if (!as7341.begin()) {
    return false;
  }
  as7341.setATIME(100);
  as7341.setASTEP(999);
  as7341.setGain(AS7341_GAIN_16X);
  return true;
}

bool as7341_readAndValidateChipId(uint8_t *raw_out) {
  Wire.beginTransmission(kAs7341I2cAddress);
  Wire.write(kAs7341WhoamiReg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7341I2cAddress), 1) != 1) {
    return false;
  }
  const uint8_t raw = Wire.read();
  if (raw_out) {
    *raw_out = raw;
  }
  return (raw & kAs7341ChipIdMask) == kAs7341ChipIdMasked;
}

bool as7341_readInto(SpectrometerResult *out) {
  if (!out) {
    return false;
  }
  uint16_t raw[kAdafruitLen];
  if (!as7341.readAllChannels(raw)) {
    return false;
  }
  out->model         = SpectrometerModel::AS7341;
  out->channel_count = kResultLen;
  out->sat_mask      = 0;
  // First SMUX pass: Adafruit[0..3] -> channels[0..3]
  out->channels[0] = raw[0];
  out->channels[1] = raw[1];
  out->channels[2] = raw[2];
  out->channels[3] = raw[3];
  // raw[4] and raw[5] are SMUX pass-1 clear/NIR intermediates — skipped
  // Second SMUX pass: Adafruit[6..11] -> channels[4..9]
  out->channels[4] = raw[6];
  out->channels[5] = raw[7];
  out->channels[6] = raw[8];
  out->channels[7] = raw[9];
  out->channels[8] = raw[10];
  out->channels[9] = raw[11];
  return true;
}

uint8_t as7341_setAtIME(uint8_t atime_value) {
  return as7341.setATIME(atime_value);
}

uint8_t as7341_getAtIME() {
  return as7341.getATIME();
}

uint16_t as7341_setAStep(uint16_t astep_value) {
  return as7341.setASTEP(astep_value);
}

uint16_t as7341_getAStep() {
  return as7341.getASTEP();
}

bool as7341_setGain(as7341_gain_t gain) {
  if (!as7341.setGain(gain)) {
    return false;
  }
  return as7341.getGain() == gain;
}

uint8_t as7341_getGain() {
  return static_cast<uint8_t>(as7341.getGain());
}

// Returns the quantized actual LED current in mA, 0 if disabled, 0xFFFF on error.
uint16_t as7341_setLEDCurrent(uint16_t led_current_ma) {
  if (led_current_ma == 0) {
    as7341.enableLED(false);
    return 0;
  }

  if (!as7341.setLEDCurrent(led_current_ma)) {
    return 0xFFFF;
  }

  // Quantize to device resolution: 4 mA minimum, 2 mA steps.
  uint16_t normalized = led_current_ma < 4 ? 4 : led_current_ma;
  normalized = 4 + (((normalized - 4) / 2) * 2);

  if (as7341.getLEDCurrent() != normalized) {
    return 0xFFFF;
  }

  as7341.enableLED(true);
  return normalized;
}
