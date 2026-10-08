/*
 * Vinyl Processor front panel.
 *
 * Three toggle switches, each wired between a pin and ground, using the
 * internal pull-ups: closed reads LOW and means ON.
 *
 *   Pin 2 -> declicker
 *   Pin 3 -> denoiser
 *   Pin 4 -> monitor (hear only what is being removed)
 *
 * Sends one line per reading at 9600 baud:
 *   Switch 1: ON  | Switch 2: OFF | Switch 3: OFF
 *
 * The host applies only what changed, so repeating the current state is
 * harmless and makes the panel self-healing after a reconnect.
 */

const int SWITCH_PINS[3] = {2, 3, 4};
const unsigned long DEBOUNCE_MS = 25;
const unsigned long HEARTBEAT_MS = 1000;

bool state[3];
bool lastRead[3];
unsigned long lastChange[3];
unsigned long lastSend = 0;

void setup() {
  Serial.begin(9600);
  for (int i = 0; i < 3; i++) {
    pinMode(SWITCH_PINS[i], INPUT_PULLUP);
    state[i] = lastRead[i] = (digitalRead(SWITCH_PINS[i]) == LOW);
    lastChange[i] = 0;
  }
  sendState();
}

void sendState() {
  for (int i = 0; i < 3; i++) {
    Serial.print("Switch ");
    Serial.print(i + 1);
    Serial.print(": ");
    Serial.print(state[i] ? "ON " : "OFF");
    if (i < 2) Serial.print(" | ");
  }
  Serial.println();
  lastSend = millis();
}

void loop() {
  bool changed = false;
  unsigned long now = millis();

  for (int i = 0; i < 3; i++) {
    bool reading = (digitalRead(SWITCH_PINS[i]) == LOW);
    if (reading != lastRead[i]) {
      lastRead[i] = reading;
      lastChange[i] = now;
    } else if (reading != state[i] && (now - lastChange[i]) > DEBOUNCE_MS) {
      state[i] = reading;
      changed = true;
    }
  }

  // Send on change, and periodically so the host recovers after a reconnect.
  if (changed || (now - lastSend) > HEARTBEAT_MS) {
    sendState();
  }
}
