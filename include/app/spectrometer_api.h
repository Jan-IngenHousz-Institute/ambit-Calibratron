#pragma once

#include <Arduino.h>
#include <stdint.h>
#include "app/spectrometer_types.h"

extern bool spectrometer_available;
extern SpectrometerModel spectrometer_model;

const char *spectrometerModelName(SpectrometerModel model);
bool initSpectrometer();
bool spectrometerPrepareLegacyCommand();
bool spectrometer_read();
bool spectrometer_set_led_current(uint16_t led_current_ma);
bool spectrometer_read_flash(uint16_t led_current_ma);
void spectrometerPrintNotAvailableError();
void spectrometerPrintUnsupportedDeviceError();
void spectrometerPrintNotYetImplementedError();
void cmd_spectrometer_status();
void cmd_spectrometer_set_atime(int argc, const char *argv[]);
void cmd_spectrometer_set_astep(int argc, const char *argv[]);
void cmd_spectrometer_set_gain(int argc, const char *argv[]);
