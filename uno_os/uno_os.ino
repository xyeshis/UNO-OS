/*
  UNO OS v0.1  для Arduino UNO
  =================================================================
  Однопользовательская "операционная система" в 32 КБ flash и 2 КБ RAM.
  Без внешних компонентов — только плата и USB.

  Что внутри:
    - shell с 22 командами и редактируемой строкой (backspace, Ctrl+C);
    - файловая система в EEPROM: 1 КБ, 8 файлов по 116 байт данных;
    - линейный редактор `write` (точка `.` сохраняет, `:q` отменяет);
    - встроенный mini-Python интерпретатор:
        * целочисленные переменные a..z (int16);
        * операторы + - * / %, == != < > <= >=, && || !;
        * блоки if / while с отступами (как в настоящем Python);
        * print(...), delay(ms), pinm/pinw/pinr, aread, led, ms();
        * комментарии # ...
    - autoexec файла "boot" при старте;
    - программный reboot через WDT.

  Best-friend клиент — host/unoctl.py (Python). Сам находит UNO по
  VID/PID, открывает Serial, поддерживает upload/download файлов
  через меню Ctrl+]. Запуск: `python unoctl.py`.

  Serial: 115200 бод, 8N1, без эха со стороны терминала. CRLF — ok.
*/

#include <Arduino.h>
#include <EEPROM.h>
#include <avr/wdt.h>

// === Конфиг =================================================================

#define VERSION    "0.1"
#define BAUD       115200

#define MAX_FILES  8
#define SLOT_SIZE  128
#define NAME_LEN   8
#define DATA_LEN   116          // SLOT_SIZE - 12 (заголовок)
#define LINE_LEN   80

#define MAGIC_USED 0xA5
#define MAGIC_FREE 0xFF

#define PY_STACK   6            // глубина if/while

// === Глобальные буферы ======================================================

static char     line[LINE_LEN + 1];

static char     py_src[DATA_LEN + 4];
static uint16_t py_len = 0;
static uint16_t py_pos = 0;
static int16_t  py_var[26];
static bool     py_err = false;
static const __FlashStringHelper* py_errMsg = nullptr;

struct PyFrame { uint16_t pos; uint8_t indent; uint8_t kind; };
static PyFrame py_stack[PY_STACK];
static uint8_t py_sp = 0;
#define PY_KIND_IF    1
#define PY_KIND_WHILE 2

// === Утилиты ================================================================

static int waitChar() {
  while (!Serial.available()) ;
  return Serial.read();
}

static int freeRam() {
  extern int __heap_start, *__brkval;
  int v;
  return (int)&v - (__brkval == 0 ? (int)&__heap_start : (int)__brkval);
}

static int parseInt(const char* s) {
  int v = 0; bool neg = false;
  if (*s == '-') { neg = true; s++; }
  while (*s >= '0' && *s <= '9') { v = v * 10 + (*s - '0'); s++; }
  return neg ? -v : v;
}

static char* trim(char* s) {
  while (*s == ' ' || *s == '\t') s++;
  uint8_t n = strlen(s);
  while (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r')) {
    s[--n] = 0;
  }
  return s;
}

static char* splitArg(char* s) {
  while (*s && *s != ' ' && *s != '\t') s++;
  if (!*s) return s;
  *s++ = 0;
  while (*s == ' ' || *s == '\t') s++;
  return s;
}

// === Мини-игра: Minesweeper =================================================

#define MS_W 8
#define MS_H 8
#define MS_N (MS_W * MS_H)
#define MS_MINES 10

static uint8_t msMine[MS_N];
static uint8_t msOpen[MS_N];
static uint8_t msFlag[MS_N];

static uint8_t msCurX = 0;
static uint8_t msCurY = 0;
static bool msGameOver = false;
static bool msWin = false;

static uint8_t msIdx(uint8_t x, uint8_t y) { return y * MS_W + x; }

static int8_t msAdj(uint8_t x, uint8_t y) {
  int8_t c = 0;
  for (int8_t dy = -1; dy <= 1; dy++) {
    for (int8_t dx = -1; dx <= 1; dx++) {
      if (dx == 0 && dy == 0) continue;
      int8_t nx = (int8_t)x + dx;
      int8_t ny = (int8_t)y + dy;
      if (nx < 0 || ny < 0 || nx >= MS_W || ny >= MS_H) continue;
      if (msMine[msIdx((uint8_t)nx, (uint8_t)ny)]) c++;
    }
  }
  return c;
}

static void msRender() {
  Serial.print(F("\x1b[2J\x1b[H"));
  Serial.println(F("Minesweeper 8x8  mines=10"));
  Serial.println(F("WASD move  O open  F flag  R restart  Q quit"));
  Serial.println();
  Serial.print(F("   "));
  for (uint8_t x = 0; x < MS_W; x++) {
    Serial.print((char)('0' + x));
    Serial.write(' ');
  }
  Serial.println();
  for (uint8_t y = 0; y < MS_H; y++) {
    Serial.print((char)('0' + y));
    Serial.print(F(": "));
    for (uint8_t x = 0; x < MS_W; x++) {
      uint8_t i = msIdx(x, y);
      char ch = '.';
      if (msOpen[i]) {
        if (msMine[i]) ch = '*';
        else {
          int8_t a = msAdj(x, y);
          ch = (a == 0) ? ' ' : (char)('0' + a);
        }
      } else if (msFlag[i]) {
        ch = 'F';
      }
      if (x == msCurX && y == msCurY) {
        Serial.write('['); Serial.write(ch); Serial.write(']');
      } else {
        Serial.write(' '); Serial.write(ch); Serial.write(' ');
      }
    }
    Serial.println();
  }
  Serial.println();
  if (msGameOver) {
    Serial.println(F("BOOM. You lost. Press R to restart or Q to quit."));
  } else if (msWin) {
    Serial.println(F("You win! Press R to play again or Q to quit."));
  } else {
    Serial.println(F("Open all safe cells."));
  }
}

static void msRevealZeros(uint8_t sx, uint8_t sy) {
  uint8_t st[MS_N];
  uint8_t sp = 0;
  st[sp++] = msIdx(sx, sy);
  while (sp > 0) {
    uint8_t cur = st[--sp];
    uint8_t x = cur % MS_W;
    uint8_t y = cur / MS_W;
    if (msAdj(x, y) != 0) continue;
    for (int8_t dy = -1; dy <= 1; dy++) {
      for (int8_t dx = -1; dx <= 1; dx++) {
        int8_t nx = (int8_t)x + dx;
        int8_t ny = (int8_t)y + dy;
        if (nx < 0 || ny < 0 || nx >= MS_W || ny >= MS_H) continue;
        uint8_t ni = msIdx((uint8_t)nx, (uint8_t)ny);
        if (msOpen[ni] || msFlag[ni]) continue;
        msOpen[ni] = 1;
        if (!msMine[ni] && msAdj((uint8_t)nx, (uint8_t)ny) == 0 && sp < MS_N) {
          st[sp++] = ni;
        }
      }
    }
  }
}

static void msCheckWin() {
  uint8_t openedSafe = 0;
  for (uint8_t i = 0; i < MS_N; i++) {
    if (!msMine[i] && msOpen[i]) openedSafe++;
  }
  if (openedSafe >= (MS_N - MS_MINES)) {
    msWin = true;
    msGameOver = true;
    for (uint8_t i = 0; i < MS_N; i++) {
      if (msMine[i]) msFlag[i] = 1;
    }
  }
}

static void msOpenCell(uint8_t x, uint8_t y) {
  uint8_t i = msIdx(x, y);
  if (msOpen[i] || msFlag[i] || msGameOver) return;
  msOpen[i] = 1;
  if (msMine[i]) {
    msGameOver = true;
    msWin = false;
    for (uint8_t k = 0; k < MS_N; k++) if (msMine[k]) msOpen[k] = 1;
    return;
  }
  if (msAdj(x, y) == 0) msRevealZeros(x, y);
  msCheckWin();
}

static void msToggleFlag(uint8_t x, uint8_t y) {
  uint8_t i = msIdx(x, y);
  if (msOpen[i] || msGameOver) return;
  msFlag[i] = msFlag[i] ? 0 : 1;
}

static void msNewGame() {
  for (uint8_t i = 0; i < MS_N; i++) {
    msMine[i] = 0;
    msOpen[i] = 0;
    msFlag[i] = 0;
  }
  msCurX = 0;
  msCurY = 0;
  msGameOver = false;
  msWin = false;

  uint8_t placed = 0;
  while (placed < MS_MINES) {
    uint8_t x = (uint8_t)random(MS_W);
    uint8_t y = (uint8_t)random(MS_H);
    uint8_t i = msIdx(x, y);
    if (msMine[i]) continue;
    msMine[i] = 1;
    placed++;
  }
}

static void cmdMine() {
  randomSeed(millis() ^ (uint32_t)analogRead(A0) ^ (uint32_t)freeRam());
  msNewGame();
  msRender();
  while (true) {
    int c = waitChar();
    if (c >= 'A' && c <= 'Z') c += 32;
    if (c == 'q') {
      Serial.println(F("bye minesweeper."));
      return;
    } else if (c == 'r') {
      msNewGame();
    } else if (c == 'w' && msCurY > 0) {
      msCurY--;
    } else if (c == 's' && msCurY + 1 < MS_H) {
      msCurY++;
    } else if (c == 'a' && msCurX > 0) {
      msCurX--;
    } else if (c == 'd' && msCurX + 1 < MS_W) {
      msCurX++;
    } else if (c == 'o' || c == ' ') {
      msOpenCell(msCurX, msCurY);
    } else if (c == 'f') {
      msToggleFlag(msCurX, msCurY);
    }
    msRender();
  }
}

// === EEPROM file system =====================================================
//
// 8 слотов по 128 байт. Заголовок:
//   [0]      magic 0xA5 (used) / 0xFF (free)
//   [1..8]   имя (8 b, NUL-padded)
//   [9..10]  size (uint16 LE)
//   [11]     reserved
//   [12..127] data (116 b)

static uint16_t slotAddr(uint8_t s) { return (uint16_t)s * SLOT_SIZE; }
static bool     slotUsed(uint8_t s) { return EEPROM.read(slotAddr(s)) == MAGIC_USED; }

static uint16_t slotSize(uint8_t s) {
  uint16_t a = slotAddr(s);
  return EEPROM.read(a + 9) | ((uint16_t)EEPROM.read(a + 10) << 8);
}

static void slotSetSize(uint8_t s, uint16_t sz) {
  uint16_t a = slotAddr(s);
  EEPROM.update(a + 9,  sz & 0xFF);
  EEPROM.update(a + 10, (sz >> 8) & 0xFF);
}

static void slotName(uint8_t s, char* out) {
  uint16_t a = slotAddr(s) + 1;
  uint8_t i;
  for (i = 0; i < NAME_LEN; i++) {
    char c = (char)EEPROM.read(a + i);
    if (c == 0) break;
    out[i] = c;
  }
  out[i] = 0;
}

static void slotSetName(uint8_t s, const char* nm) {
  uint16_t a = slotAddr(s) + 1;
  bool done = false;
  for (uint8_t i = 0; i < NAME_LEN; i++) {
    uint8_t c = 0;
    if (!done) {
      c = (uint8_t)nm[i];
      if (c == 0) done = true;
    }
    EEPROM.update(a + i, c);
  }
}

static int8_t fsFind(const char* nm) {
  for (uint8_t s = 0; s < MAX_FILES; s++) {
    if (!slotUsed(s)) continue;
    char buf[NAME_LEN + 1];
    slotName(s, buf);
    if (strcmp(buf, nm) == 0) return (int8_t)s;
  }
  return -1;
}

static int8_t fsFreeSlot() {
  for (uint8_t s = 0; s < MAX_FILES; s++) {
    if (!slotUsed(s)) return (int8_t)s;
  }
  return -1;
}

static void fsCreate(uint8_t s, const char* nm) {
  EEPROM.update(slotAddr(s), MAGIC_USED);
  slotSetName(s, nm);
  slotSetSize(s, 0);
}

static void fsRm(uint8_t s) {
  EEPROM.update(slotAddr(s), MAGIC_FREE);
}

static uint8_t fsGet(uint8_t s, uint16_t off) {
  return EEPROM.read(slotAddr(s) + 12 + off);
}

static void fsPut(uint8_t s, uint16_t off, uint8_t v) {
  EEPROM.update(slotAddr(s) + 12 + off, v);
}

static uint16_t fsUsed() {
  uint16_t t = 0;
  for (uint8_t s = 0; s < MAX_FILES; s++) {
    if (slotUsed(s)) t += slotSize(s);
  }
  return t;
}

static uint8_t fsCount() {
  uint8_t n = 0;
  for (uint8_t s = 0; s < MAX_FILES; s++) if (slotUsed(s)) n++;
  return n;
}

static void fsFormat() {
  for (uint8_t s = 0; s < MAX_FILES; s++) {
    EEPROM.update(slotAddr(s), MAGIC_FREE);
  }
}

static bool nameValid(const char* n) {
  if (!n || !*n) return false;
  uint8_t L = 0;
  for (const char* p = n; *p; p++) {
    if (++L > NAME_LEN) return false;
    char c = *p;
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) {
      return false;
    }
  }
  return true;
}

// === Shell I/O ==============================================================

static void prompt() { Serial.print(F("uno> ")); }

static void readLine() {
  uint8_t n = 0;
  while (true) {
    int c = waitChar();
    if (c == '\r') continue;
    if (c == '\n') { Serial.println(); line[n] = 0; return; }
    if (c == 8 || c == 127) {
      if (n > 0) { n--; Serial.print(F("\b \b")); }
      continue;
    }
    if (c == 3) {                      // Ctrl+C
      Serial.println(F("^C"));
      line[0] = 0;
      return;
    }
    if (c >= 32 && c < 127 && n < LINE_LEN) {
      line[n++] = (char)c;
      Serial.write((char)c);
    }
  }
}

// === mini-Python interpreter ================================================

static char pyPeek() { return py_pos < py_len ? py_src[py_pos] : 0; }

static void pyError(const __FlashStringHelper* m) {
  if (!py_err) { py_err = true; py_errMsg = m; }
}

static void pySkipSp() {
  while (py_pos < py_len) {
    char c = py_src[py_pos];
    if (c == ' ' || c == '\t') py_pos++; else break;
  }
}

static void pyToEol() {
  while (py_pos < py_len && py_src[py_pos] != '\n') py_pos++;
  if (py_pos < py_len) py_pos++;
}

static bool pyMatchCh(char c) {
  pySkipSp();
  if (pyPeek() == c) { py_pos++; return true; }
  return false;
}

// keyword followed by non-identifier char
static bool pyTryKw(const char* kw) {
  pySkipSp();
  uint16_t save = py_pos;
  while (*kw) {
    if (py_pos >= py_len || py_src[py_pos] != *kw) { py_pos = save; return false; }
    py_pos++; kw++;
  }
  char nx = pyPeek();
  if ((nx >= 'a' && nx <= 'z') || (nx >= 'A' && nx <= 'Z') ||
      (nx >= '0' && nx <= '9') || nx == '_') {
    py_pos = save; return false;
  }
  return true;
}

static int16_t pyExpr();    // forward

static int16_t pyPrim() {
  pySkipSp();
  char c = pyPeek();
  if (c == '(') {
    py_pos++;
    int16_t v = pyExpr();
    if (!pyMatchCh(')')) pyError(F("?)"));
    return v;
  }
  if (c == '-') { py_pos++; return -pyPrim(); }
  if (c == '+') { py_pos++; return  pyPrim(); }
  if (c == '!') { py_pos++; return pyPrim() == 0 ? 1 : 0; }
  if (c >= '0' && c <= '9') {
    int16_t v = 0;
    while (py_pos < py_len) {
      char d = py_src[py_pos];
      if (d < '0' || d > '9') break;
      v = v * 10 + (d - '0'); py_pos++;
    }
    return v;
  }
  if (pyTryKw("pinr")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return 0; }
    int16_t p = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return 0; }
    return digitalRead((uint8_t)p);
  }
  if (pyTryKw("aread")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return 0; }
    int16_t p = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return 0; }
    return analogRead((uint8_t)p);
  }
  if (pyTryKw("ms")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return 0; }
    if (!pyMatchCh(')')) { pyError(F("?)")); return 0; }
    return (int16_t)millis();
  }
  if (c >= 'a' && c <= 'z') {
    char nx = (py_pos + 1 < py_len) ? py_src[py_pos + 1] : 0;
    if (!((nx >= 'a' && nx <= 'z') || (nx >= '0' && nx <= '9') || nx == '_')) {
      py_pos++;
      return py_var[c - 'a'];
    }
    pyError(F("?id")); return 0;
  }
  pyError(F("?expr"));
  return 0;
}

static int16_t pyMul() {
  int16_t a = pyPrim();
  while (!py_err) {
    pySkipSp();
    char c = pyPeek();
    if (c == '*') { py_pos++; a *= pyPrim(); }
    else if (c == '/') { py_pos++; int16_t b = pyPrim(); if (b == 0) { pyError(F("?div0")); return 0; } a /= b; }
    else if (c == '%') { py_pos++; int16_t b = pyPrim(); if (b == 0) { pyError(F("?div0")); return 0; } a %= b; }
    else break;
  }
  return a;
}

static int16_t pyAdd() {
  int16_t a = pyMul();
  while (!py_err) {
    pySkipSp();
    char c = pyPeek();
    if (c == '+')      { py_pos++; a += pyMul(); }
    else if (c == '-') { py_pos++; a -= pyMul(); }
    else break;
  }
  return a;
}

static int16_t pyCmp() {
  int16_t a = pyAdd();
  pySkipSp();
  char c = pyPeek();
  char d = (py_pos + 1 < py_len) ? py_src[py_pos + 1] : 0;
  if (c == '=' && d == '=') { py_pos += 2; return a == pyAdd() ? 1 : 0; }
  if (c == '!' && d == '=') { py_pos += 2; return a != pyAdd() ? 1 : 0; }
  if (c == '<' && d == '=') { py_pos += 2; return a <= pyAdd() ? 1 : 0; }
  if (c == '>' && d == '=') { py_pos += 2; return a >= pyAdd() ? 1 : 0; }
  if (c == '<')             { py_pos += 1; return a <  pyAdd() ? 1 : 0; }
  if (c == '>')             { py_pos += 1; return a >  pyAdd() ? 1 : 0; }
  return a;
}

static int16_t pyAndE() {
  int16_t a = pyCmp();
  while (!py_err) {
    pySkipSp();
    if (py_pos + 1 < py_len && py_src[py_pos] == '&' && py_src[py_pos + 1] == '&') {
      py_pos += 2;
      int16_t b = pyCmp();
      a = (a && b) ? 1 : 0;
    } else break;
  }
  return a;
}

static int16_t pyExpr() {
  int16_t a = pyAndE();
  while (!py_err) {
    pySkipSp();
    if (py_pos + 1 < py_len && py_src[py_pos] == '|' && py_src[py_pos + 1] == '|') {
      py_pos += 2;
      int16_t b = pyAndE();
      a = (a || b) ? 1 : 0;
    } else break;
  }
  return a;
}

static void pySkipBlock(uint8_t headerIndent) {
  pyToEol();                         // пропустили заголовок (`if ...:` / `while ...:`)
  while (py_pos < py_len) {
    uint16_t ls = py_pos;
    uint8_t ind = 0;
    while (py_pos < py_len && py_src[py_pos] == ' ') { ind++; py_pos++; }
    if (py_pos >= py_len) break;
    char ch = py_src[py_pos];
    if (ch == '\n' || ch == '#') { pyToEol(); continue; }
    if (ind <= headerIndent) {       // строка снаружи нашего блока
      py_pos = ls;                   // отмотаем до начала, главный цикл прочитает её снова
      return;
    }
    pyToEol();
  }
}

static void pyExecStmt(uint8_t indent, uint16_t lineStart) {
  if (pyTryKw("if")) {
    int16_t v = pyExpr();
    if (!pyMatchCh(':')) { pyError(F("?:")); return; }
    if (v) {
      if (py_sp >= PY_STACK) { pyError(F("?stk")); return; }
      py_stack[py_sp].pos = lineStart;
      py_stack[py_sp].indent = indent;
      py_stack[py_sp].kind = PY_KIND_IF;
      py_sp++;
      pyToEol();
    } else {
      pySkipBlock(indent);
    }
    return;
  }
  if (pyTryKw("while")) {
    int16_t v = pyExpr();
    if (!pyMatchCh(':')) { pyError(F("?:")); return; }
    if (v) {
      if (py_sp >= PY_STACK) { pyError(F("?stk")); return; }
      py_stack[py_sp].pos = lineStart;
      py_stack[py_sp].indent = indent;
      py_stack[py_sp].kind = PY_KIND_WHILE;
      py_sp++;
      pyToEol();
    } else {
      pySkipBlock(indent);
    }
    return;
  }
  if (pyTryKw("print")) {
    if (!pyMatchCh('(')) {
      Serial.println();
      pyToEol();
      return;
    }
    pySkipSp();
    if (pyPeek() == ')') {
      py_pos++;
      Serial.println();
    } else if (pyPeek() == '"') {
      py_pos++;
      while (py_pos < py_len && py_src[py_pos] != '"' && py_src[py_pos] != '\n') {
        Serial.write(py_src[py_pos++]);
      }
      if (pyPeek() == '"') py_pos++;
      if (!pyMatchCh(')')) pyError(F("?)"));
      Serial.println();
    } else {
      int16_t v = pyExpr();
      if (!pyMatchCh(')')) pyError(F("?)"));
      Serial.println(v);
    }
    pyToEol();
    return;
  }
  if (pyTryKw("delay")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return; }
    int16_t v = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return; }
    if (v < 0) v = 0;
    delay((uint16_t)v);
    pyToEol();
    return;
  }
  if (pyTryKw("pinm")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return; }
    int16_t p = pyExpr();
    if (!pyMatchCh(',')) { pyError(F("?,")); return; }
    int16_t m = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return; }
    pinMode((uint8_t)p, (uint8_t)m);
    pyToEol();
    return;
  }
  if (pyTryKw("pinw")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return; }
    int16_t p = pyExpr();
    if (!pyMatchCh(',')) { pyError(F("?,")); return; }
    int16_t v = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return; }
    digitalWrite((uint8_t)p, v ? HIGH : LOW);
    pyToEol();
    return;
  }
  if (pyTryKw("led")) {
    if (!pyMatchCh('(')) { pyError(F("?(")); return; }
    int16_t v = pyExpr();
    if (!pyMatchCh(')')) { pyError(F("?)")); return; }
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, v ? HIGH : LOW);
    pyToEol();
    return;
  }
  // assignment: <letter> = expr
  pySkipSp();
  char nm = pyPeek();
  if (nm >= 'a' && nm <= 'z') {
    char nx = (py_pos + 1 < py_len) ? py_src[py_pos + 1] : 0;
    if (!((nx >= 'a' && nx <= 'z') || (nx >= '0' && nx <= '9') || nx == '_')) {
      uint16_t save = py_pos;
      py_pos++;
      pySkipSp();
      if (py_pos < py_len && py_src[py_pos] == '=' &&
          (py_pos + 1 >= py_len || py_src[py_pos + 1] != '=')) {
        py_pos++;
        int16_t v = pyExpr();
        py_var[nm - 'a'] = v;
        pyToEol();
        return;
      }
      py_pos = save;
    }
  }
  pyError(F("?stmt"));
}

static void runScript(uint8_t slot) {
  py_len = slotSize(slot);
  if (py_len > sizeof(py_src) - 2) py_len = sizeof(py_src) - 2;
  for (uint16_t i = 0; i < py_len; i++) py_src[i] = (char)fsGet(slot, i);
  if (py_len == 0 || py_src[py_len - 1] != '\n') {
    py_src[py_len++] = '\n';
  }
  py_src[py_len] = 0;

  py_pos = 0;
  py_sp = 0;
  py_err = false;
  py_errMsg = nullptr;
  for (uint8_t i = 0; i < 26; i++) py_var[i] = 0;

  while (py_pos < py_len && !py_err) {
    uint16_t ls = py_pos;
    uint8_t ind = 0;
    while (py_pos < py_len && py_src[py_pos] == ' ') { ind++; py_pos++; }
    if (py_pos >= py_len) break;
    char c = py_src[py_pos];
    if (c == '\n') { py_pos++; continue; }
    if (c == '#')  { pyToEol(); continue; }

    bool jumped = false;
    while (py_sp > 0 && ind <= py_stack[py_sp - 1].indent) {
      if (py_stack[py_sp - 1].kind == PY_KIND_WHILE) {
        py_pos = py_stack[py_sp - 1].pos;
        py_sp--;
        jumped = true;
        break;
      }
      py_sp--;
    }
    if (jumped) continue;

    pyExecStmt(ind, ls);
  }

  if (py_err) {
    Serial.print(F("py: "));
    if (py_errMsg) Serial.println(py_errMsg);
    else            Serial.println(F("?"));
  }
}

// === Команды shell ==========================================================

static void cmdHelp() {
  Serial.println(F(
    "help  ls  cat  write  rm  mv  cp  df  mem  echo\r\n"
    "run  py  format  clear  reboot  ver  uptime\r\n"
    "pinm  pinw  pinr  aread  led  mine"
  ));
}

static void cmdLs() {
  Serial.print(F("files: ")); Serial.print(fsCount());
  Serial.print('/');           Serial.println(MAX_FILES);
  for (uint8_t s = 0; s < MAX_FILES; s++) {
    if (!slotUsed(s)) continue;
    char nm[NAME_LEN + 1];
    slotName(s, nm);
    Serial.print(F("  "));
    Serial.print(nm);
    uint8_t pad = NAME_LEN + 2 - strlen(nm);
    while (pad--) Serial.write(' ');
    Serial.println(slotSize(s));
  }
}

static void cmdCat(const char* nm) {
  if (!*nm) { Serial.println(F("? cat <name>")); return; }
  int8_t s = fsFind(nm);
  if (s < 0) { Serial.println(F("? not found")); return; }
  uint16_t sz = slotSize((uint8_t)s);
  for (uint16_t i = 0; i < sz; i++) Serial.write((char)fsGet((uint8_t)s, i));
  if (sz == 0 || fsGet((uint8_t)s, sz - 1) != '\n') Serial.println();
}

static void cmdWrite(const char* nm) {
  if (!nameValid(nm)) { Serial.println(F("? bad name")); return; }
  int8_t s = fsFind(nm);
  if (s < 0) {
    s = fsFreeSlot();
    if (s < 0) { Serial.println(F("? no slot")); return; }
    fsCreate((uint8_t)s, nm);
  } else {
    slotSetSize((uint8_t)s, 0);
  }
  Serial.println(F("end with '.'  (':q' to abort)"));
  uint16_t off = 0;
  bool aborted = false;
  while (true) {
    Serial.print(F("... > "));
    readLine();
    if (line[0] == '.' && line[1] == 0) break;
    if (line[0] == ':' && line[1] == 'q' && line[2] == 0) { aborted = true; break; }
    uint16_t L = (uint16_t)strlen(line);
    if (off + L + 1 > DATA_LEN) {
      Serial.println(F("! file full — truncating"));
      L = (off >= DATA_LEN - 1) ? 0 : (DATA_LEN - 1 - off);
    }
    for (uint16_t i = 0; i < L; i++) fsPut((uint8_t)s, off + i, (uint8_t)line[i]);
    off += L;
    if (off < DATA_LEN) {
      fsPut((uint8_t)s, off, (uint8_t)'\n');
      off++;
    }
  }
  if (aborted) {
    fsRm((uint8_t)s);
    Serial.println(F("aborted"));
  } else {
    slotSetSize((uint8_t)s, off);
    Serial.print(F("wrote ")); Serial.print(off); Serial.println(F(" b"));
  }
}

static void cmdRm(const char* nm) {
  if (!*nm) { Serial.println(F("? rm <name>")); return; }
  int8_t s = fsFind(nm);
  if (s < 0) { Serial.println(F("? not found")); return; }
  fsRm((uint8_t)s);
  Serial.println(F("ok"));
}

static void cmdMv(char* args) {
  char* dst = splitArg(args);
  if (!*args || !*dst)  { Serial.println(F("? mv <old> <new>")); return; }
  if (!nameValid(dst))  { Serial.println(F("? bad name"));      return; }
  if (fsFind(dst) >= 0) { Serial.println(F("? exists"));        return; }
  int8_t s = fsFind(args);
  if (s < 0) { Serial.println(F("? not found")); return; }
  slotSetName((uint8_t)s, dst);
  Serial.println(F("ok"));
}

static void cmdCp(char* args) {
  char* dst = splitArg(args);
  if (!*args || !*dst)  { Serial.println(F("? cp <src> <dst>")); return; }
  if (!nameValid(dst))  { Serial.println(F("? bad name"));      return; }
  if (fsFind(dst) >= 0) { Serial.println(F("? exists"));        return; }
  int8_t src = fsFind(args);
  if (src < 0) { Serial.println(F("? not found")); return; }
  int8_t d = fsFreeSlot();
  if (d < 0) { Serial.println(F("? no slot")); return; }
  uint16_t sz = slotSize((uint8_t)src);
  fsCreate((uint8_t)d, dst);
  for (uint16_t i = 0; i < sz; i++) fsPut((uint8_t)d, i, fsGet((uint8_t)src, i));
  slotSetSize((uint8_t)d, sz);
  Serial.println(F("ok"));
}

static void cmdDf() {
  uint16_t used = fsUsed();
  uint16_t cap  = (uint16_t)MAX_FILES * DATA_LEN;
  Serial.print(F("disk:  ")); Serial.print(used); Serial.print('/');
  Serial.print(cap);          Serial.println(F(" b"));
  Serial.print(F("files: ")); Serial.print(fsCount()); Serial.print('/');
  Serial.println(MAX_FILES);
}

static void cmdMem() {
  Serial.print(F("ram free: ")); Serial.print(freeRam()); Serial.println(F(" b"));
}

static void cmdEcho(const char* s) { Serial.println(s); }

static void cmdRun(const char* nm) {
  if (!*nm) { Serial.println(F("? run <name>")); return; }
  int8_t s = fsFind(nm);
  if (s < 0) { Serial.println(F("? not found")); return; }
  runScript((uint8_t)s);
}

static void cmdFormat() {
  Serial.print(F("format? y/N "));
  readLine();
  if (line[0] == 'y' || line[0] == 'Y') {
    fsFormat();
    Serial.println(F("ok"));
  } else {
    Serial.println(F("aborted"));
  }
}

static void cmdClear()  { Serial.print(F("\x1b[2J\x1b[H")); }

static void cmdReboot() {
  Serial.println(F("rebooting..."));
  Serial.flush();
  wdt_enable(WDTO_15MS);
  while (true) ;
}

static void cmdVer() {
  Serial.print(F("UNO OS v")); Serial.println(F(VERSION));
  Serial.print(F("ATmega328P  flash 32k  ram 2k  ee 1k  baud "));
  Serial.println(BAUD);
}

static void cmdUptime() {
  uint32_t t = millis() / 1000UL;
  uint32_t h = t / 3600;
  uint32_t m = (t / 60) % 60;
  uint32_t s = t % 60;
  Serial.print(F("up: ")); Serial.print(h); Serial.print('h');
  Serial.print(m);         Serial.print('m');
  Serial.print(s);         Serial.println('s');
}

static void cmdPinm(char* args) {
  char* m = splitArg(args);
  if (!*args || !*m) { Serial.println(F("? pinm <pin> <0=in|1=out|2=in_pullup>")); return; }
  pinMode((uint8_t)parseInt(args), (uint8_t)parseInt(m));
  Serial.println(F("ok"));
}

static void cmdPinw(char* args) {
  char* v = splitArg(args);
  if (!*args || !*v) { Serial.println(F("? pinw <pin> <0|1>")); return; }
  digitalWrite((uint8_t)parseInt(args), parseInt(v) ? HIGH : LOW);
  Serial.println(F("ok"));
}

static void cmdPinr(const char* a) {
  if (!*a) { Serial.println(F("? pinr <pin>")); return; }
  Serial.println(digitalRead((uint8_t)parseInt(a)));
}

static void cmdAread(const char* a) {
  if (!*a) { Serial.println(F("? aread <pin>")); return; }
  Serial.println(analogRead((uint8_t)parseInt(a)));
}

static void cmdLed(const char* a) {
  pinMode(LED_BUILTIN, OUTPUT);
  bool on = (strcmp(a, "on") == 0) || (a[0] == '1');
  digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
  Serial.print(F("led ")); Serial.println(on ? F("on") : F("off"));
}

// === Dispatcher =============================================================

static void exec(char* l) {
  l = trim(l);
  if (!*l) return;
  char* args = splitArg(l);
  if      (!strcmp(l, "help"))   cmdHelp();
  else if (!strcmp(l, "ls"))     cmdLs();
  else if (!strcmp(l, "cat"))    cmdCat(args);
  else if (!strcmp(l, "write"))  cmdWrite(args);
  else if (!strcmp(l, "rm"))     cmdRm(args);
  else if (!strcmp(l, "mv"))     cmdMv(args);
  else if (!strcmp(l, "cp"))     cmdCp(args);
  else if (!strcmp(l, "df"))     cmdDf();
  else if (!strcmp(l, "mem"))    cmdMem();
  else if (!strcmp(l, "echo"))   cmdEcho(args);
  else if (!strcmp(l, "run"))    cmdRun(args);
  else if (!strcmp(l, "py"))     cmdRun(args);
  else if (!strcmp(l, "format")) cmdFormat();
  else if (!strcmp(l, "clear"))  cmdClear();
  else if (!strcmp(l, "cls"))    cmdClear();
  else if (!strcmp(l, "reboot")) cmdReboot();
  else if (!strcmp(l, "ver"))    cmdVer();
  else if (!strcmp(l, "uptime")) cmdUptime();
  else if (!strcmp(l, "pinm"))   cmdPinm(args);
  else if (!strcmp(l, "pinw"))   cmdPinw(args);
  else if (!strcmp(l, "pinr"))   cmdPinr(args);
  else if (!strcmp(l, "aread"))  cmdAread(args);
  else if (!strcmp(l, "led"))    cmdLed(args);
  else if (!strcmp(l, "mine"))   cmdMine();
  else if (!strcmp(l, "mines"))  cmdMine();
  else if (!strcmp(l, "ms"))     cmdMine();
  else { Serial.print(F("? ")); Serial.println(l); }
}

// === setup / loop ===========================================================

void setup() {
  MCUSR = 0;
  wdt_disable();

  Serial.begin(BAUD);
  while (!Serial) ;
  delay(80);

  Serial.print(F("\x1b[2J\x1b[H"));
  Serial.print(F("UNO OS v")); Serial.print(F(VERSION));
  Serial.println(F("  -  type 'help'"));
  Serial.print(F("ram free: "));  Serial.print(freeRam());
  Serial.print(F(" b   files: ")); Serial.print(fsCount());
  Serial.print('/');               Serial.println(MAX_FILES);

  int8_t b = fsFind("boot");
  if (b >= 0) {
    Serial.println(F("[boot]"));
    runScript((uint8_t)b);
  }
  prompt();
}

void loop() {
  if (!Serial.available()) return;
  readLine();
  exec(line);
  prompt();
}
