"""
The physical layer - where the AI acts on and senses the real world.

Python can't touch GPIO or I2C directly on this board (App Lab containerizes
the Python side), so everything here goes through the Router Bridge RPC to
sketch/sketch.ino:

    Bridge.call("set_relay", channel, state)
    Bridge.call("lcd_show", line1, line2)

Three relay channels, each demonstrating a different sensing->acting link:
    CH_LAMP  (0)  presence      -> desk lamp
    CH_FAN   (1)  presence      -> fan          <- same signal/debounce as the lamp
    CH_ALERT (2)  poor posture  -> alert light

Design notes that matter in practice:

- HYSTERESIS everywhere. Presence flickers; temperature hovers around a
  threshold. Without a deadband the lamp strobes and the fan chatters on and
  off. Each controller below turns on decisively and releases slowly.
- EDGE-TRIGGERED. We only call across the Bridge when a value actually
  CHANGES. Spamming RPC at 1Hz for a value that rarely moves is wasteful and
  adds failure surface.
- FAIL SOFT. If the Bridge isn't available (sketch missing, compile failed,
  MCU not responding), everything here degrades to no-ops and logs once. The
  vision pipeline and dashboard keep running regardless.
"""
import time
import logging

logger = logging.getLogger("workspace_ai.actuators")

CH_LAMP = 0
CH_FAN = 1
CH_ALERT = 2


def _bridge():
    """Imported lazily so this module is testable off-device and a missing
    Bridge degrades gracefully rather than breaking startup."""
    from arduino.app_utils import Bridge
    return Bridge


class PhysicalLayer:
    def __init__(self, config):
        self.config = config
        self.enabled = config.PHYSICAL_ENABLED
        self._bridge_ok = True

        # Relay states, indexed by channel
        self._relay_state = {CH_LAMP: False, CH_FAN: False, CH_ALERT: False}
        self._relay_sent = {}

        # Presence hysteresis
        self._last_present_at = None

        # Posture alert tracking - only fires after SUSTAINED poor posture,
        # not a single bad frame
        self._poor_posture_since = None

        # LCD - track what's displayed so we only push on change
        self._lcd_last = None
        self._lcd_last_update = 0.0

        # Posture/engagement LEDs - track the last (r, g, b) tuple sent so
        # set_leds() is only called on change. None (not a tuple) means
        # "never sent yet", so the very first call always goes out.
        self._leds_sent = None

        # Posture beep: one-shot on crossing below POSTURE_ALERT_THRESHOLD,
        # then every POSTURE_BEEP_REPEAT_SECONDS while it stays low. Reset
        # the moment posture recovers or the person leaves, so the next bad
        # spell beeps immediately rather than waiting out a stale timer.
        self._posture_bad = False
        self._next_beep_at = None

    # ---------- Relays ----------

    def _set_relay(self, channel: int, state: bool):
        if not self.enabled or not self._bridge_ok:
            return
        if self._relay_sent.get(channel) == state:
            return
        try:
            _bridge().call("set_relay", channel, state)
            self._relay_sent[channel] = state
            logger.info("Relay ch%d -> %s", channel, "ON" if state else "OFF")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Relay Bridge call failed (%s) - physical control disabled for this session. "
                "Check sketch/sketch.ino compiled and the required sketch libraries are added.", exc,
            )
            self._bridge_ok = False

    def update_lamp(self, person_present: bool):
        """Desk lamp follows presence, with a slow release so it doesn't
        flicker when you lean out of frame."""
        now = time.monotonic()
        if person_present:
            self._last_present_at = now
            self._relay_state[CH_LAMP] = True
        elif self._relay_state[CH_LAMP] and self._last_present_at is not None:
            if (now - self._last_present_at) >= self.config.LAMP_OFF_DELAY_SECONDS:
                self._relay_state[CH_LAMP] = False
        self._set_relay(CH_LAMP, self._relay_state[CH_LAMP])

    def update_fan(self, person_present: bool):
        """Fan follows presence - same signal and release delay as the desk
        lamp. Environmental sensing was dropped, so a temperature trigger
        would never fire in practice."""
        now = time.monotonic()
        if person_present:
            self._last_present_at = now
            self._relay_state[CH_FAN] = True
        elif self._relay_state[CH_FAN] and self._last_present_at is not None:
            if (now - self._last_present_at) >= self.config.LAMP_OFF_DELAY_SECONDS:
                self._relay_state[CH_FAN] = False

        self._set_relay(CH_FAN, self._relay_state[CH_FAN])

    def update_posture_alert(self, posture_score: float, posture_label: str, person_present: bool):
        """Alert light after posture score stays SUSTAINED below threshold -
        a momentary slouch while reaching for something shouldn't set off a
        light, and calibration samples (no baseline yet) never count."""
        now = time.monotonic()

        is_low = (
            person_present
            and posture_label != "calibrating"
            and posture_score < self.config.POSTURE_ALERT_THRESHOLD
        )

        if not is_low:
            self._poor_posture_since = None
            self._relay_state[CH_ALERT] = False
        else:
            if self._poor_posture_since is None:
                self._poor_posture_since = now
            elif (now - self._poor_posture_since) >= self.config.POSTURE_ALERT_SUSTAIN_SECONDS:
                self._relay_state[CH_ALERT] = True

        self._set_relay(CH_ALERT, self._relay_state[CH_ALERT])

    # ---------- LCD ----------

    @staticmethod
    def _lcd_line(prefix: str, value: str) -> str:
        """`prefix` left-aligned, `value` right-aligned so the line lands
        exactly on column 16 - keeps the display from overflowing."""
        return f"{prefix}{value:>{16 - len(prefix)}}"

    def update_lcd(self, sample):
        """Shows live status on the device itself - so the assistant is
        readable at a glance without opening a browser."""
        if not self.enabled or not self._bridge_ok:
            return

        now = time.monotonic()
        if (now - self._lcd_last_update) < self.config.LCD_UPDATE_INTERVAL_SECONDS:
            return

        if sample is None:
            line1, line2 = "DeskSense", "starting up"
        else:
            posture_unavailable = sample.posture_label in ("no_person", "calibrating")
            posture_value = "--" if posture_unavailable else str(int(round(sample.posture_score)))
            line1 = self._lcd_line("Posture:", posture_value)

            engagement_unavailable = sample.state == "away"
            engagement_value = "--" if engagement_unavailable else str(int(round(sample.engagement_score)))
            line2 = self._lcd_line("Focus:", engagement_value)

        payload = (line1[:16], line2[:16])
        if payload == self._lcd_last:
            return

        try:
            _bridge().call("lcd_show", payload[0], payload[1])
            self._lcd_last = payload
            self._lcd_last_update = now
        except Exception as exc:  # noqa: BLE001
            logger.warning("LCD Bridge call failed (%s) - LCD updates disabled.", exc)
            self._bridge_ok = False

    # ---------- TFT ----------

    def draw_tab(self, tab_index: int, title: str, line1: str, line2: str, line3: str, line4: str, accent_index: int):
        """Paints one of the five tab screens. Python decides all content -
        pre-formatted title/line strings and an accent index - the MCU only
        lays it out and colours it. Fails soft like every other Bridge call
        here."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("draw_tab", tab_index, title, line1, line2, line3, line4, accent_index)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT draw_tab Bridge call failed (%s) - display updates disabled.", exc)
            self._bridge_ok = False

    def tft_stats(self, values: list):
        """Pushes the Today view's pre-computed integers to the MCU, which
        stores them and repaints only the value boxes that changed. Order is
        fixed and must match tft_stats() in sketch.ino."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("tft_stats", values)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT tft_stats Bridge call failed (%s) - display updates disabled.", exc)
            self._bridge_ok = False

    def draw_text_block(self, title: str, text: str, accent_index: int):
        """Paints the Summary tab: free text the MCU word-wraps across up to
        six lines, instead of the fixed four-row layout the other tabs use."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("draw_text_block", title, text, accent_index)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT draw_text_block Bridge call failed (%s) - display updates disabled.", exc)
            self._bridge_ok = False

    def draw_face(self, expression: int):
        """Paints the Buddy tab's face (0 happy, 1 concerned, 2 sleepy,
        3 away). The MCU only repaints when the expression actually changes
        and owns its own blink timer - this is a cheap no-op call otherwise."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("draw_face", expression)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT draw_face Bridge call failed (%s) - display updates disabled.", exc)
            self._bridge_ok = False

    def look_at(self, x: int, y: int):
        """Tells the Buddy tab's eye engine where to look, in the same
        -100..100 range on both axes. Store-only on the MCU side (see
        look_at() in sketch.ino) - this is a cheap, frequent call, so it
        fails soft exactly like every other Bridge call here rather than
        logging on every miss."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("look_at", x, y)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT look_at Bridge call failed (%s) - gaze tracking disabled.", exc)
            self._bridge_ok = False

    def set_leds(self, red: bool, green: bool, blue: bool):
        """Red/green/blue status LEDs - one combined Bridge call, only sent
        when the (r, g, b) tuple actually changes. Store-only on the MCU
        side (see set_leds() in sketch.ino), which drives the pins."""
        state = (bool(red), bool(green), bool(blue))
        if not self.enabled or not self._bridge_ok:
            return
        if self._leds_sent == state:
            return
        try:
            _bridge().call("set_leds", state[0], state[1], state[2])
            self._leds_sent = state
            logger.info("LEDs -> R=%s G=%s B=%s", state[0], state[1], state[2])
        except Exception as exc:  # noqa: BLE001
            logger.warning("LED set_leds Bridge call failed (%s) - LEDs disabled.", exc)
            self._bridge_ok = False

    def beep(self, ms: int):
        """One-shot buzzer beep for `ms` milliseconds. Store-only on the MCU
        side (see beep() in sketch.ino) - the timing itself is handled
        entirely there, non-blockingly."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("beep", ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Buzzer beep Bridge call failed (%s) - buzzer disabled.", exc)
            self._bridge_ok = False

    def update_feedback(self, sample):
        """Posture (red/green) + engagement (blue) LEDs, plus the posture
        beep - all in one place because set_leds() is a single combined
        call, so red/green/blue have to be decided together before it goes
        out. Separate from (and immediate, unlike) update_posture_alert()'s
        10s-sustained alert relay above; both read the same
        POSTURE_ALERT_THRESHOLD, just on different triggers.

        Nobody at the desk, or posture not yet known (calibrating): both
        posture LEDs off and the beep timer resets, so the next bad spell
        beeps right away instead of waiting out a stale 15s window."""
        now = time.monotonic()
        person_present = sample is not None and sample.person_present

        if not person_present:
            self._posture_bad = False
            self._next_beep_at = None
            self.set_leds(False, False, False)
            return

        posture_unavailable = sample.posture_label in ("no_person", "calibrating")
        blue = sample.state == "engaged"

        if posture_unavailable:
            self._posture_bad = False
            self._next_beep_at = None
            self.set_leds(False, False, blue)
            return

        is_low = sample.posture_score < self.config.POSTURE_ALERT_THRESHOLD
        if is_low:
            if not self._posture_bad:
                self._posture_bad = True
                self.beep(self.config.POSTURE_BEEP_MS)
                self._next_beep_at = now + self.config.POSTURE_BEEP_REPEAT_SECONDS
            elif now >= self._next_beep_at:
                self.beep(self.config.POSTURE_BEEP_MS)
                self._next_beep_at = now + self.config.POSTURE_BEEP_REPEAT_SECONDS
            self.set_leds(True, False, blue)
        else:
            self._posture_bad = False
            self._next_beep_at = None
            self.set_leds(False, True, blue)

    def pomo_state(self, phase: int, remaining_s: int, total_s: int, running: int):
        """Pushes the pomodoro state to the MCU's Focus tab. The sketch stores
        it and repaints only the digits and the progress bar when they change.
        phase: 0 idle, 1 focus, 2 short_break, 3 long_break - must match
        Pomodoro.tft_payload() and pomo_state() in sketch.ino."""
        if not self.enabled or not self._bridge_ok:
            return
        try:
            _bridge().call("pomo_state", phase, remaining_s, total_s, running)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TFT pomo_state Bridge call failed (%s) - display updates disabled.", exc)
            self._bridge_ok = False

    # ---------- Tab navigation ----------

    def poll_tab(self):
        """Reads the current tab index from the touch-pad handler on the
        MCU. Returns None if the Bridge is unavailable."""
        if not self.enabled or not self._bridge_ok:
            return None
        try:
            return _bridge().call("get_tab")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tab Bridge call failed (%s) - tab polling disabled.", exc)
            self._bridge_ok = False
            return None

    def poll_action(self):
        """Reads and clears the one-shot action-pad flag from the MCU.
        Returns None if the Bridge is unavailable."""
        if not self.enabled or not self._bridge_ok:
            return None
        try:
            return _bridge().call("get_action")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Action Bridge call failed (%s) - action polling disabled.", exc)
            self._bridge_ok = False
            return None

    # ---------- Reporting ----------

    @property
    def available(self) -> bool:
        return self.enabled and self._bridge_ok

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "relays": {
                "lamp": self._relay_state[CH_LAMP],
                "fan": self._relay_state[CH_FAN],
                "alert": self._relay_state[CH_ALERT],
            },
        }
