#include "Arduino_RouterBridge.h"
#include <zephyr/kernel.h>  // k_mutex: guards the Bridge -> loop() handoff below
#include <vector>
#include <math.h>    // sinf/cosf/lroundf - the eye engine's spring/breathing/saccade math
#include <string.h>  // memset/memcmp - clearing the eye RAM buffers each frame
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <Arduino_GFX_Library.h>

// ---------------------------------------------------------------------
// Buddy face (tab 0): two glowing rounded-square eyes on pure black - no
// mouth, no bitmaps. Every expression is purely shape/position/lid-cut/
// tilt/colour (EyeParams below), eased with a spring (slight overshoot,
// ~250ms settle) instead of a linear tween. Owns FACE_TOP..FACE_BOTTOM; the
// reserved FACE_COMMENT_H strip below that is cleared once per tab entry by
// renderTab()'s tab-0 branch and otherwise left blank.
//
// Mood IDs are the SAME accent/expression integer Python has always sent via
// draw_face() - this rewrite doesn't add a new Bridge signal, so the mapping
// is unchanged: 0 engaged, 1 distracted, 2 bad posture, 3 absent. MOOD_FOCUSED
// (narrowed/steadier, called out as a distinct look in the brief) is fully
// implemented below but Python has no 5th signal to request it with, so it
// is currently unreachable - see the note at MOOD_FOCUSED.
//
// "Happy" is not a 6th incoming mood number either (draw_face() only ever
// carries 0-3): the existing pomodoro hook in main.py
// (_pomodoro_eye_mood -> relay.draw_face(0)) is indistinguishable on the wire
// from the ordinary periodic refresh that also sends 0 while the user is
// simply engaged. The only information this side actually has is "did the
// mood just TRANSITION into engaged" (drawFaceNow's unchanged-expression
// early-out already tracks exactly that) - so entering MOOD_ENGAGED from a
// different mood plays the happy flourish (upward-arc squint + bob, held
// ~3s) before settling into the normal engaged look. Known gap: if the user
// is already engaged the instant a focus phase completes, there is no
// transition to hook the flourish to, so it won't fire that one time.
//
// Types/#defines consolidated here (rather than down with the rest of the
// eye-engine code) because Arduino's build step auto-generates function
// prototypes and inserts them above the first function definition in the
// file - any function below that takes/returns EyeParams or Spring1, or any
// code that uses one of these #defines, needs the type/macro already
// visible by then.
#define MOOD_ENGAGED     0  // == old FACE_FOCUSED - full shape, idle motion
#define MOOD_DISTRACTED  1  // shift+shrink ~10%, restless, tint #F5E663
#define MOOD_BAD_POSTURE 2  // worried top-lid cut, occasional slow look-down, tint #FF8A65
#define MOOD_ABSENT      3  // lids droop to a thin line, slow sway, drifting "z"
#define MOOD_FOCUSED     4  // narrowed ~75% height, steadier - see note above
#define MOOD_COUNT       5
#define FACE_FOCUSED     MOOD_ENGAGED  // alias: serviceTftRecovery() (unchanged) still names this

// FACE_TOP/FACE_AREA_H/FACE_BOTTOM are declared down with the rest of the
// chrome layout, since draw_tab() needs them too.

// ---- Eye geometry ----
// Nominal 110x110, radius 28, 70px gap, centred slightly above the face
// band's own middle (EYE_CY uses FACE_AREA_H's upper-biased midpoint the
// same way the old code did).
#define EYE_NOM_W   110
#define EYE_NOM_H   110
#define EYE_NOM_R    28
#define EYE_GAP      70
#define EYE_CY      (FACE_TOP + FACE_AREA_H * 4 / 10)
#define EYE_DX      ((EYE_GAP + EYE_NOM_W) / 2)
#define EYE_L_CX    (SCREEN_W / 2 - EYE_DX)
#define EYE_R_CX    (SCREEN_W / 2 + EYE_DX)

// ---- Per-eye RAM buffer ----
// Gaze is realised by moving the eye shape WITHIN its own buffer (not by
// moving the buffer's screen position), so the buffer must be big enough for
// shape-at-max-stretch + the glow rings + the full gaze travel range, per
// the spec. Buffer is blitted to a FIXED screen rect every frame - no
// screen-space erase bookkeeping needed, the buffer's own black clear each
// frame *is* the erase.
//   shape max:   128 x 142  (nominal + ~16%/~29% for blink-squash / mood-react overshoot)
//   glow pad:      8 px/side (2 rings x 3px + 2px AA fudge)
//   gaze travel:  22 x / 10 y px/side (leaves safety margin before the eyes could touch)
//   => 188 x 178 px x 2 bytes (RGB565) = 66,928 B/eye, 133,856 B (~130.7 KiB) for both.
#define EYE_SHAPE_MAX_W 128
#define EYE_SHAPE_MAX_H 142
#define GLOW_RING_GAP     3   // px between the eye edge and each ring, and ring-to-ring
#define GLOW_PAD        (2 * GLOW_RING_GAP + 2)
#define GAZE_RANGE_X     22
#define GAZE_RANGE_Y     10
#define EYE_BUF_W (EYE_SHAPE_MAX_W + 2 * GLOW_PAD + 2 * GAZE_RANGE_X)  // 188
#define EYE_BUF_H (EYE_SHAPE_MAX_H + 2 * GLOW_PAD + 2 * GAZE_RANGE_Y)  // 178

#define EYE_BLIT_L_X (EYE_L_CX - EYE_BUF_W / 2)
#define EYE_BLIT_R_X (EYE_R_CX - EYE_BUF_W / 2)
#define EYE_BLIT_Y   (EYE_CY - EYE_BUF_H / 2)

#define COLOR_EYE_CORE  RGB565(63, 224, 232)  // #3FE0E8
#define COLOR_EYE_RING1 RGB565(26, 127, 134)  // #1A7F86
#define COLOR_EYE_RING2 RGB565(13, 63, 67)    // #0D3F43
#define COLOR_FACE_BG   RGB565(0, 0, 0)       // pure black - this tab is never card-styled

#define EYE_L 0
#define EYE_R 1

// struct EyeParams (the 8 parameters every expression/motion acts through)
// and struct Spring1 (the spring every one of those parameters rides) are
// used in function signatures further down (eyeParamsEqual,
// renderEyeToBuffer, springStep), hence being up here too.
struct EyeParams {
  float offsetX, offsetY;
  float width, height;
  float radius;
  float topCut, bottomCut;
  float tilt;
  uint16_t color;
};

struct Spring1 { float value, vel, target; };
#define SPRING_K        170.0f
#define SPRING_DAMPING   16.0f

#define EYE_FRAME_INTERVAL_MS 40  // ~25fps target
#define BLINK_FRAMES   6
#define BLINK_FRAME_MS 28
#define REACT_MS 120
#define HAPPY_HOLD_MS 3000
#define LOOK_AT_FRESH_MS 2000

// Must be declared before any function definition: Arduino's build step
// auto-generates function prototypes and inserts them above the first one
// in the file, so a function below that takes/returns a type the compiler
// hasn't seen yet (because the type was declared further down, after that
// first function) fails with "does not name a type". StatsData is used by
// several tab-1 (Today view) functions, so it has to live up here rather
// than next to the rest of that tab's code further down.
// -1 in avgPosture / avgEngagement means "no scored samples", rendered as
// "--" exactly like the dashboard does.
struct StatsData {
  int focusedMin, distractionMin, posturePoorMin, awayMin;
  int engagedMin, engagedPct;
  int distractedMin, distractedPct;
  int awayBdMin, awayBdPct;
  int avgPosture, avgEngagement, sessionMin;
};

// Pomodoro state pushed by Python over "pomo_state". Same reason as
// StatsData for living up here: tab-5 functions below take it by reference.
// phase: 0 idle, 1 focus, 2 short_break, 3 long_break (see
// Pomodoro.tft_payload() in python/core/pomodoro.py - keep in sync).
struct PomoData {
  int phase;
  int remainingS;
  int totalS;
  int running;
};

// Shared between the TFT tab chrome and the touch-pad handlers below -
// declared once up top so both sections can see it regardless of order.
//
// Tab 5 (Focus, the pomodoro view) was appended rather than inserted, so
// every existing index keeps its meaning: 0 Buddy, 1 Now, 2 Session,
// 3 Summary, 4 System, 5 Focus. renderTab()'s tab-1/tab-3 special cases and
// main.py's _push_tab_content() both key off those numbers.
#define TAB_COUNT 6
#define TAB_FOCUS 5
int currentTab = 0;  // touch-driven; also read by updateEyes() to gate the eye animation to tab 0

// -1 = no Buddy face drawn yet / invalidated by another tab - read by
// draw_tab() (to invalidate it) and by the face-drawing block further down
// (to decide whether draw_face() needs to do a full repaint).
int currentExpression = -1;

// Moved off D4/D5/D6: those are now the buzzer and red/green status LEDs
// Moved off D4/D5/D6: those are now the buzzer and red/green status LEDs
// (newly wired hardware, active-HIGH) - the old relay init here was driving
// them HIGH at boot (meant as "relay off" under RELAY_ACTIVE_LOW), which is
// exactly what was turning the buzzer/LEDs on at boot. A1/A2/A3 were free -
// D12 was briefly tried instead of A3 but it's the TFT SPI bus's MISO line.
//
// Plain numbers, not the A0/A1/A2/A3 macros: this board's core
// (arduino:zephyr:unoq) doesn't define them - confirmed via
// arduino_uno_q_stm32u585xx.overlay's digital-pin-gpios devicetree node,
// one contiguous array where D0..D13 are indices 0-13 and A0..A5
// immediately follow as indices 14-19 (A0=14, A1=15, A2=16, A3=17), the
// same positional numbering classic AVR Arduino cores use. Using the bare
// `A1`/`A2`/`A3` identifiers was a hard compile error on this board - no
// fresh binary was ever produced, which is why the previous restart stayed
// on the old sketch.
const int RELAY_PINS[3] = {15, 16, 17};  // A1, A2, A3
const bool RELAY_ACTIVE_LOW = true;

hd44780_I2Cexp lcd;
bool lcdPresent = false;

// ---------------------------------------------------------------------
// 3.5" TFT status screen: ILI9488 480x320 over hardware SPI, rotated to
// 480x320 landscape. CS=D10, DC=D8, RESET=D9, MOSI=D11, SCK=D13 (hardware
// SPI pins, taken implicitly by Arduino_HWSPI, unchanged from the ILI9341
// this replaces); MISO unused, no backlight pin (LED wired to 3V3).
//
// Layout: a coloured header bar (title + current-state accent) on top, a
// content area Python fills per-tab, and a row of tab dots on the bottom.
// Python owns all content - every tab's text arrives pre-formatted over the
// Bridge; this side only lays it out, colours it, and never wraps text
// (wrapping was leaving residue from the previous row - truncating instead
// keeps every repaint fully self-contained within its own rectangle).
#define TFT_CS   10
#define TFT_RST   9
#define TFT_DC    8
// 18-bit colour means 3 bytes/pixel, so a 480x320 full-screen fill pushes
// ~461 KB over the bus - at 8 MHz that alone is ~460 ms, which is what made
// a redraw after a touch feel slow. 20 MHz quarters it. Bump to 40000000L
// next if the panel stays clean at 20 (watch for speckles/tearing on the
// colour fills at boot); drop back a step if it doesn't.
#define TFT_SPI_SPEED 20000000L  // 20 MHz (was 8 MHz)

Arduino_DataBus *tftBus = new Arduino_HWSPI(TFT_DC, TFT_CS);
Arduino_GFX *gfx = new Arduino_ILI9488_18bit(tftBus, TFT_RST, 1 /* rotation */, false /* IPS */);

#define SCREEN_W 480
#define SCREEN_H 320
#define MARGIN   10

// Manual RGB565 pack so custom colours don't depend on library-provided
// macros (tftcheck confirmed this library version has no built-in grey).
#define RGB565(r, g, b) ((uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)))

// ---- Colour theme ----
// Matches the web dashboard's dark "bento card" palette exactly (same hex
// values as assets/style.css's --bg/--card/--text/--text-muted and the five
// state colours) rather than an independent TFT theme. accentColor()'s
// mapping (good=FOCUSED, warning=DISTRACTED, bad=DROWSY/posture, AWAY) still
// drives the status stripe and drawRow's score-threshold colours exactly as
// before - only the values moved.
#define COLOR_BG              RGB565(11, 11, 18)    // #0B0B12 - page background
#define COLOR_SURFACE         RGB565(21, 21, 31)    // #15151F - card fill
#define COLOR_BORDER          RGB565(53, 53, 74)    // ~ card border, one step lighter than the fill
#define COLOR_LABEL           RGB565(154, 154, 168) // #9A9AA8 - secondary/muted text
#define COLOR_VALUE           RGB565(245, 245, 247) // #F5F5F7 - primary text
#define COLOR_HIGHLIGHT       RGB565(183, 156, 255) // #B79CFF - violet (Absent), general highlight
#define COLOR_ACCENT_GREEN    RGB565(124, 242, 154) // #7CF29A - Focused: good / FOCUSED
#define COLOR_ACCENT_AMBER    RGB565(245, 230, 99)  // #F5E663 - Distracted: warning / DISTRACTED
#define COLOR_ACCENT_RED      RGB565(255, 138, 101) // #FF8A65 - Bad posture: bad / DROWSY
#define COLOR_ACCENT_BLUEGREY RGB565(183, 156, 255) // #B79CFF - Absent: AWAY
#define COLOR_ACCENT_PHONE    RGB565(96, 165, 250)  // #60A5FA - Phone, for future pills/badges
// Card geometry shared by every "stat block" on the TFT (drawRow, the
// Insights/stats panel, the Pomodoro card) - one radius/border so they all
// read as the same bento card the web dashboard uses.
#define CARD_BORDER_W 1

// accentIndex convention shared with the Python side: 0=green, 1=amber,
// 2=red, 3=blue-grey. Keep in sync with main.py's _compute_accent(). This
// still exclusively drives the status stripe, the Buddy face, and drawRow's
// score-threshold colours.
uint16_t accentColor(int accentIndex) {
  switch (accentIndex) {
    case 0: return COLOR_ACCENT_GREEN;
    case 1: return COLOR_ACCENT_AMBER;
    case 2: return COLOR_ACCENT_RED;
    default: return COLOR_ACCENT_BLUEGREY;
  }
}

// ---- Tab identity colours ----
// Static per tab, independent of the live state accent above: used only for
// the tab name text in the header and the active dot in the tab bar. Never
// touches the status stripe, the face, or score colours - those stay on
// accentColor().
#define COLOR_TAB_BUDDY   RGB565(96, 209, 224)  // cyan
#define COLOR_TAB_NOW     RGB565(46, 204, 113)  // green
#define COLOR_TAB_SESSION RGB565(235, 165, 30)  // amber
#define COLOR_TAB_SUMMARY RGB565(170, 110, 230) // purple
#define COLOR_TAB_SYSTEM  RGB565(70, 140, 235)  // blue
#define COLOR_TAB_FOCUS   RGB565(124, 242, 154) // green - same as the web Focus pill

uint16_t tabIdentityColor(int tabIndex) {
  switch (tabIndex) {
    case 0: return COLOR_TAB_BUDDY;
    case 1: return COLOR_TAB_NOW;
    case 2: return COLOR_TAB_SESSION;
    case 3: return COLOR_TAB_SUMMARY;
    case TAB_FOCUS: return COLOR_TAB_FOCUS;
    default: return COLOR_TAB_SYSTEM;
  }
}

// ---- Chrome layout: header bar, content area, tab-dot bar ----
#define HEADER_H       28
#define FOOTER_H       20
#define CONTENT_TOP    (HEADER_H + 8)
#define CONTENT_BOTTOM (SCREEN_H - FOOTER_H - 4)

#define TITLE_TEXT_SIZE    2
#define HEADER_STRIPE_W    4  // status stripe at the far-left edge of the header

#define TAB_DOT_SIZE      8
#define TAB_DOT_GAP       14
// Active indicator: a small white capsule/pill, matching the web nav's
// active-tab pill, rather than a plain enlarged dot.
#define TAB_PILL_W        18
#define TAB_PILL_H         12

// ---- Row cards (tabs 1/2/4): each row is a rounded card spanning the full
// content width, label and value sharing one line (label left at size 2,
// value right-aligned at size 3). A purely-numeric value additionally gets
// a thin proportional score bar under the line (drives tab 1's
// POSTURE/ENGAGE bars without any tab-specific logic - only tab 1 ever
// sends a bare-integer value here). CARD_GAP/CARD_H are chosen so
// 4*CARD_H + 3*CARD_GAP divides the content band exactly, with no leftover
// band at the bottom. Sized to fit the existing content band, so this
// doesn't touch CONTENT_TOP/CONTENT_BOTTOM (and therefore never shifts the
// Buddy face, which is measured from those same constants).
#define TAB_ROW_COUNT     4
#define CARD_GAP          4
#define CARD_RADIUS       12
#define CARD_PADDING      4
#define CARD_LABEL_SIZE   2
#define CARD_VALUE_SIZE   3
#define CARD_BAR_H        3
#define LABEL_VALUE_GAP   6  // minimum gap between the label and the right-aligned value
#define CARD_H            ((CONTENT_BOTTOM - CONTENT_TOP - (TAB_ROW_COUNT - 1) * CARD_GAP) / TAB_ROW_COUNT)

// Buddy tab (0): the face owns the upper part of the content area,
// FACE_TOP..FACE_BOTTOM. The bottom FACE_COMMENT_H px is reserved for a
// future one-line comment and is kept blank for now - draw_tab()'s tab-0
// branch just clears it on tab entry (see the eye engine further down,
// which owns everything above that strip). Defined here (rather than down
// with the rest of the face-drawing code) since draw_tab() needs them and
// macros must be defined before they're used.
#define FACE_COMMENT_H 36
#define FACE_TOP       CONTENT_TOP
#define FACE_AREA_H    (CONTENT_BOTTOM - FACE_TOP - FACE_COMMENT_H)
#define FACE_BOTTOM    (FACE_TOP + FACE_AREA_H)

// Adafruit-GFX-compatible default font: 5px glyph + 1px gap, scaled by size.
#define CHAR_W(size) (6 * (size))

// ---------------------------------------------------------------------
// Redraw timing instrumentation + partial-redraw caches.
//
// touchEdgeAt is stamped the instant a debounced tab-change edge is
// accepted and cleared by the next draw_tab()/draw_text_block(), so the
// latency it reports spans everything in between: Python's poll of
// get_tab() plus the Bridge round-trip, not just the drawing.
unsigned long touchEdgeAt = 0;

// Every cache below mirrors what is currently on the glass so an identical
// repaint can be skipped. They change only *whether* the same pixels get
// rewritten - never what is drawn. "\x01" is a sentinel no real string can
// equal, so the first paint after boot or a tab switch always happens.
#define CACHE_INVALID "\x01"

int    lastHeaderTab    = -1;
String lastHeaderTitle  = CACHE_INVALID;
int    lastHeaderAccent = -1;
int    lastTabBarTab    = -1;
int    lastContentTab   = -1;  // which tab the content area currently belongs to
String lastRowText[TAB_ROW_COUNT] = {CACHE_INVALID, CACHE_INVALID, CACHE_INVALID, CACHE_INVALID};
String lastWord         = CACHE_INVALID;
int    lastWordAccent   = -1;
String lastTextBlock    = CACHE_INVALID;

// Called on a tab switch: forces the next paint of every content element.
void invalidateContentCaches() {
  for (int i = 0; i < TAB_ROW_COUNT; i++) lastRowText[i] = CACHE_INVALID;
  lastWord = CACHE_INVALID;
  lastWordAccent = -1;
  lastTextBlock = CACHE_INVALID;
}

// ---- Per-tab content cache ----
// The last content Python pushed for each tab. A touch-driven switch
// repaints from here on the spot instead of waiting for Python's next
// poll+push; Python's own update then lands on top, and is a cheap no-op
// when it matches what we already drew.
struct TabContent {
  bool   valid;
  bool   isTextBlock;
  String title;
  String line1, line2, line3, line4;
  String text;
  int    accentIndex;
};
TabContent tabCache[TAB_COUNT];

// Set by a touch edge, consumed by loop(). The repaint deliberately happens
// in loop() rather than inside the touch handler so all loop-side drawing
// stays in one place.
bool pendingTabRedraw = false;

// ---------------------------------------------------------------------
// Bridge -> loop() handoff.
//
// loop() is the only context that ever touches the panel. The Bridge
// handlers run on their own thread, so none of them draw: each copies its
// payload into the pending state below, sets a flag, and returns. loop()
// picks the work up on its next pass, so exactly one thread drives the SPI
// bus.
//
// This replaces the old tftBusy flag, which was the freeze: its 25 ms spin
// gave up and took the "lock" anyway, so a long paint in loop() (a tab
// switch wipes the whole content area) let the Bridge thread interleave its
// own commands onto the same bus. That desyncs the ILI9488's command/
// parameter state machine, and nothing in software ever resynced it - hence
// a black panel until a power cycle.
//
// pendingLock guards only the shared payloads: tabCache, pendingStats,
// pendingFace and their flags. It is held across the copy in and the copy
// out and never across a draw, so a Python push never waits on SPI.
K_MUTEX_DEFINE(pendingLock);

// Which tab Python most recently pushed content for, or -1 when loop() has
// drawn everything. The push itself always lands in tabCache[] under the
// lock, so no tab loses its cached content even if two pushes arrive between
// two loop() passes - this only records which one still needs painting.
int pendingDrawTab = -1;

// Newest expression from draw_face(). The "expression unchanged" early-out
// moved to the loop() side, next to currentExpression, so a repeat push is
// still the cheap no-op it always was.
int  pendingFace = 0;
bool pendingFaceDirty = false;

// Newest pomodoro state from pomo_state(), guarded by pendingLock. loop()
// copies it into pomo (below) and draws from there.
PomoData pendingPomo = {0, 0, 0, 0};
bool     pendingPomoDirty = false;

bool isAsciiDigit(char c) {
  return c >= '0' && c <= '9';
}

// Cuts a string down to maxChars rather than letting it wrap or overflow
// its rectangle - every row now spans the full content width, so a
// 10-character string like "DISTRACTED" always fits; this only bites for
// something longer than that.
String truncateToWidth(const String &s, int maxChars) {
  if (maxChars <= 0) return "";
  if ((int)s.length() <= maxChars) return s;
  return s.substring(0, maxChars);
}

// Dark surface bar (not a solid accent fill): a 4px status stripe in the
// live state accent at the far-left edge, "DeskSense" in white just after
// it, and the current tab's name right-aligned in that tab's identity
// colour (not the state accent - see tabIdentityColor()).
void drawHeader(int tabIndex, const String &title, int accentIndex) {
  // Python re-sends the same header on every refresh; repainting it costs a
  // 480x28 surface fill plus two strings for no visible change, so bail out
  // when all three inputs match what is already up there.
  if (tabIndex == lastHeaderTab && accentIndex == lastHeaderAccent && title == lastHeaderTitle) return;
  lastHeaderTab = tabIndex;
  lastHeaderAccent = accentIndex;
  lastHeaderTitle = title;

  gfx->fillRect(0, 0, SCREEN_W, HEADER_H, COLOR_SURFACE);
  gfx->fillRect(0, 0, HEADER_STRIPE_W, HEADER_H, accentColor(accentIndex));

  int textY = (HEADER_H - 8 * TITLE_TEXT_SIZE) / 2;
  gfx->setTextSize(TITLE_TEXT_SIZE);

  gfx->setTextColor(COLOR_VALUE);
  gfx->setCursor(HEADER_STRIPE_W + MARGIN, textY);
  gfx->print("DeskSense");

  int maxChars = (SCREEN_W - 2 * MARGIN) / CHAR_W(TITLE_TEXT_SIZE);
  String tabName = truncateToWidth(title, maxChars);
  int textW = (int)(tabName.length() * CHAR_W(TITLE_TEXT_SIZE));
  gfx->setTextColor(tabIdentityColor(tabIndex));
  gfx->setCursor(SCREEN_W - MARGIN - textW, textY);
  gfx->print(tabName);
}

// Surface-coloured strip along the bottom holding the tab indicators: the
// current tab shows as a small white capsule/pill (matching the web nav's
// active-tab pill), the rest as plain dots in the border tone.
//
// Only the strip's first paint refills the whole band. After that a tab
// change touches just the two slots whose state actually changed - the one
// losing the pill and the one gaining it. Each is erased with the pill's
// full bounding box first (the largest shape any slot can ever hold), so
// neither a pill->dot nor a dot->pill transition leaves residue.
void drawTabBar(int tabIndex, int accentIndex) {
  if (tabIndex == lastTabBarTab) return;

  int rSmall = TAB_DOT_SIZE / 2;
  int totalW = TAB_COUNT * TAB_DOT_SIZE + (TAB_COUNT - 1) * TAB_DOT_GAP;
  int startX = (SCREEN_W - totalW) / 2 + rSmall;
  int cy = SCREEN_H - FOOTER_H / 2;
  bool firstPaint = (lastTabBarTab < 0);

  if (firstPaint) {
    gfx->fillRect(0, SCREEN_H - FOOTER_H, SCREEN_W, FOOTER_H, COLOR_SURFACE);
  }
  for (int i = 0; i < TAB_COUNT; i++) {
    if (!firstPaint && i != lastTabBarTab && i != tabIndex) continue;
    int cx = startX + i * (TAB_DOT_SIZE + TAB_DOT_GAP);
    bool active = (i == tabIndex);
    if (!firstPaint) {
      gfx->fillRoundRect(cx - TAB_PILL_W / 2, cy - TAB_PILL_H / 2, TAB_PILL_W, TAB_PILL_H, TAB_PILL_H / 2, COLOR_SURFACE);
    }
    if (active) {
      gfx->fillRoundRect(cx - TAB_PILL_W / 2, cy - TAB_PILL_H / 2, TAB_PILL_W, TAB_PILL_H, TAB_PILL_H / 2, COLOR_VALUE);
    } else {
      gfx->fillCircle(cx, cy, rSmall, COLOR_BORDER);
    }
  }

  lastTabBarTab = tabIndex;
}

// One row as a card, spanning the full content width (MARGIN to
// SCREEN_W - MARGIN): label in grey at size 2, left-aligned, and value in
// white (or score-threshold colour, if purely numeric) at size 3,
// right-aligned to the card's inner edge - both on the same line. The
// label is capped to ~45% of the inner width so a short label (the common
// case) leaves the value most of the row; a long value (an IP address on
// the System tab) only truncates as a last resort. A numeric value also
// gets a thin horizontal bar under the line, filled proportionally in the
// threshold colour over an unfilled border-tone track - this is what puts
// bars under tab 1's POSTURE/ENGAGE cards without the card renderer
// knowing which tab it's on.
void drawRow(int rowIndex, const String &line) {
  int cardX = MARGIN;
  int cardW = SCREEN_W - 2 * MARGIN;
  int cardY = CONTENT_TOP + rowIndex * (CARD_H + CARD_GAP);
  gfx->fillRoundRect(cardX, cardY, cardW, CARD_H, CARD_RADIUS, COLOR_SURFACE);
  gfx->drawRoundRect(cardX, cardY, cardW, CARD_H, CARD_RADIUS, COLOR_BORDER);

  int innerW = cardW - 2 * CARD_PADDING;
  int lastSpace = line.lastIndexOf(' ');
  String labelPart = lastSpace >= 0 ? line.substring(0, lastSpace) : "";
  String valuePart = lastSpace >= 0 ? line.substring(lastSpace + 1) : line;
  labelPart.trim();

  bool isNumeric = valuePart.length() > 0;
  for (unsigned int i = 0; i < valuePart.length() && isNumeric; i++) {
    if (!isAsciiDigit(valuePart[i])) isNumeric = false;
  }
  uint16_t valueColor = COLOR_VALUE;
  int score = 0;
  if (isNumeric) {
    score = valuePart.toInt();
    if (score < 0) score = 0;
    if (score > 100) score = 100;
    valueColor = (score >= 85) ? COLOR_ACCENT_GREEN : (score >= 70) ? COLOR_ACCENT_AMBER : COLOR_ACCENT_RED;
  }

  int labelCapChars = (innerW * 45 / 100) / CHAR_W(CARD_LABEL_SIZE);
  String label = truncateToWidth(labelPart, labelCapChars);
  int labelW = (int)(label.length() * CHAR_W(CARD_LABEL_SIZE));

  int valueMaxW = innerW - labelW - LABEL_VALUE_GAP;
  int valueMaxChars = valueMaxW / CHAR_W(CARD_VALUE_SIZE);
  String value = truncateToWidth(valuePart, valueMaxChars);
  int valueW = (int)(value.length() * CHAR_W(CARD_VALUE_SIZE));

  int rowH = 8 * CARD_VALUE_SIZE;  // the taller of the two text sizes sets the shared line's height
  int rowY = cardY + CARD_PADDING;
  int labelY = rowY + (rowH - 8 * CARD_LABEL_SIZE) / 2;  // vertically centred against the value's height

  gfx->setTextColor(COLOR_LABEL);
  gfx->setTextSize(CARD_LABEL_SIZE);
  gfx->setCursor(cardX + CARD_PADDING, labelY);
  gfx->print(label);

  gfx->setTextColor(valueColor);
  gfx->setTextSize(CARD_VALUE_SIZE);
  gfx->setCursor(cardX + cardW - CARD_PADDING - valueW, rowY);
  gfx->print(value);

  if (isNumeric) {
    int barX = cardX + CARD_PADDING;
    int barY = rowY + rowH + LABEL_VALUE_GAP;
    int filledW = (innerW * score) / 100;
    gfx->fillRect(barX, barY, innerW, CARD_BAR_H, COLOR_BORDER);
    if (filledW > 0) gfx->fillRect(barX, barY, filledW, CARD_BAR_H, valueColor);
  }
}

// Clears the content area only (never the header or tab bar) and paints
// either the Buddy tab's single big centred word or up to four label/value
// rows. Returns 1 so the Python side can confirm the call landed.
// ---------------------------------------------------------------------
// Tab 1: the Today view, mirroring the web dashboard's layout. Python owns
// all the arithmetic (it ports assets/app.js's renderStatCards /
// renderTodayBreakdown verbatim) and pushes the finished integers here over
// "tft_stats"; this side only lays them out.
//
// Dark bento palette, matching the rest of the TFT chrome and the web
// dashboard's own (now-dark) Insights tab - this used to be deliberately
// light, back when the web dashboard itself was light; both are dark now,
// so this panel's values just point at the same named colours as the rest
// of the file instead of keeping an independent light theme.
#define ST_BG      COLOR_BG       // #0B0B12 page background
#define ST_CARD    COLOR_SURFACE  // #15151F card / panel fill
#define ST_BORDER  COLOR_BORDER   // card border
#define ST_LABEL   COLOR_LABEL    // #9A9AA8 secondary text
#define ST_TEAL    COLOR_ACCENT_GREEN   // #7CF29A - Focused
#define ST_AMBER   COLOR_ACCENT_AMBER   // #F5E663 - Distracted
#define ST_BEIGE   COLOR_ACCENT_BLUEGREY // #B79CFF - Absent, used for the bar's 3rd/"away" segment
#define ST_TEXT    COLOR_VALUE    // #F5F5F7 primary text
#define ST_RADIUS  12

// Geometry. Everything is derived so the whole thing is bounded by
// CONTENT_TOP..CONTENT_BOTTOM with a 6 px outer margin, and the final
// element lands at y=274 against a 290 px floor.
#define ST_MARGIN     6
#define ST_X          ST_MARGIN
#define ST_W          (SCREEN_W - 2 * ST_MARGIN)
#define ST_TOP        (CONTENT_TOP + ST_MARGIN)

#define ST_CARD_H     54
#define ST_CARD_GAP   6
#define ST_CARD_W     ((ST_W - 3 * ST_CARD_GAP) / 4)
#define ST_CARD_Y     ST_TOP
#define ST_CARD_X(i)  (ST_X + (i) * (ST_CARD_W + ST_CARD_GAP))

#define ST_PANEL_Y    (ST_CARD_Y + ST_CARD_H + 6)
#define ST_PANEL_H    (CONTENT_BOTTOM - ST_MARGIN - ST_PANEL_Y)
#define ST_PANEL_PAD  8
#define ST_IN_X       (ST_X + ST_PANEL_PAD)
#define ST_IN_W       (ST_W - 2 * ST_PANEL_PAD)

#define ST_TITLE_Y    (ST_PANEL_Y + 8)
#define ST_BAR_Y      (ST_PANEL_Y + 22)
#define ST_BAR_H      12
#define ST_LEGEND_Y   (ST_PANEL_Y + 40)
#define ST_DIV1_Y     (ST_PANEL_Y + 56)
#define ST_BD_Y0      (ST_PANEL_Y + 62)
#define ST_BD_PITCH   22
#define ST_DIV2_Y     (ST_PANEL_Y + 128)
#define ST_COL_LBL_Y  (ST_PANEL_Y + 136)
#define ST_COL_VAL_Y  (ST_PANEL_Y + 148)

#define ST_LBL_SIZE   1
#define ST_CARD_VSIZE 2
#define ST_BD_VSIZE   2
#define ST_COL_VSIZE  3

#define ST_COL_W      (ST_IN_W / 3)

// StatsData itself now lives at the top of the file, right after the
// #includes - Arduino's auto-generated function prototypes land above the
// first function definition, and several functions below take `const
// StatsData &`, so the type has to be visible that early or those
// prototypes reference an undeclared type.

// stats = newest from Python, statsDrawn = what is currently on the glass.
// Diffing the two is what keeps updates to just the changed value boxes.
StatsData stats = {0,0,0,0, 0,0, 0,0, 0,0, -1,-1, 0};
StatsData statsDrawn;
bool statsDirty = false;
bool statsFrameDrawn = false;  // cleared whenever another tab takes the screen

// What tft_stats() last received, guarded by pendingLock. loop() copies it
// into `stats` above and draws from there, so every read of `stats` - and
// every draw that follows one - stays on the loop() thread.
StatsData pendingStats = {0,0,0,0, 0,0, 0,0, 0,0, -1,-1, 0};
bool pendingStatsDirty = false;

String statsMin(int v) { return String(v) + " min"; }
String statsScore(int v) { return v < 0 ? String("--") : String(v); }

// Prints text right-aligned against a given right edge.
void printRight(int rightX, int y, int textSize, uint16_t color, const String &s) {
  gfx->setTextSize(textSize);
  gfx->setTextColor(color);
  gfx->setCursor(rightX - (int)(s.length() * CHAR_W(textSize)), y);
  gfx->print(s);
}

void printCentred(int centreX, int y, int textSize, uint16_t color, const String &s) {
  gfx->setTextSize(textSize);
  gfx->setTextColor(color);
  gfx->setCursor(centreX - (int)(s.length() * CHAR_W(textSize)) / 2, y);
  gfx->print(s);
}

// ---- Row A: the four summary cards ----
const char *ST_CARD_LABELS[4] = {"FOCUSED", "DISTRACTION", "POSTURE", "AWAY"};
uint16_t statsCardColor(int i) {
  // teal, amber, amber, teal - matching the dashboard's card accents.
  return (i == 1 || i == 2) ? ST_AMBER : ST_TEAL;
}
int statsCardValue(const StatsData &s, int i) {
  switch (i) {
    case 0:  return s.focusedMin;
    case 1:  return s.distractionMin;
    case 2:  return s.posturePoorMin;
    default: return s.awayMin;
  }
}

// Each card's value sits on its own line inside the card, so a value update
// only has to repaint this rect.
void statsCardValueBox(int i, int &x, int &y, int &w, int &h) {
  x = ST_CARD_X(i) + 6;
  y = ST_CARD_Y + 26;
  w = ST_CARD_W - 12;
  h = 8 * ST_CARD_VSIZE + 2;
}

void drawStatsCardValue(int i, const StatsData &s) {
  int x, y, w, h;
  statsCardValueBox(i, x, y, w, h);
  gfx->fillRect(x, y, w, h, ST_CARD);
  gfx->setTextSize(ST_CARD_VSIZE);
  gfx->setTextColor(statsCardColor(i));
  gfx->setCursor(x, y);
  gfx->print(statsMin(statsCardValue(s, i)));
}

// ---- Panel B: stacked bar ----
// Widths are apportioned from the percentages, with the last segment taking
// the rounding remainder so the bar always spans exactly ST_IN_W.
void drawStatsBar(const StatsData &s) {
  int x = ST_IN_X;
  int wEng = (ST_IN_W * s.engagedPct) / 100;
  int wDis = (ST_IN_W * s.distractedPct) / 100;
  int wAwy = ST_IN_W - wEng - wDis;
  if (wAwy < 0) wAwy = 0;

  gfx->fillRoundRect(x, ST_BAR_Y, ST_IN_W, ST_BAR_H, ST_BAR_H / 2, ST_BEIGE);
  if (wEng > 0) gfx->fillRect(x, ST_BAR_Y, wEng, ST_BAR_H, ST_TEAL);
  if (wDis > 0) gfx->fillRect(x + wEng, ST_BAR_Y, wDis, ST_BAR_H, ST_AMBER);
}

// ---- Panel B: the three breakdown rows ----
const char *ST_BD_LABELS[3] = {"Engaged", "Distracted", "Away from desk"};
void statsBdValues(const StatsData &s, int i, int &mins, int &pct) {
  if (i == 0)      { mins = s.engagedMin;    pct = s.engagedPct; }
  else if (i == 1) { mins = s.distractedMin; pct = s.distractedPct; }
  else             { mins = s.awayBdMin;     pct = s.awayBdPct; }
}

// The value box covers the right half of the row - wide enough for both the
// bold "N min" and the small percentage that trails it.
void statsBdValueBox(int i, int &x, int &y, int &w, int &h) {
  x = ST_IN_X + ST_IN_W / 2;
  y = ST_BD_Y0 + i * ST_BD_PITCH;
  w = ST_IN_W / 2;
  h = 8 * ST_BD_VSIZE + 2;
}

void drawStatsBdValue(int i, const StatsData &s) {
  int x, y, w, h, mins, pct;
  statsBdValueBox(i, x, y, w, h);
  statsBdValues(s, i, mins, pct);
  gfx->fillRect(x, y, w, h, ST_CARD);

  int rightEdge = ST_IN_X + ST_IN_W;
  String pctStr = String(pct) + "%";
  printRight(rightEdge, y + 4, ST_LBL_SIZE, ST_LABEL, pctStr);
  int pctW = (int)(pctStr.length() * CHAR_W(ST_LBL_SIZE)) + 6;
  printRight(rightEdge - pctW, y, ST_BD_VSIZE, ST_TEXT, statsMin(mins));
}

// ---- Panel B: the three centred columns ----
const char *ST_COL_LABELS[3] = {"AVG POSTURE", "AVG ENGAGEMENT", "SESSION"};
String statsColValue(const StatsData &s, int i) {
  if (i == 0) return statsScore(s.avgPosture);
  if (i == 1) return statsScore(s.avgEngagement);
  return statsMin(s.sessionMin);
}

void statsColValueBox(int i, int &x, int &y, int &w, int &h) {
  x = ST_IN_X + i * ST_COL_W;
  y = ST_COL_VAL_Y;
  w = ST_COL_W;
  h = 8 * ST_COL_VSIZE + 2;
}

void drawStatsColValue(int i, const StatsData &s) {
  int x, y, w, h;
  statsColValueBox(i, x, y, w, h);
  gfx->fillRect(x, y, w, h, ST_CARD);
  printCentred(x + w / 2, y, ST_COL_VSIZE, ST_TEXT, statsColValue(s, i));
}

// Paints the whole tab: background, card shells, panel chrome, and every
// value. Only runs on entering the tab - after that only value boxes move.
void renderStatsFrame() {
  gfx->fillRect(0, CONTENT_TOP, SCREEN_W, CONTENT_BOTTOM - CONTENT_TOP, ST_BG);

  // Row A
  for (int i = 0; i < 4; i++) {
    int cx = ST_CARD_X(i);
    gfx->fillRoundRect(cx, ST_CARD_Y, ST_CARD_W, ST_CARD_H, ST_RADIUS, ST_CARD);
    gfx->drawRoundRect(cx, ST_CARD_Y, ST_CARD_W, ST_CARD_H, ST_RADIUS, ST_BORDER);
    gfx->setTextSize(ST_LBL_SIZE);
    gfx->setTextColor(ST_LABEL);
    gfx->setCursor(cx + 6, ST_CARD_Y + 8);
    gfx->print(ST_CARD_LABELS[i]);
    drawStatsCardValue(i, stats);
  }

  // Panel B shell
  gfx->fillRoundRect(ST_X, ST_PANEL_Y, ST_W, ST_PANEL_H, ST_RADIUS, ST_CARD);
  gfx->drawRoundRect(ST_X, ST_PANEL_Y, ST_W, ST_PANEL_H, ST_RADIUS, ST_BORDER);

  gfx->setTextSize(ST_LBL_SIZE);
  gfx->setTextColor(ST_LABEL);
  gfx->setCursor(ST_IN_X, ST_TITLE_Y);
  gfx->print("TODAY SO FAR");

  drawStatsBar(stats);

  // Legend: swatch + caption, laid out left to right.
  const char *legend[3] = {"Engaged", "Distracted", "Away"};
  uint16_t legendColors[3] = {ST_TEAL, ST_AMBER, ST_BEIGE};
  int lx = ST_IN_X;
  for (int i = 0; i < 3; i++) {
    gfx->fillRect(lx, ST_LEGEND_Y, 8, 8, legendColors[i]);
    gfx->setTextSize(ST_LBL_SIZE);
    gfx->setTextColor(ST_LABEL);
    gfx->setCursor(lx + 12, ST_LEGEND_Y);
    gfx->print(legend[i]);
    lx += 12 + (int)(String(legend[i]).length() * CHAR_W(ST_LBL_SIZE)) + 16;
  }

  gfx->drawFastHLine(ST_IN_X, ST_DIV1_Y, ST_IN_W, ST_BORDER);

  for (int i = 0; i < 3; i++) {
    int ry = ST_BD_Y0 + i * ST_BD_PITCH;
    gfx->setTextSize(ST_LBL_SIZE);
    gfx->setTextColor(ST_LABEL);
    gfx->setCursor(ST_IN_X, ry + 4);
    gfx->print(ST_BD_LABELS[i]);
    drawStatsBdValue(i, stats);
  }

  gfx->drawFastHLine(ST_IN_X, ST_DIV2_Y, ST_IN_W, ST_BORDER);

  for (int i = 0; i < 3; i++) {
    int colCentre = ST_IN_X + i * ST_COL_W + ST_COL_W / 2;
    printCentred(colCentre, ST_COL_LBL_Y, ST_LBL_SIZE, ST_LABEL, ST_COL_LABELS[i]);
    drawStatsColValue(i, stats);
  }

  statsDrawn = stats;
  statsFrameDrawn = true;
  statsDirty = false;
}

// Value-only refresh: repaints just the boxes whose number changed, and the
// bar only when a percentage moved. Never clears the whole tab.
void updateStatsValues() {
  for (int i = 0; i < 4; i++) {
    if (statsCardValue(stats, i) != statsCardValue(statsDrawn, i)) drawStatsCardValue(i, stats);
  }

  if (stats.engagedPct != statsDrawn.engagedPct ||
      stats.distractedPct != statsDrawn.distractedPct ||
      stats.awayBdPct != statsDrawn.awayBdPct) {
    drawStatsBar(stats);
  }

  for (int i = 0; i < 3; i++) {
    int m, p, dm, dp;
    statsBdValues(stats, i, m, p);
    statsBdValues(statsDrawn, i, dm, dp);
    if (m != dm || p != dp) drawStatsBdValue(i, stats);
  }

  for (int i = 0; i < 3; i++) {
    if (statsColValue(stats, i) != statsColValue(statsDrawn, i)) drawStatsColValue(i, stats);
  }

  statsDrawn = stats;
  statsDirty = false;
}

// Bridge handler: unpacks into a local, then copies it into the pending slot
// under the lock and returns. No drawing - loop() does all of that.
int tft_stats(std::vector<int> v) {
  if (v.size() < 13) return 0;
  StatsData s;
  s.focusedMin     = v[0];
  s.distractionMin = v[1];
  s.posturePoorMin = v[2];
  s.awayMin        = v[3];
  s.engagedMin     = v[4];
  s.engagedPct     = v[5];
  s.distractedMin  = v[6];
  s.distractedPct  = v[7];
  s.awayBdMin      = v[8];
  s.awayBdPct      = v[9];
  s.avgPosture     = v[10];
  s.avgEngagement  = v[11];
  s.sessionMin     = v[12];

  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingStats = s;
  pendingStatsDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// Called from loop(): applies a pending stats update, but only while tab 1
// is the one on screen.
void serviceStatsUpdate() {
  if (!statsDirty) return;
  if (currentTab != 1 || !statsFrameDrawn) return;
  updateStatsValues();
}

// ---------------------------------------------------------------------
// Tab 5: Focus (pomodoro). Python owns the whole state machine; this side
// only renders what pomo_state() last pushed.
//
// Layout: a phase label strip, huge MM:SS digits centred in the content
// band, and a progress bar under them. Entering the tab paints the frame
// once; after that only the digits and the bar are repainted, and only when
// their rendered value actually changed - so the once-a-second update costs
// two small fills instead of a full content wipe.
#define POMO_LABEL_SIZE   2
#define POMO_DIGIT_SIZE   8                       // 8*6=48px per char, 8*8=64px tall
#define POMO_DIGIT_W      (CHAR_W(POMO_DIGIT_SIZE) * 5)  // "MM:SS" is 5 glyphs
#define POMO_DIGIT_H      (8 * POMO_DIGIT_SIZE)
#define POMO_LABEL_Y      (CONTENT_TOP + 6)
#define POMO_DIGIT_Y      (CONTENT_TOP + 54)
#define POMO_BAR_H        14
#define POMO_BAR_Y        (POMO_DIGIT_Y + POMO_DIGIT_H + 26)
#define POMO_BAR_X        MARGIN
#define POMO_BAR_W        (SCREEN_W - 2 * MARGIN)

// Card panel the whole tab sits on - same bento treatment as the other
// tabs' stat blocks, drawn once behind the label/digits/bar on tab entry.
// The per-element repaints below erase to COLOR_SURFACE (the card's own
// fill), not COLOR_BG, so a same-tab digit/bar update doesn't punch a
// background-coloured hole through the card.
#define POMO_CARD_X       MARGIN
#define POMO_CARD_Y       CONTENT_TOP
#define POMO_CARD_W       (SCREEN_W - 2 * MARGIN)
#define POMO_CARD_H       (CONTENT_BOTTOM - CONTENT_TOP)
#define POMO_CARD_RADIUS  12

#define POMO_PHASE_IDLE   0
#define POMO_PHASE_FOCUS  1
#define POMO_PHASE_SHORT  2
#define POMO_PHASE_LONG   3

// Newest state from Python plus what is currently on the glass, so the
// per-second refresh can diff instead of repainting blindly.
PomoData pomo = {0, 0, 0, 0};
bool     pomoDirty = false;
bool     pomoFrameDrawn = false;   // cleared whenever another tab takes the screen
String   pomoDrawnDigits = CACHE_INVALID;
int      pomoDrawnFillW = -1;
int      pomoDrawnPhase = -1;
int      pomoDrawnRunning = -1;

uint16_t pomoPhaseColor(int phase) {
  // Focus green / break violet, matching the web Focus tab's phase pill.
  switch (phase) {
    case POMO_PHASE_FOCUS: return RGB565(124, 242, 154);
    case POMO_PHASE_SHORT:
    case POMO_PHASE_LONG:  return RGB565(183, 156, 255);
    default:               return COLOR_LABEL;
  }
}

String pomoPhaseLabel(const PomoData &p) {
  if (p.phase == POMO_PHASE_FOCUS) return p.running ? "FOCUS" : "FOCUS - PAUSED";
  if (p.phase == POMO_PHASE_SHORT) return p.running ? "SHORT BREAK" : "SHORT BREAK - PAUSED";
  if (p.phase == POMO_PHASE_LONG)  return p.running ? "LONG BREAK" : "LONG BREAK - PAUSED";
  return "IDLE - TAP TO START";
}

String pomoDigits(const PomoData &p) {
  int secs = p.remainingS;
  if (secs < 0) secs = 0;
  int mm = secs / 60;
  int ss = secs % 60;
  if (mm > 99) mm = 99;
  char buf[8];
  snprintf(buf, sizeof(buf), "%02d:%02d", mm, ss);
  return String(buf);
}

int pomoFillWidth(const PomoData &p) {
  if (p.totalS <= 0) return 0;
  int elapsed = p.totalS - p.remainingS;
  if (elapsed < 0) elapsed = 0;
  if (elapsed > p.totalS) elapsed = p.totalS;
  return (int)((long)POMO_BAR_W * elapsed / p.totalS);
}

// Phase label: its own strip, repainted only when the phase or run state
// changes (not once a second).
void drawPomoLabel(const PomoData &p) {
  int h = 8 * POMO_LABEL_SIZE + 4;
  gfx->fillRect(0, POMO_LABEL_Y, SCREEN_W, h, COLOR_SURFACE);
  String label = pomoPhaseLabel(p);
  int maxChars = (SCREEN_W - 2 * MARGIN) / CHAR_W(POMO_LABEL_SIZE);
  label = truncateToWidth(label, maxChars);
  int w = (int)(label.length() * CHAR_W(POMO_LABEL_SIZE));
  gfx->setTextSize(POMO_LABEL_SIZE);
  gfx->setTextColor(pomoPhaseColor(p.phase));
  gfx->setCursor((SCREEN_W - w) / 2, POMO_LABEL_Y);
  gfx->print(label);
}

void drawPomoDigits(const PomoData &p) {
  String digits = pomoDigits(p);
  int x = (SCREEN_W - POMO_DIGIT_W) / 2;
  gfx->fillRect(x - 4, POMO_DIGIT_Y - 2, POMO_DIGIT_W + 8, POMO_DIGIT_H + 4, COLOR_SURFACE);
  gfx->setTextSize(POMO_DIGIT_SIZE);
  gfx->setTextColor(p.phase == POMO_PHASE_IDLE ? COLOR_LABEL : COLOR_VALUE);
  gfx->setCursor(x, POMO_DIGIT_Y);
  gfx->print(digits);
  pomoDrawnDigits = digits;
}

void drawPomoBar(const PomoData &p) {
  int fill = pomoFillWidth(p);
  gfx->fillRoundRect(POMO_BAR_X, POMO_BAR_Y, POMO_BAR_W, POMO_BAR_H, POMO_BAR_H / 2, COLOR_BORDER);
  if (fill > 0) {
    gfx->fillRoundRect(POMO_BAR_X, POMO_BAR_Y, fill, POMO_BAR_H, POMO_BAR_H / 2, pomoPhaseColor(p.phase));
  }
  pomoDrawnFillW = fill;
}

// Full paint, on entering the tab.
void renderPomoFrame() {
  gfx->fillRect(0, CONTENT_TOP, SCREEN_W, CONTENT_BOTTOM - CONTENT_TOP, COLOR_BG);
  gfx->fillRoundRect(POMO_CARD_X, POMO_CARD_Y, POMO_CARD_W, POMO_CARD_H, POMO_CARD_RADIUS, COLOR_SURFACE);
  gfx->drawRoundRect(POMO_CARD_X, POMO_CARD_Y, POMO_CARD_W, POMO_CARD_H, POMO_CARD_RADIUS, COLOR_BORDER);
  drawPomoLabel(pomo);
  drawPomoDigits(pomo);
  drawPomoBar(pomo);
  pomoDrawnPhase = pomo.phase;
  pomoDrawnRunning = pomo.running;
  pomoFrameDrawn = true;
  pomoDirty = false;
}

// Per-second refresh: digits and bar only, and only when what they'd render
// differs from what is already up.
void updatePomoValues() {
  if (pomo.phase != pomoDrawnPhase || pomo.running != pomoDrawnRunning) {
    drawPomoLabel(pomo);
    pomoDrawnPhase = pomo.phase;
    pomoDrawnRunning = pomo.running;
  }
  if (pomoDigits(pomo) != pomoDrawnDigits) drawPomoDigits(pomo);
  if (pomoFillWidth(pomo) != pomoDrawnFillW) drawPomoBar(pomo);
  pomoDirty = false;
}

// Bridge handler: store-only under pendingLock, exactly like tft_stats.
int pomo_state(int phase, int remaining_s, int total_s, int running) {
  PomoData p;
  p.phase = phase;
  p.remainingS = remaining_s;
  p.totalS = total_s;
  p.running = running;

  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingPomo = p;
  pendingPomoDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// Called from loop(): applies a pending pomodoro update, but only while the
// Focus tab is on screen.
void servicePomoUpdate() {
  if (!pomoDirty) return;
  if (currentTab != TAB_FOCUS || !pomoFrameDrawn) return;
  updatePomoValues();
}

int renderTab(int tabIndex, const String &title, const String &line1, const String &line2,
              const String &line3, const String &line4, int accentIndex) {
  unsigned long t0 = millis();

  // Report how long the touch took to reach a redraw, then disarm. Covers
  // Python's get_tab() poll interval as well as the Bridge hop, so a large
  // number here points at the poll cadence rather than at the SPI bus.
  if (touchEdgeAt != 0) {
    Monitor.print("touch->redraw ms: ");
    Monitor.println(t0 - touchEdgeAt);
    touchEdgeAt = 0;
  }

  drawHeader(tabIndex, title, accentIndex);

  // A real tab switch is the only case that has to wipe the content area -
  // it clears the outgoing tab's rows/face so nothing stale survives. The
  // routine same-tab refresh skips the wipe entirely and repaints only the
  // rows whose text actually changed.
  bool tabChanged = (tabIndex != lastContentTab);
  if (tabChanged) {
    invalidateContentCaches();
    lastContentTab = tabIndex;
  }
  drawTabBar(tabIndex, accentIndex);

  // Tab 1 is the Today view. It ignores the title/line strings Python still
  // sends via draw_tab and renders from the values pushed over "tft_stats"
  // instead, so leaving the tab and coming back repaints the full frame
  // while a routine refresh is a no-op (value updates run from loop()).
  if (tabIndex == 1) {
    if (tabChanged || !statsFrameDrawn) {
      renderStatsFrame();
    } else if (statsDirty) {
      updateStatsValues();
    }
    Monitor.print(tabChanged ? "stats(frame) ms: " : "stats(values) ms: ");
    Monitor.println(millis() - t0);
    return 1;
  }
  // Any other tab is taking the screen, so the stats frame is gone.
  statsFrameDrawn = false;

  // Tab 5 is the Focus (pomodoro) view. Like tab 1 it ignores the
  // title/line strings and renders from its own pushed state, so entering
  // the tab paints the frame and the per-second updates run from loop().
  if (tabIndex == TAB_FOCUS) {
    currentExpression = -1;  // the face is gone from the content area
    if (tabChanged || !pomoFrameDrawn) {
      renderPomoFrame();
    } else if (pomoDirty) {
      updatePomoValues();
    }
    Monitor.print(tabChanged ? "pomo(frame) ms: " : "pomo(values) ms: ");
    Monitor.println(millis() - t0);
    return 1;
  }
  // Any other tab is taking the screen, so the pomodoro frame is gone too.
  pomoFrameDrawn = false;

  if (tabIndex == 0) {
    // Buddy tab: the face itself is owned entirely by draw_face()/updateEyes()
    // (so tab refreshes never blank it out or interrupt an animation in
    // progress). The old state-word strip below the eyes is retired - the
    // eyes' own shape/tint now carries that meaning - so this just keeps the
    // reserved comment strip blank, once per tab entry. line1/accentIndex
    // are unused here now; draw_face() (a separate Bridge call) is what
    // actually drives the face.
    if (tabChanged) {
      gfx->fillRect(0, FACE_BOTTOM, SCREEN_W, CONTENT_BOTTOM - FACE_BOTTOM, COLOR_FACE_BG);
    }
  } else {
    // Any other tab reclaims the whole content area, including wherever the
    // face was drawn - invalidate it so returning to tab 0 always repaints
    // even if the expression itself hasn't changed.
    currentExpression = -1;

    if (tabChanged) {
      int contentH = CONTENT_BOTTOM - CONTENT_TOP;
      gfx->fillRect(0, CONTENT_TOP, SCREEN_W, contentH, COLOR_BG);
    }
    String rows[TAB_ROW_COUNT] = {line1, line2, line3, line4};
    for (int i = 0; i < TAB_ROW_COUNT; i++) {
      if (rows[i] == lastRowText[i]) continue;
      lastRowText[i] = rows[i];
      if (rows[i].length() > 0) {
        drawRow(i, rows[i]);
      } else {
        // Row went empty on a same-tab refresh: wipe the card that used to
        // be here, which is what the old unconditional content clear did.
        int cardY = CONTENT_TOP + i * (CARD_H + CARD_GAP);
        gfx->fillRect(MARGIN, cardY, SCREEN_W - 2 * MARGIN, CARD_H, COLOR_BG);
      }
    }
  }

  Monitor.print(tabChanged ? "draw_tab(switch) ms: " : "draw_tab(refresh) ms: ");
  Monitor.println(millis() - t0);
  return 1;
}

// Bridge entry point: stashes the content for this tab - which is both the
// cache a later touch switch repaints from and the pending buffer loop()
// draws from - then returns. Runs on the Bridge thread, so it must not touch
// the panel; renderTab() above is reached only from loop().
int draw_tab(int tabIndex, String title, String line1, String line2, String line3, String line4, int accentIndex) {
  if (tabIndex < 0 || tabIndex >= TAB_COUNT) return 1;  // main.py only ever sends 0/1/2/4

  k_mutex_lock(&pendingLock, K_FOREVER);
  TabContent &c = tabCache[tabIndex];
  c.valid = true;
  c.isTextBlock = false;
  c.title = title;
  c.line1 = line1;
  c.line2 = line2;
  c.line3 = line3;
  c.line4 = line4;
  c.accentIndex = accentIndex;
  pendingDrawTab = tabIndex;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// Summary tab only: word-wraps free text across up to six lines instead of
// the fixed four-row layout the other tabs use. Always paints the tab-bar
// as tab 3, since that's the only tab that calls this handler.
#define TEXT_BLOCK_TEXT_SIZE 2
#define TEXT_BLOCK_MAX_LINES 6

int renderTextBlock(const String &title, const String &text, int accentIndex) {
  unsigned long t0 = millis();

  if (touchEdgeAt != 0) {
    Monitor.print("touch->redraw ms: ");
    Monitor.println(t0 - touchEdgeAt);
    touchEdgeAt = 0;
  }

  drawHeader(3, title, accentIndex);

  bool tabChanged = (lastContentTab != 3);
  if (tabChanged) {
    invalidateContentCaches();
    lastContentTab = 3;
  }
  drawTabBar(3, accentIndex);

  // Same idea as draw_tab: an unchanged summary on a same-tab refresh is
  // already on the glass, so re-wrapping and repainting it is pure cost.
  if (!tabChanged && text == lastTextBlock) {
    Monitor.print("draw_text_block(skip) ms: ");
    Monitor.println(millis() - t0);
    return 1;
  }
  lastTextBlock = text;

  int contentH = CONTENT_BOTTOM - CONTENT_TOP;
  gfx->fillRect(0, CONTENT_TOP, SCREEN_W, contentH, COLOR_BG);

  gfx->setTextColor(COLOR_VALUE);
  gfx->setTextSize(TEXT_BLOCK_TEXT_SIZE);

  int maxChars = (SCREEN_W - 2 * MARGIN) / CHAR_W(TEXT_BLOCK_TEXT_SIZE);
  int lineH = 8 * TEXT_BLOCK_TEXT_SIZE + 4;
  int len = text.length();
  int start = 0;
  int lineCount = 0;

  while (start < len && lineCount < TEXT_BLOCK_MAX_LINES) {
    int remaining = len - start;
    int take = (remaining < maxChars) ? remaining : maxChars;
    int breakAt = start + take;

    if (take == maxChars && breakAt < len) {
      int lastSpace = text.lastIndexOf(' ', breakAt);
      if (lastSpace > start) breakAt = lastSpace;
    }

    String lineText = text.substring(start, breakAt);
    lineText.trim();
    gfx->setCursor(MARGIN, CONTENT_TOP + lineCount * lineH);
    gfx->print(lineText);

    start = breakAt;
    while (start < len && text[start] == ' ') start++;
    lineCount++;
  }

  return 1;
}

// Bridge entry point, mirroring draw_tab: caches the summary against tab 3
// so a touch switch back to it repaints without Python, and flags it for
// loop() to paint. No drawing on this thread.
int draw_text_block(String title, String text, int accentIndex) {
  k_mutex_lock(&pendingLock, K_FOREVER);
  TabContent &c = tabCache[3];
  c.valid = true;
  c.isTextBlock = true;
  c.title = title;
  c.text = text;
  c.accentIndex = accentIndex;
  pendingDrawTab = 3;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// Called from loop() right after a touch edge moved currentTab: shifts the
// tab-bar highlight and repaints the content area from whatever Python last
// sent for that tab, so the press shows up immediately. Python's next push
// overwrites this - and is a no-op if it carries the same content.
void serviceTabRedraw() {
  if (!pendingTabRedraw) return;
  pendingTabRedraw = false;

  // Copy the cache entry out under the lock - the Bridge thread can be
  // writing another push into tabCache at any moment - then draw from the
  // local copy with the lock released.
  int t = currentTab;
  k_mutex_lock(&pendingLock, K_FOREVER);
  TabContent c = tabCache[t];
  k_mutex_unlock(&pendingLock);

  if (c.valid && c.isTextBlock) {
    renderTextBlock(c.title, c.text, c.accentIndex);
  } else if (c.valid) {
    renderTab(t, c.title, c.line1, c.line2, c.line3, c.line4, c.accentIndex);
  } else {
    // Nothing cached for this tab yet: still move the highlight and clear
    // the content area so the press is visibly acknowledged. Accent 3 is
    // the same neutral grey setup() starts with.
    renderTab(t, "", "", "", "", "", 3);
  }
}

// ---------------------------------------------------------------------
// Buddy face (tab 0) continued - MOOD_*/EYE_*/COLOR_EYE_*/COLOR_FACE_BG and
// struct EyeParams/Spring1 are defined up near the #includes (Arduino's
// auto-generated prototypes need them before the first function
// definition, and COLOR_FACE_BG is used below before this point in the
// file) - this section is everything else: the RAM buffers, the
// rasterizer, and the engine itself.
// Static (not heap) so the footprint is fixed and exactly reportable - see
// the byte math above. Flat 1D so a plain uint16_t* can be handed to
// draw16bitRGBBitmap() with no array-decay ambiguity.
uint16_t eyeBuf[2][EYE_BUF_W * EYE_BUF_H];

inline void bufSetPx(int eye, int x, int y, uint16_t color) {
  if (x < 0 || y < 0 || x >= EYE_BUF_W || y >= EYE_BUF_H) return;
  eyeBuf[eye][y * EYE_BUF_W + x] = color;
}

// Inclusive rounded-rect fill via a per-pixel nearest-corner distance test -
// simple and obviously correct; buffers are small enough (<= 188x178) that
// this comfortably clears a 20-25fps budget on a Cortex-M33.
void bufFillRoundRect(int eye, int x0, int y0, int w, int h, int r, uint16_t color) {
  if (w <= 0 || h <= 0) return;
  if (r < 0) r = 0;
  if (r > w / 2) r = w / 2;
  if (r > h / 2) r = h / 2;
  int x1 = x0 + w - 1, y1 = y0 + h - 1;
  int cxL = x0 + r, cxR = x1 - r, cyT = y0 + r, cyB = y1 - r;
  for (int y = y0; y <= y1; y++) {
    bool topBand = y < cyT, botBand = y > cyB;
    for (int x = x0; x <= x1; x++) {
      if ((topBand || botBand) && (x < cxL || x > cxR)) {
        int ccx = (x < cxL) ? cxL : cxR;
        int ccy = topBand ? cyT : cyB;
        int dx = x - ccx, dy = y - ccy;
        if (dx * dx + dy * dy > r * r) continue;
      }
      bufSetPx(eye, x, y, color);
    }
  }
}

// Cuts `cut` px (at the deepest point) off the top of the shape, sloping by
// `tilt` px across its width so one side cuts deeper - the worried/sad
// asymmetry. deepenRight selects which side is deep (true = the shape's own
// right edge, i.e. the left eye's inner corner; false = its left edge, the
// right eye's inner corner).
void bufCutTopLid(int eye, int x0, int y0, int w, int cut, int tilt, bool deepenRight) {
  if (cut <= 0 && tilt == 0) return;
  for (int dx = 0; dx < w; dx++) {
    float t = (w > 1) ? (float)dx / (w - 1) : 0.0f;
    if (!deepenRight) t = 1.0f - t;
    int rowCut = cut + (int)lroundf(tilt * t);
    for (int dy = 0; dy < rowCut; dy++) bufSetPx(eye, x0 + dx, y0 + dy, COLOR_FACE_BG);
  }
}

void bufCutBottomLid(int eye, int x0, int y0, int h, int w, int cut) {
  if (cut <= 0) return;
  for (int dx = 0; dx < w; dx++) {
    for (int dy = 0; dy < cut; dy++) bufSetPx(eye, x0 + dx, y0 + h - 1 - dy, COLOR_FACE_BG);
  }
}

// struct EyeParams (the 8 parameters every expression/motion acts through)
// is defined up near the #includes - see the note there.
bool eyeParamsEqual(const EyeParams &a, const EyeParams &b) {
  return a.offsetX == b.offsetX && a.offsetY == b.offsetY && a.width == b.width &&
         a.height == b.height && a.radius == b.radius && a.topCut == b.topCut &&
         a.bottomCut == b.bottomCut && a.tilt == b.tilt && a.color == b.color;
}

// Renders one eye (clear -> 2 glow rings, largest/dimmest first -> eye core
// -> lid cuts) into its buffer. Never touches the screen.
void renderEyeToBuffer(int eye, const EyeParams &p, bool deepenRight) {
  memset(eyeBuf[eye], 0, sizeof(eyeBuf[eye]));  // RGB565 black is all-zero bits

  int cx = EYE_BUF_W / 2 + (int)lroundf(p.offsetX);
  int cy = EYE_BUF_H / 2 + (int)lroundf(p.offsetY);
  int w = (int)lroundf(p.width), h = (int)lroundf(p.height);
  int r = (int)lroundf(p.radius);
  if (r > w / 2) r = w / 2;
  if (r > h / 2) r = h / 2;

  int g2 = 2 * GLOW_RING_GAP, g1 = GLOW_RING_GAP;
  bufFillRoundRect(eye, cx - w / 2 - g2, cy - h / 2 - g2, w + 2 * g2, h + 2 * g2, r + g2, COLOR_EYE_RING2);
  bufFillRoundRect(eye, cx - w / 2 - g1, cy - h / 2 - g1, w + 2 * g1, h + 2 * g1, r + g1, COLOR_EYE_RING1);
  bufFillRoundRect(eye, cx - w / 2, cy - h / 2, w, h, r, p.color);

  int topCut = (int)lroundf(p.topCut), botCut = (int)lroundf(p.bottomCut), tilt = (int)lroundf(p.tilt);
  if (topCut > 0 || tilt != 0) bufCutTopLid(eye, cx - w / 2, cy - h / 2, w, topCut, tilt, deepenRight);
  if (botCut > 0) bufCutBottomLid(eye, cx - w / 2, cy - h / 2, h, w, botCut);
}

void blitEye(int eye) {
  int x = (eye == EYE_L) ? EYE_BLIT_L_X : EYE_BLIT_R_X;
  gfx->draw16bitRGBBitmap(x, EYE_BLIT_Y, eyeBuf[eye], EYE_BUF_W, EYE_BUF_H);
}

// struct Spring1 (slight overshoot, ~250ms settle - every animatable param
// rides one of these instead of a linear tween) and SPRING_K/SPRING_DAMPING
// are defined up near the #includes - see the note there.
void springStep(Spring1 &s, float dt) {
  float accel = (s.target - s.value) * SPRING_K - s.vel * SPRING_DAMPING;
  s.vel += accel * dt;
  s.value += s.vel * dt;
}

// Both eyes share one "shape" spring set (symmetric moods) plus one shared
// gaze target (eyes track together, like the old glide) - per-eye asymmetry
// (the worried lid's deep side, L/R micro-drift) is applied at render time.
Spring1 eyeW   = {EYE_NOM_W, 0, EYE_NOM_W};
Spring1 eyeH   = {EYE_NOM_H, 0, EYE_NOM_H};
Spring1 eyeR   = {EYE_NOM_R, 0, EYE_NOM_R};
Spring1 eyeTopCut = {0, 0, 0};
Spring1 eyeBotCut = {0, 0, 0};
Spring1 eyeTilt   = {0, 0, 0};
Spring1 gazeX  = {0, 0, 0};
Spring1 gazeY  = {0, 0, 0};

struct MoodTarget { float w, h, r, topCut, botCut, tilt; uint16_t color; };
const MoodTarget MOOD_TARGETS[MOOD_COUNT] = {
  /* ENGAGED     */ { EYE_NOM_W,        EYE_NOM_H,         EYE_NOM_R,        0,  0, 0,  COLOR_EYE_CORE },
  /* DISTRACTED  */ { EYE_NOM_W * 0.90f, EYE_NOM_H * 0.90f, EYE_NOM_R * 0.90f, 0,  0, 0,  RGB565(245, 230, 99) },   // #F5E663
  /* BAD_POSTURE */ { EYE_NOM_W,        EYE_NOM_H * 0.82f, EYE_NOM_R,        34, 0, 10, RGB565(255, 138, 101) },   // #FF8A65
  /* ABSENT      */ { EYE_NOM_W,        EYE_NOM_H * 0.14f, EYE_NOM_R * 0.3f, 0,  0, 0,  RGB565(183, 156, 255) },   // #B79CFF
  /* FOCUSED     */ { EYE_NOM_W * 0.95f, EYE_NOM_H * 0.75f, EYE_NOM_R,        0,  0, 0,  COLOR_EYE_CORE },
};
// MOOD_FOCUSED's row above is real and renders correctly, but nothing ever
// sets eyeMood to 4 today - Python's draw_face() only carries 0-3 (see the
// block comment at the top of this section).

int eyeMood = MOOD_ENGAGED;
unsigned long lastEyeFrameAt = 0;  // EYE_FRAME_INTERVAL_MS is defined up near the #includes

// Idle saccades: new target every 1.5-4s (mood-flavoured), quick jump, with
// an occasional immediate double-glance.
unsigned long nextSaccadeAt = 0;
bool doubleGlancePending = false;

// Micro-drift: tiny continuous wander, layered on top of the gaze spring's
// target rather than replacing it.
float microDriftPhase = 0.0f;

// Breathing: +-3% height pulse, ~4s cycle.
float breathPhase = 0.0f;

// Blink: engaged/focused only (the other moods' lids already carry their
// own shape). Occasional double blink, same 6-frame squash table as before.
const int BLINK_HEIGHTS[6] = {77, 43, 10, 43, 77, 100};  // % of target height
// BLINK_FRAMES/BLINK_FRAME_MS are defined up near the #includes
bool blinkActive = false;
int blinkFrame = 0;
unsigned long blinkNextFrameAt = 0;
unsigned long nextBlinkAt = 0;
bool doubleBlinkPending = false;

// Mood-change react: a quick stretch-tall bump (~120ms) overlaid on the
// shape spring's target before it settles into the new mood - never an
// instant snap.
bool reactActive = false;
unsigned long reactStartAt = 0;  // REACT_MS is defined up near the #includes

// Happy: upward-arc squint + a small bob, held ~3s - see the block comment
// above for how this gets triggered from the pomodoro hook.
bool happyActive = false;
unsigned long happyUntil = 0;  // HAPPY_HOLD_MS is defined up near the #includes

// look_at(): Python sends the detected face/head centre while a person is
// present; this replaces idle saccades while fresh and falls back to idle
// 2s after the last update. Store-only under pendingLock, like every other
// Bridge handler - loop() copies it out below.
int pendingLookAtX = 0, pendingLookAtY = 0;
bool pendingLookAtDirty = false;
int lookAtX = 0, lookAtY = 0;
unsigned long lastLookAtAt = 0;  // 0 = never tracked yet this boot - LOOK_AT_FRESH_MS is defined up near the #includes

int look_at(int x, int y) {
  if (x < -100) x = -100; else if (x > 100) x = 100;
  if (y < -100) y = -100; else if (y > 100) y = 100;
  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingLookAtX = x;
  pendingLookAtY = y;
  pendingLookAtDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// Frame-push timing, averaged and printed once a minute.
unsigned long eyeFrameTimeSumUs = 0;
unsigned long eyeFrameTimeCount = 0;
unsigned long lastEyeFrameReportAt = 0;

// A small drifting "z" above the eyes while absent/drowsy - screen-space
// text, not part of the eye buffers, updated only every ~600ms (cheap) and
// erased before each move.
int zDriftY = 0;
unsigned long nextZMoveAt = 0;
bool zVisible = false;

// Repaints the whole face only when the mood actually changed (or was
// invalidated by another tab) - otherwise a cheap no-op, so the periodic
// Python refresh never fights the animation below.
//
// loop()-side only: this draws (clears the band so updateEyes() starts from
// a known state) and arms the mood-change react / happy flourish, so it must
// stay on the same thread as updateEyes(). draw_face() below is the Bridge
// entry point that feeds it.
void drawFaceNow(int expression) {
  if (expression < 0 || expression > 4) expression = MOOD_ENGAGED;  // unknown -> neutral
  if (expression == currentExpression) return;

  bool enteringEngaged = (expression == MOOD_ENGAGED);
  currentExpression = expression;
  eyeMood = expression;

  reactActive = true;
  reactStartAt = millis();
  blinkActive = false;
  doubleBlinkPending = false;
  nextBlinkAt = millis() + random(3000, 6001);
  if (enteringEngaged) {
    happyActive = true;
    happyUntil = millis() + HAPPY_HOLD_MS;
  }

  gfx->fillRect(0, FACE_TOP, SCREEN_W, FACE_AREA_H, COLOR_FACE_BG);
  zVisible = false;
  lastEyeFrameAt = 0;  // next updateEyes() call treats this as frame 1 - draws immediately
}

// Bridge entry point: records the expression and returns. The repaint, the
// unchanged-expression early-out and the mood-change reaction all happen in
// loop() via drawFaceNow(), so this thread never reaches the panel.
int draw_face(int expression) {
  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingFace = expression;
  pendingFaceDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// The eye engine's per-frame tick - replaces the old updateBlink(). Targets
// 20-25fps; a late frame (e.g. a slow SPI push) just moves lastEyeFrameAt to
// "now" rather than "+= interval", so time is skipped ahead, never queued up
// to catch up.
void updateEyes() {
  if (currentTab != 0) return;  // leaving the tab stops the animation entirely

  unsigned long now = millis();
  if (lastEyeFrameAt != 0 && (now - lastEyeFrameAt) < EYE_FRAME_INTERVAL_MS) return;
  float dt = (lastEyeFrameAt == 0) ? (EYE_FRAME_INTERVAL_MS / 1000.0f) : (now - lastEyeFrameAt) / 1000.0f;
  if (dt > 0.25f) dt = 0.25f;  // guard a huge dt (e.g. right after the blocking tft-recovery path)
  lastEyeFrameAt = now;

  unsigned long frameStartUs = micros();

  const MoodTarget &mt = MOOD_TARGETS[eyeMood];
  float targetW = mt.w, targetH = mt.h, targetR = mt.r;

  if (reactActive) {
    float t = (now - reactStartAt) / (float)REACT_MS;
    if (t >= 1.0f) {
      reactActive = false;
    } else {
      float bump = sinf(t * 3.14159f);  // peaks mid-react, back to 0 at the end
      targetH = mt.h * (1.0f + 0.35f * bump);
      targetW = mt.w * (1.0f - 0.12f * bump);
    }
  }

  bool trackingGaze = (lastLookAtAt != 0) && (now - lastLookAtAt) < LOOK_AT_FRESH_MS;

  if (happyActive) {
    if ((long)(now - happyUntil) >= 0) {
      happyActive = false;
    } else {
      float bouncePhase = (now % 400) / 400.0f;
      targetH = EYE_NOM_H * 0.55f;   // upward-arc squint
      targetR = EYE_NOM_R * 1.3f;
      if (!trackingGaze) gazeY.target = -6.0f + sinf(bouncePhase * 2.0f * 3.14159f) * 3.0f;  // small bob
    }
  }

  eyeW.target = targetW;
  eyeH.target = targetH;
  eyeR.target = targetR;
  eyeTopCut.target = mt.topCut;
  eyeBotCut.target = mt.botCut;
  eyeTilt.target = mt.tilt;

  // Breathing: independent of the mood spring, always running.
  breathPhase += dt * (2.0f * 3.14159f / 4.0f);  // ~4s cycle
  float breath = 1.0f + 0.03f * sinf(breathPhase);

  // Gaze: look_at() while fresh, else idle saccades (mood-flavoured
  // interval + an occasional biased target), with an occasional
  // double-glance re-target shortly after the first.
  if (trackingGaze) {
    gazeX.target = (lookAtX / 100.0f) * GAZE_RANGE_X;
    if (!happyActive) gazeY.target = (lookAtY / 100.0f) * GAZE_RANGE_Y;
  } else if (now >= nextSaccadeAt) {
    gazeX.target = (float)random(-GAZE_RANGE_X, GAZE_RANGE_X + 1);
    if (!happyActive) gazeY.target = (float)random(-GAZE_RANGE_Y, GAZE_RANGE_Y + 1);
    if (eyeMood == MOOD_BAD_POSTURE && random(100) < 35) gazeY.target = GAZE_RANGE_Y;  // occasional slow look-down
    unsigned long minMs = 1500, maxMs = 4000;
    if (eyeMood == MOOD_DISTRACTED) { minMs = 700; maxMs = 1800; }    // restless
    if (eyeMood == MOOD_ABSENT)     { minMs = 2500; maxMs = 5000; }   // slow sway
    if (!doubleGlancePending && random(100) < 20) {
      doubleGlancePending = true;
      nextSaccadeAt = now + random(250, 450);
    } else {
      doubleGlancePending = false;
      nextSaccadeAt = now + random(minMs, maxMs + 1);
    }
  }

  microDriftPhase += dt * 1.3f;
  float driftX = 2.0f * sinf(microDriftPhase);
  float driftY = 2.0f * cosf(microDriftPhase * 0.8f);

  springStep(eyeW, dt);
  springStep(eyeH, dt);
  springStep(eyeR, dt);
  springStep(eyeTopCut, dt);
  springStep(eyeBotCut, dt);
  springStep(eyeTilt, dt);
  springStep(gazeX, dt);
  springStep(gazeY, dt);

  bool blinkEligible = (eyeMood == MOOD_ENGAGED || eyeMood == MOOD_FOCUSED) && !happyActive;
  float blinkMul = 1.0f;
  if (blinkEligible) {
    if (!blinkActive && now >= nextBlinkAt) {
      blinkActive = true;
      blinkFrame = 0;
      blinkNextFrameAt = now;
    }
    if (blinkActive && now >= blinkNextFrameAt) {
      blinkFrame++;
      blinkNextFrameAt = now + BLINK_FRAME_MS;
      if (blinkFrame >= BLINK_FRAMES) {
        blinkActive = false;
        bool doDouble = !doubleBlinkPending && random(100) < 15;
        doubleBlinkPending = doDouble;
        nextBlinkAt = now + (doDouble ? random(150, 300) : random(3000, 7001));
      }
    }
    int frameIdx = blinkActive ? blinkFrame : (BLINK_FRAMES - 1);
    if (frameIdx >= BLINK_FRAMES) frameIdx = BLINK_FRAMES - 1;
    if (blinkActive) blinkMul = BLINK_HEIGHTS[frameIdx] / 100.0f;
  } else {
    blinkActive = false;
  }

  EyeParams p;
  p.width = eyeW.value;
  p.height = eyeH.value * breath * blinkMul;
  p.radius = eyeR.value;
  p.topCut = eyeTopCut.value;
  p.bottomCut = eyeBotCut.value;
  p.tilt = eyeTilt.value;
  p.color = mt.color;  // not sprung - an instant tint change reads fine next to the shape spring
  p.offsetX = gazeX.value + driftX;
  p.offsetY = gazeY.value + driftY;

  static EyeParams lastRendered = {-9999, 0, 0, 0, 0, 0, 0, 0, 0};
  if (!eyeParamsEqual(p, lastRendered)) {
    renderEyeToBuffer(EYE_L, p, /*deepenRight=*/true);
    renderEyeToBuffer(EYE_R, p, /*deepenRight=*/false);
    blitEye(EYE_L);
    blitEye(EYE_R);
    lastRendered = p;
  }

  // Drifting "z" while absent - slow upward drift above the eyes, reset
  // once it's drifted far enough. Cheap: moved only every ~600ms.
  if (eyeMood == MOOD_ABSENT) {
    if (now >= nextZMoveAt) {
      if (zVisible) {
        gfx->fillRect(EYE_R_CX + 40, EYE_CY - 70 + zDriftY, 20, 16, COLOR_FACE_BG);
      }
      zDriftY -= 6;
      if (zDriftY < -36) zDriftY = 0;
      gfx->setTextSize(2);
      gfx->setTextColor(COLOR_EYE_RING1);
      gfx->setCursor(EYE_R_CX + 40, EYE_CY - 70 + zDriftY);
      gfx->print("z");
      zVisible = true;
      nextZMoveAt = now + 600;
    }
  } else if (zVisible) {
    gfx->fillRect(EYE_R_CX + 40, EYE_CY - 70 + zDriftY, 20, 16, COLOR_FACE_BG);
    zVisible = false;
  }

  unsigned long frameUs = micros() - frameStartUs;
  eyeFrameTimeSumUs += frameUs;
  eyeFrameTimeCount++;
  if (now - lastEyeFrameReportAt >= 60000) {
    lastEyeFrameReportAt = now;
    if (eyeFrameTimeCount > 0) {
      Monitor.print("eye frame push avg us: ");
      Monitor.println(eyeFrameTimeSumUs / eyeFrameTimeCount);
    }
    eyeFrameTimeSumUs = 0;
    eyeFrameTimeCount = 0;
  }
}

// ---------------------------------------------------------------------
// Capacitive touch pads (tab navigation): three TTP223 modules, active-high,
// latching high while touched. D2 = previous tab, D3 = next tab, D7 = action.
// Python owns what's drawn for each tab - this side only tracks the index
// and a one-shot action flag; it never repaints the screen.
#define TOUCH_PIN_PREV   2
#define TOUCH_PIN_NEXT   3
#define TOUCH_PIN_ACTION 7
const int TOUCH_PINS[3] = {TOUCH_PIN_PREV, TOUCH_PIN_NEXT, TOUCH_PIN_ACTION};
#define TOUCH_DEBOUNCE_MS 40
// TAB_COUNT is declared near the top of the file, shared with the TFT chrome.

bool touchLastRaw[3] = {LOW, LOW, LOW};
bool touchDebounced[3] = {LOW, LOW, LOW};
unsigned long touchLastChangeAt[3] = {0, 0, 0};

// currentTab is declared near the top of the file, shared with the TFT chrome.
bool actionPressed = false;

// D7 does two jobs, told apart purely by how long it is held:
//
//   short tap (release before TFT_RECOVER_HOLD_MS) -> one-shot action flag,
//       which Python reads via get_action(). On the Focus tab that toggles
//       the pomodoro; on the Summary tab it still triggers the LLM summary.
//   hold >= TFT_RECOVER_HOLD_MS                    -> display recovery.
//
// The one-shot therefore arms on *release*, not on the press: arming on the
// press (as it used to) meant a 3 s recovery hold also fired an action,
// because Python polls get_action roughly once a second and would consume
// the flag long before the hold completed. actionHoldConsumed is what stops
// the release at the end of a recovery hold from registering as a tap too.
#define TFT_RECOVER_HOLD_MS 3000
unsigned long actionHeldSince = 0;    // 0 = action pad not currently held
bool actionHoldConsumed = false;      // this press already fired recovery
bool tftRecoverPending = false;       // set here, serviced by loop()

// Fires once per low-to-high transition, after debounce settles - never
// while the pad is just being held down.
void handleTouchEdge(int index) {
  if (index == 0 || index == 1) {
    // Stamped here, consumed by the next draw_tab()/draw_text_block(). The
    // second number is the debounce window this edge already spent waiting
    // in pollTouchPads() before we got here - pure pre-draw latency.
    touchEdgeAt = millis();
    Monitor.print("touch debounce ms: ");
    Monitor.println(touchEdgeAt - touchLastChangeAt[index]);
  }

  if (index == 0) {
    currentTab = (currentTab - 1 + TAB_COUNT) % TAB_COUNT;
    pendingTabRedraw = true;  // serviced by loop(), not drawn from here
    Monitor.print("tab prev -> ");
    Monitor.println(currentTab);
  } else if (index == 1) {
    currentTab = (currentTab + 1) % TAB_COUNT;
    pendingTabRedraw = true;
    Monitor.print("tab next -> ");
    Monitor.println(currentTab);
  }
  // index == 2 (the action pad) deliberately does nothing here - see the
  // tap-vs-hold note above; it arms on release in pollTouchPads().
}

void pollTouchPads() {
  unsigned long now = millis();
  for (int i = 0; i < 3; i++) {
    bool reading = digitalRead(TOUCH_PINS[i]);
    if (reading != touchLastRaw[i]) {
      touchLastChangeAt[i] = now;
      touchLastRaw[i] = reading;
    }
    if ((now - touchLastChangeAt[i]) > TOUCH_DEBOUNCE_MS) {
      if (reading != touchDebounced[i]) {
        touchDebounced[i] = reading;
        if (touchDebounced[i] == HIGH) {
          handleTouchEdge(i);
          if (i == 2) {
            actionHeldSince = now;        // start timing this press
            actionHoldConsumed = false;
          }
        } else if (i == 2) {
          // Released. A press that never reached the hold threshold is a
          // tap, and only now does it become an action for Python to read.
          if (!actionHoldConsumed) {
            actionPressed = true;
            Monitor.print("action tap (");
            Monitor.print(now - actionHeldSince);
            Monitor.println(" ms)");
          }
          actionHeldSince = 0;
          actionHoldConsumed = false;
        }
      }
    }
  }

  // Display recovery arms once per hold: actionHoldConsumed both stops it
  // re-triggering while the pad stays down and marks the coming release as
  // "not a tap".
  if (actionHeldSince != 0 && !actionHoldConsumed &&
      (now - actionHeldSince) >= TFT_RECOVER_HOLD_MS) {
    actionHoldConsumed = true;
    tftRecoverPending = true;
    Monitor.println("action held 3s -> tft recover");
  }
}

int get_tab() {
  return currentTab;
}

bool get_action() {
  bool a = actionPressed;
  actionPressed = false;
  return a;
}

void set_relay(int channel, bool state) {
  if (channel < 0 || channel > 2) return;
  digitalWrite(RELAY_PINS[channel], RELAY_ACTIVE_LOW ? !state : state);
  if (channel == 0) {
    digitalWrite(LED_BUILTIN, state ? LOW : HIGH);
  }
  Monitor.print("relay ");
  Monitor.print(channel);
  Monitor.println(state ? " ON" : " OFF");
}

// ---------------------------------------------------------------------
// Buzzer + status LEDs: active-HIGH, buzzer through an NPN transistor, LEDs
// through 1k resistors. set_leds()/beep() are store-only under pendingLock
// like the TFT handlers - loop() (serviceLedsAndBuzzer(), called from
// loop() itself) is the only thing that ever writes these pins. Beep timing
// is non-blocking (millis(), no delay()), so a 300ms beep never stalls
// loop() or any Bridge call.
#define BUZZER_PIN    4
#define LED_RED_PIN   5
#define LED_GREEN_PIN 6
#define LED_BLUE_PIN  14  // A0 - this core doesn't define the A0 macro, see RELAY_PINS' comment above

bool pendingLedR = false, pendingLedG = false, pendingLedB = false;
bool pendingLedsDirty = false;
int  pendingBeepMs = 0;
bool pendingBeepDirty = false;

int set_leds(bool r, bool g, bool b) {
  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingLedR = r;
  pendingLedG = g;
  pendingLedB = b;
  pendingLedsDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

int beep(int ms) {
  if (ms < 0) ms = 0;
  if (ms > 5000) ms = 5000;  // sane upper bound against a bad call
  k_mutex_lock(&pendingLock, K_FOREVER);
  pendingBeepMs = ms;
  pendingBeepDirty = true;
  k_mutex_unlock(&pendingLock);
  return 1;
}

// loop()-side only: what's actually on the pins right now, plus the
// buzzer's non-blocking off-deadline.
bool ledRNow = false, ledGNow = false, ledBNow = false;
bool buzzerOn = false;
unsigned long buzzerOffAt = 0;

void serviceLedsAndBuzzer() {
  bool haveLeds = false, r = false, g = false, b = false;
  bool haveBeep = false;
  int beepMs = 0;

  k_mutex_lock(&pendingLock, K_FOREVER);
  if (pendingLedsDirty) {
    r = pendingLedR;
    g = pendingLedG;
    b = pendingLedB;
    pendingLedsDirty = false;
    haveLeds = true;
  }
  if (pendingBeepDirty) {
    beepMs = pendingBeepMs;
    pendingBeepDirty = false;
    haveBeep = true;
  }
  k_mutex_unlock(&pendingLock);

  if (haveLeds) {
    if (r != ledRNow) { ledRNow = r; digitalWrite(LED_RED_PIN, r ? HIGH : LOW); }
    if (g != ledGNow) { ledGNow = g; digitalWrite(LED_GREEN_PIN, g ? HIGH : LOW); }
    if (b != ledBNow) { ledBNow = b; digitalWrite(LED_BLUE_PIN, b ? HIGH : LOW); }
  }

  // A new beep request always (re)arms the off-deadline, even if one was
  // already running - so two beep() calls close together just extend/reset
  // the buzzer rather than fighting each other.
  if (haveBeep && beepMs > 0) {
    buzzerOn = true;
    digitalWrite(BUZZER_PIN, HIGH);
    buzzerOffAt = millis() + (unsigned long)beepMs;
  }

  if (buzzerOn && (long)(millis() - buzzerOffAt) >= 0) {
    buzzerOn = false;
    digitalWrite(BUZZER_PIN, LOW);
  }
}

void lcd_show(String line1, String line2) {
  if (!lcdPresent) return;
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1);
  lcd.setCursor(0, 1);
  lcd.print(line2);
}

// TEMPORARY diagnostic for the OLED bring-up (Step 1) - not part of normal
// operation, safe to remove once the OLED's address is confirmed. Runs once
// at boot, after Wire.begin(), before any device driver touches the bus.
void i2c_scan() {
  Monitor.println("I2C scan starting...");
  int found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    byte err = Wire.endTransmission();
    if (err == 0) {
      Monitor.print("I2C device found at 0x");
      Monitor.println(String(addr, HEX));
      found++;
    }
  }
  Monitor.print("I2C scan done, devices found: ");
  Monitor.println(found);
}

void setup() {
  // Buzzer/LEDs safed first, before anything else runs (including
  // Monitor.begin()) - these pins must never glow/beep from a floating or
  // momentarily-wrong level at boot.
  pinMode(BUZZER_PIN, OUTPUT);     digitalWrite(BUZZER_PIN, LOW);
  pinMode(LED_RED_PIN, OUTPUT);    digitalWrite(LED_RED_PIN, LOW);
  pinMode(LED_GREEN_PIN, OUTPUT);  digitalWrite(LED_GREEN_PIN, LOW);
  pinMode(LED_BLUE_PIN, OUTPUT);   digitalWrite(LED_BLUE_PIN, LOW);

  Monitor.begin(9600);
  randomSeed(micros());  // varies the blink interval a little across boots

  for (int i = 0; i < 3; i++) {
    pinMode(RELAY_PINS[i], OUTPUT);
    digitalWrite(RELAY_PINS[i], RELAY_ACTIVE_LOW ? HIGH : LOW);
  }
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);

  for (int i = 0; i < 3; i++) {
    pinMode(TOUCH_PINS[i], INPUT);
  }

  Wire.begin();
  i2c_scan();

  int lcdStatus = lcd.begin(16, 2);
  lcdPresent = (lcdStatus == 0);
  if (lcdPresent) {
    lcd.backlight();
    lcd.clear();
    lcd.print("DeskSense");
  }

  Monitor.print("lcd:");
  Monitor.println(lcdPresent);

  if (!gfx->begin(TFT_SPI_SPEED)) {
    Monitor.println("tft gfx->begin() FAILED");
  } else {
    Serial.begin(9600);
    Serial.println("driver: Arduino_ILI9488_18bit, bus: Arduino_HWSPI(DC=8,CS=10), rotation: 1, IPS: true, spi_speed: 8000000");
    Serial.println("ILI9488 init done");

    // One-time boot test: confirms the new panel is alive and correctly
    // addressed before the existing UI takes over the screen. The red fill
    // is timed to give a baseline for one full-screen write at the current
    // SPI speed - the upper bound on any redraw.
    unsigned long fsT0 = millis();
    gfx->fillScreen(RGB565_RED);
    unsigned long fsMs = millis() - fsT0;
    Monitor.print("fillScreen ms @ ");
    Monitor.print(TFT_SPI_SPEED);
    Monitor.print(" Hz: ");
    Monitor.println(fsMs);
    delay(500);
    gfx->fillScreen(RGB565_GREEN);
    delay(500);
    gfx->fillScreen(RGB565_BLUE);
    delay(500);

    gfx->setTextWrap(false);  // wrapping left residue on the next row - truncate instead
    gfx->fillScreen(COLOR_BG);
    drawHeader(0, "", 3);  // accent 3 = blue-grey, a neutral default before Python's first draw_tab
    drawTabBar(0, 3);

    Monitor.println("tft ready");
  }

  Bridge.begin();
  Bridge.provide("set_relay", set_relay);
  Bridge.provide("lcd_show", lcd_show);
  Bridge.provide("draw_tab", draw_tab);
  Bridge.provide("tft_stats", tft_stats);
  Bridge.provide("draw_text_block", draw_text_block);
  Bridge.provide("draw_face", draw_face);
  Bridge.provide("pomo_state", pomo_state);
  Bridge.provide("look_at", look_at);
  Bridge.provide("set_leds", set_leds);
  Bridge.provide("beep", beep);
  Bridge.provide("get_tab", get_tab);
  Bridge.provide("get_action", get_action);

  Monitor.println("bridge ready");
}

// ---------------------------------------------------------------------
// loop()-side pumps for the work the Bridge handlers deferred. Everything
// below runs on the loop() thread, which is the only thread that draws.

// Applies whatever Python pushed since the last pass. The lock is held just
// long enough to copy the pending payloads into locals and is released
// before a single pixel is drawn. Order matches main.py's own call order for
// tab 0 (draw_face then draw_tab), though the face band and the word strip
// below it never overlap anyway.
void serviceBridgeDraws() {
  bool      haveFace = false;
  int       face = 0;
  bool      haveStats = false;
  StatsData newStats;
  bool      havePomo = false;
  PomoData  newPomo;
  bool      haveLookAt = false;
  int       lookAtXNew = 0, lookAtYNew = 0;
  int       drawTab = -1;
  TabContent c;

  k_mutex_lock(&pendingLock, K_FOREVER);
  if (pendingFaceDirty) {
    face = pendingFace;
    pendingFaceDirty = false;
    haveFace = true;
  }
  if (pendingStatsDirty) {
    newStats = pendingStats;
    pendingStatsDirty = false;
    haveStats = true;
  }
  if (pendingPomoDirty) {
    newPomo = pendingPomo;
    pendingPomoDirty = false;
    havePomo = true;
  }
  if (pendingLookAtDirty) {
    lookAtXNew = pendingLookAtX;
    lookAtYNew = pendingLookAtY;
    pendingLookAtDirty = false;
    haveLookAt = true;
  }
  if (pendingDrawTab >= 0) {
    drawTab = pendingDrawTab;
    c = tabCache[drawTab];  // local copy; tabCache keeps changing under the lock
    pendingDrawTab = -1;
  }
  k_mutex_unlock(&pendingLock);

  if (haveLookAt) {
    lookAtX = lookAtXNew;
    lookAtY = lookAtYNew;
    lastLookAtAt = millis();  // freshness clock the eye engine checks against LOOK_AT_FRESH_MS
  }

  // Stats and pomodoro state first: renderTab()'s tab-1 and tab-5 branches
  // read them (`stats`/`statsDirty`, `pomo`/`pomoDirty`) when a draw_tab
  // push lands in the same pass.
  if (haveStats) {
    stats = newStats;
    statsDirty = true;
  }
  if (havePomo) {
    pomo = newPomo;
    pomoDirty = true;
  }
  if (haveFace) drawFaceNow(face);
  if (drawTab >= 0) {
    if (c.isTextBlock) {
      renderTextBlock(c.title, c.text, c.accentIndex);
    } else {
      renderTab(drawTab, c.title, c.line1, c.line2, c.line3, c.line4, c.accentIndex);
    }
  }
}

// Panel recovery (action pad held 3 s): gfx->begin() pulses RESET and
// replays the ILI9488 init sequence, then every "already on the glass" cache
// is dropped so the repaint that follows is a full one. The init's internal
// delays block loop() for a few hundred ms, which is fine on a path the user
// asked for deliberately.
void serviceTftRecovery() {
  if (!tftRecoverPending) return;
  tftRecoverPending = false;

  Monitor.println("tft recover: re-init + full redraw");
  if (!gfx->begin(TFT_SPI_SPEED)) {
    Monitor.println("tft recover: gfx->begin() FAILED");
  }
  gfx->setTextWrap(false);
  gfx->fillScreen(COLOR_BG);

  int expression = currentExpression;  // kept so the face can be put back below
  lastHeaderTab = -1;
  lastHeaderTitle = CACHE_INVALID;
  lastHeaderAccent = -1;
  lastTabBarTab = -1;
  lastContentTab = -1;
  statsFrameDrawn = false;
  currentExpression = -1;
  invalidateContentCaches();

  // Repaint the current tab exactly the way a touch switch does, then redraw
  // the face if we are on the Buddy tab - renderTab() leaves it alone.
  pendingTabRedraw = true;
  serviceTabRedraw();
  if (currentTab == 0) drawFaceNow(expression < 0 ? FACE_FOCUSED : expression);
}

void loop() {
  static unsigned long lastHeartbeatAt = 0;
  static unsigned long loopCount = 0;
  loopCount++;
  unsigned long nowHb = millis();
  if (nowHb - lastHeartbeatAt >= 1000) {
    lastHeartbeatAt = nowHb;
    Serial.print("heartbeat ms=");
    Serial.print(nowHb);
    Serial.print(" tab=");
    Serial.print(currentTab);
    Serial.print(" loop=");
    Serial.println(loopCount);
  }

  pollTouchPads();
  serviceTftRecovery();  // action pad held 3 s: re-init the panel, repaint all
  serviceTabRedraw();    // repaint from cache the moment a pad moved the tab
  serviceBridgeDraws();  // Python's pushes, drawn here instead of on its thread
  serviceStatsUpdate();  // value-only refresh of the Today tab
  servicePomoUpdate();   // digits + bar refresh of the Focus tab
  updateEyes();          // Buddy tab: spring/saccade/blink/breathing engine
  serviceLedsAndBuzzer(); // posture/engagement LEDs + non-blocking beep timer
}