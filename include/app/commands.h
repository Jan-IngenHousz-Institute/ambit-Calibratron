
#pragma once

#include <Arduino.h>

bool dispatchCommand(const char *cmdText);

void handleCommandText(const String &cmd);
