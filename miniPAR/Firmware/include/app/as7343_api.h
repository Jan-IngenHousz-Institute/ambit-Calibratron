#pragma once

#include <Arduino.h>
#include "app/spectrometer_types.h"

bool initAS7343();
bool as7343_readChipId(uint8_t *chip_id);
bool as7343_readInto(SpectrometerResult *out);
uint16_t as7343_setLEDCurrent(uint16_t led_current_ma);
uint8_t  as7343_getAtIME();
bool     as7343_setAtIME(uint8_t atime);
uint16_t as7343_getAStep();
bool     as7343_setAStep(uint16_t astep);
uint8_t  as7343_getGain();
bool     as7343_setGain(uint8_t gain);
