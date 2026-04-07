#pragma once
#include <stdint.h>

enum class SpectrometerModel : uint8_t {
  None,
  AS7341,
  AS7343,
  ProbePendingAt0x39,
  UnknownAt0x39,
};

struct SpectrometerResult {
  SpectrometerModel model;
  uint8_t  channel_count;  // number of valid entries in channels[]
  uint16_t channels[18];   // indexed 0..channel_count-1; 18 = AS7343 bringup max
  uint16_t sat_mask;       // bit N set => channels[N] is saturated or invalid
};
