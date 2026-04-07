#include <Arduino.h>
#include <Wire.h>

#include "app/as7343_api.h"

// AS7343 bringup mode: populates channels[0..17] with raw DATA_0..DATA_17 so
// real hardware can confirm the DATA_n -> semantic band mapping under known
// illumination.  DELETE this define and every #ifdef block once the mapping is
// confirmed and the semantic table is frozen.
#define AS7343_BRINGUP_RAW_DUMP

namespace {

// ---------------------------------------------------------------------------
// I2C address and identity
// ---------------------------------------------------------------------------
constexpr uint8_t kAs7343I2cAddress     = 0x39;
constexpr uint8_t kAs7343Cfg0Register   = 0xBF;  // bank-select; verified Phase 0
constexpr uint8_t kAs7343RegBankBitMask = 0x10;   // bit 4 = REG_BANK
constexpr uint8_t kAs7343ChipIdRegister = 0x5A;   // bank 1
constexpr uint8_t kAs7343ChipId         = 0x81;

// ---------------------------------------------------------------------------
// Control registers (bank 0)
// ---------------------------------------------------------------------------
constexpr uint8_t kAs7343Enable  = 0x80;  // PON=bit0, SP_EN=bit1, FDEN=bit6
constexpr uint8_t kAs7343Atime   = 0x81;
constexpr uint8_t kAs7343Status  = 0x93;  // self-clear by write-back
constexpr uint8_t kAs7343Status2 = 0x90;  // AVALID=bit6 (hardware verified)
constexpr uint8_t kAs7343Astatus = 0x94;  // read to latch spectral data; ASAT=bit7
// DATA_0_L = 0x95, hardware-confirmed.
// (0x94=ASTATUS, 0x95=DATA_0_L, ..., 0xB4=DATA_17_H)
// The Adafruit .cpp comment says 0x96 but the register map shows 0x95.
// Original burst-from-0x94 code validated this: buf[1]=0x95 gave correct values.
constexpr uint8_t kAs7343Data0L  = 0x95;
constexpr uint8_t kAs7343Cfg1    = 0xC6;  // GAIN field bits[4:0]
constexpr uint8_t kAs7343Led     = 0xCD;  // LED_ACT=bit7, LED_DRIVE=bits[6:0]
constexpr uint8_t kAs7343AstepL  = 0xD4;
constexpr uint8_t kAs7343AstepH  = 0xD5;

// CFG20 (0xD6): AUTO_SMUX at bits[6:5]
// Hardware confirmed address from register scan.
// Bit positions confirmed from Adafruit AS7343 library: bits[6:5], 2-bit field.
// value 0 = 6CH, value 1 = 12CH, value 3 = 18CH
constexpr uint8_t kAs7343Cfg20        = 0xD6;
constexpr uint8_t kAs7343AutoSmuxMask = 0x60;  // bits[6:5]
constexpr uint8_t kAs7343AutoSmux18   = 0x60;  // bits[6:5]=11 = 18-channel mode

// Bit masks
constexpr uint8_t kAs7343PonBit      = 0x01;
constexpr uint8_t kAs7343SpEnBit     = 0x02;
constexpr uint8_t kAs7343AvalidBit   = 0x40;  // bit 6 of STATUS2; hardware verified
constexpr uint8_t kAs7343AsatAnalog  = 0x04;  // bit 2 of STATUS2; verified 0x44=AVALID|ASAT_ANA
constexpr uint8_t kAs7343AsatDigital = 0x08;  // bit 3 of STATUS2
constexpr uint8_t kAs7343AstatusAsat = 0x80;  // bit 7 of ASTATUS

// Default configuration (low gain for bringup; raise after band mapping confirmed)
constexpr uint8_t  kAs7343DefaultAtime = 29;
constexpr uint16_t kAs7343DefaultAstep = 599;
constexpr uint8_t  kAs7343DefaultGain  = 1;   // 1x

// Burst: 18 channels × 2 bytes starting from DATA_0_L (ASTATUS read separately)
constexpr uint8_t kBurstLen = 36;

// ---------------------------------------------------------------------------
// Stored configuration
// ---------------------------------------------------------------------------
uint8_t  s_atime = kAs7343DefaultAtime;
uint16_t s_astep = kAs7343DefaultAstep;
uint8_t  s_gain  = kAs7343DefaultGain;

// ---------------------------------------------------------------------------
// Low-level I2C helpers
// ---------------------------------------------------------------------------
bool writeRegister8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readRegister8(uint8_t reg, uint8_t *value) {
  if (!value) return false;
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(static_cast<int>(kAs7343I2cAddress), 1) != 1) return false;
  *value = Wire.read();
  return true;
}

bool setRegisterBank1(uint8_t *original_cfg0) {
  if (!original_cfg0) return false;
  if (!readRegister8(kAs7343Cfg0Register, original_cfg0)) return false;
  const uint8_t bank1_cfg0 = *original_cfg0 | kAs7343RegBankBitMask;
  if (bank1_cfg0 == *original_cfg0) return true;
  return writeRegister8(kAs7343Cfg0Register, bank1_cfg0);
}

bool restoreRegisterBank(uint8_t original_cfg0) {
  return writeRegister8(kAs7343Cfg0Register, original_cfg0);
}

bool readBank1Register8(uint8_t reg, uint8_t *value) {
  uint8_t original_cfg0 = 0;
  if (!setRegisterBank1(&original_cfg0)) return false;
  const bool ok = readRegister8(reg, value);
  const bool restore_ok = restoreRegisterBank(original_cfg0);
  return ok && restore_ok;
}

} // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool as7343_readChipId(uint8_t *chip_id) {
  return readBank1Register8(kAs7343ChipIdRegister, chip_id);
}

bool initAS7343() {
  uint8_t chip_id = 0;
  if (!as7343_readChipId(&chip_id) || chip_id != kAs7343ChipId) {
    return false;
  }

  // Power on: PON=1, SP_EN=0, FDEN=0 (flicker detection disabled)
  if (!writeRegister8(kAs7343Enable, kAs7343PonBit)) return false;
  delay(2);  // AS7343 power-on oscillator stabilization (2 ms per datasheet)

  if (!writeRegister8(kAs7343Atime, s_atime)) return false;
  if (!writeRegister8(kAs7343AstepL, static_cast<uint8_t>(s_astep & 0xFF))) return false;
  if (!writeRegister8(kAs7343AstepH, static_cast<uint8_t>(s_astep >> 8)))   return false;
  if (!writeRegister8(kAs7343Cfg1,   s_gain)) return false;

  // Configure AUTO_SMUX=3 (18-channel) in CFG20 bits[6:5]
  // Confirmed: kAs7343Cfg20=0xD6, bits[6:5] from Adafruit AS7343 library source
  {
    uint8_t cfg20 = 0;
    if (!readRegister8(kAs7343Cfg20, &cfg20)) return false;
    const uint8_t new_cfg20 = (cfg20 & ~kAs7343AutoSmuxMask) | kAs7343AutoSmux18;
    if (!writeRegister8(kAs7343Cfg20, new_cfg20)) return false;
    uint8_t readback = 0;
    readRegister8(kAs7343Cfg20, &readback);
    Serial.print(F("[as7343-init] CFG20(0xD6) was=0x")); Serial.print(cfg20, HEX);
    Serial.print(F(" wrote=0x")); Serial.print(new_cfg20, HEX);
    Serial.print(F(" readback=0x")); Serial.println(readback, HEX);
  }

  return true;
}

bool as7343_readInto(SpectrometerResult *out) {
  if (!out) return false;

  // Read ENABLE; ensure SP_EN is clear before starting a fresh measurement
  uint8_t enable_val = 0;
  if (!readRegister8(kAs7343Enable, &enable_val)) return false;
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);

  // Clear stale STATUS flags (self-clear by write-back)
  uint8_t status_val = 0;
  if (readRegister8(kAs7343Status, &status_val)) {
    writeRegister8(kAs7343Status, status_val);
  }

  // Read ASTATUS to clear any stale data latch
  uint8_t astatus_stale = 0;
  readRegister8(kAs7343Astatus, &astatus_stale);

  // Start measurement: set SP_EN=1
  if (!writeRegister8(kAs7343Enable, enable_val | kAs7343SpEnBit)) return false;

  // Poll AVALID (STATUS2 bit 6) with 1000 ms timeout.
  // AVALID fires once after all 18-channel cycles complete.
  const uint32_t deadline = millis() + 1000ul;
  bool avalid = false;
  uint8_t status2 = 0;
  while (millis() < deadline) {
    if (readRegister8(kAs7343Status2, &status2) && (status2 & kAs7343AvalidBit)) {
      avalid = true;
      break;
    }
    delay(1);
  }
  #if DEBUG
  Serial.print(F("[as7343-debug] STATUS2=0x")); Serial.print(status2, HEX);
  Serial.println(avalid ? F(" AVALID") : F(" TIMEOUT"));
  #endif

  if (!avalid) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }

  // Read ASTATUS to latch spectral data (required before burst read)
  uint8_t astatus_val = 0;
  readRegister8(kAs7343Astatus, &astatus_val);

  // Burst read: DATA_0_L (0x96) through DATA_17_H = 36 bytes
  // SP_EN remains set during burst — clearing it first resets DATA registers
  Wire.beginTransmission(kAs7343I2cAddress);
  Wire.write(kAs7343Data0L);
  if (Wire.endTransmission(false) != 0) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }
  if (Wire.requestFrom(static_cast<int>(kAs7343I2cAddress),
                       static_cast<int>(kBurstLen)) != kBurstLen) {
    writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);
    return false;
  }
  uint8_t buf[kBurstLen];
  for (uint8_t i = 0; i < kBurstLen; i++) {
    buf[i] = Wire.read();
  }

  // Safe to stop measurement now
  writeRegister8(kAs7343Enable, enable_val & ~kAs7343SpEnBit);

  const bool saturated = (astatus_val & kAs7343AstatusAsat) != 0;
  out->model    = SpectrometerModel::AS7343;
  out->sat_mask = saturated ? 0xFFFF : 0;

#ifdef AS7343_BRINGUP_RAW_DUMP
  // Bringup mode: 18 channels in Adafruit DATA register order.
  // channel_count=18 triggers the bringup name table in the facade.
  // DELETE after band mapping confirmed from hardware evidence.
  //
  // DATA order (from Adafruit AS7343 header as7343_channel_t enum):
  //  [0]=FZ/450  [1]=FY/555  [2]=FXL/600 [3]=NIR/855
  //  [4]=VIS_TL_0           [5]=VIS_BR_0
  //  [6]=F2/425  [7]=F3/475  [8]=F4/515  [9]=F6/640
  //  [10]=VIS_TL_1          [11]=VIS_BR_1
  //  [12]=F1/405 [13]=F7/690 [14]=F8/745 [15]=F5/550
  //  [16]=VIS_TL_2          [17]=VIS_BR_2
  out->channel_count = 18;
  for (uint8_t i = 0; i < 18; i++) {
    out->channels[i] = static_cast<uint16_t>(buf[i * 2]) |
                       (static_cast<uint16_t>(buf[i * 2 + 1]) << 8);
  }
#else
  // Production: 13 semantic channels from confirmed DATA_n mapping
  // TODO: fill from bring-up evidence
  out->channel_count = 13;
  for (uint8_t i = 0; i < 13; i++) {
    out->channels[i] = 0xFFFF;  // sentinel: mapping not confirmed
  }
#endif

  return true;
}

// Returns quantized actual LED current in mA, 0 if disabled, 0xFFFF on error.
// TODO: verify AS7343 LED register format against datasheet.
uint16_t as7343_setLEDCurrent(uint16_t led_current_ma) {
  if (led_current_ma == 0) {
    uint8_t reg = 0;
    if (!readRegister8(kAs7343Led, &reg)) return 0xFFFF;
    if (!writeRegister8(kAs7343Led, reg & ~0x80u)) return 0xFFFF;
    return 0;
  }
  uint16_t normalized = led_current_ma < 4 ? 4 : led_current_ma;
  normalized = 4 + (((normalized - 4) / 2) * 2);
  const uint8_t drive = static_cast<uint8_t>((normalized - 4) / 2);
  if (!writeRegister8(kAs7343Led, static_cast<uint8_t>(0x80u | (drive & 0x7Fu)))) {
    return 0xFFFF;
  }
  return normalized;
}

uint8_t  as7343_getAtIME() { return s_atime; }
uint16_t as7343_getAStep() { return s_astep; }
uint8_t  as7343_getGain()  { return s_gain;  }

bool as7343_setAtIME(uint8_t atime) {
  if (!writeRegister8(kAs7343Atime, atime)) return false;
  s_atime = atime;
  return true;
}

bool as7343_setAStep(uint16_t astep) {
  if (!writeRegister8(kAs7343AstepL, static_cast<uint8_t>(astep & 0xFF))) return false;
  if (!writeRegister8(kAs7343AstepH, static_cast<uint8_t>(astep >> 8)))   return false;
  s_astep = astep;
  return true;
}

bool as7343_setGain(uint8_t gain) {
  if (!writeRegister8(kAs7343Cfg1, gain)) return false;
  s_gain = gain;
  return true;
}
