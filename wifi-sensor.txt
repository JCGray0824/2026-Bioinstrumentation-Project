/*
 * ============================================================================
 * KX134 Wireless Sensor Readout Test
 * Bioinstrumentation | BIOM 8209 | University of Memphis | Fall 2026
 * Revision: 2026-11-14
 * ============================================================================
 * Purpose
 *   Test accelerometer communication from an Arduino UNO R4 WiFi to a laptop
 *   over a direct Wi-Fi connection. The Arduino creates its own access point
 *   and streams readings to one TCP client. USB serial mirrors the readings
 *   and provides startup diagnostics when a USB cable is connected.
 *
 * Hardware and libraries
 *   - Arduino UNO R4 WiFi, powered by USB or the working standalone supply.
 *   - SparkFun KX134 connected to the onboard Qwiic connector (Wire1).
 *   - WiFiS3 from the Arduino UNO R4 board package.
 *   - SparkFun KX13X Arduino Library, version 2.0.4.
 *
 * Setup and use
 *   1. Paste this code into an Arduino sketch. Keep an arduino_secrets.h tab
 *      beside it defining SECRET_SSID and SECRET_PASS for your access point.
 *      Use a password of at least eight characters.
 *   2. Select Arduino UNO R4 WiFi and upload. Optional USB Serial Monitor:
 *      115200 baud. Startup continues without a Serial Monitor connected.
 *   3. Join the Arduino's Wi-Fi network from the laptop.
 *   4. In macOS Terminal, run: nc 192.48.56.2 5000
 *      Press Control-C to disconnect; run the command again to reconnect.
 *
 * Output and scope
 *   CSV columns: read_ms,x_g,y_g,z_g. Acceleration values are in g.
 *   read_ms is board uptime at the sensor read, not laptop arrival time.
 *   Sensor configuration: 50 Hz output data rate and +/-64 g on startup.
 *   The GUI can change range between recordings. The TCP client may send
 *   RANGE 8, RANGE 16, RANGE 32, or RANGE 64 followed by a newline.
 *   "# RANGE <g>" announces the active range on connection and after each
 *   successful change, before readings at that range. Errors start with
 *   "# RANGE ERROR". CSV sample lines are unchanged.
 *   This is a readout test: polling and network delays can reduce the delivered
 *   sample rate. Recording and file saving are handled on the laptop.
 *   Arduino IDE's Serial Monitor itself uses USB;
 *   the nc session is the wireless text monitor. No HTTP webpage is served.
 *
 * LED status
 *   Off: waiting for a TCP client. On: client connected. Blinking: startup fault.
 *
 * Adapted from Arduino's AP_SimpleWebServer example (Tom Igoe / Adafruit) and
 * SparkFun's example1-BasicReadings (Elias Santistevan, October 2022; MIT).
 * References:
 *   https://docs.arduino.cc/tutorials/uno-r4-wifi/wifi-examples/
 *   https://github.com/sparkfun/SparkFun_KX13X_Arduino_Library
 * ============================================================================
 */

#include <Wire.h>
#include <WiFiS3.h>
#include <SparkFun_KX13X.h>
#include "arduino_secrets.h"

// Keep the address from the access-point sketch that already worked.
const IPAddress AP_ADDRESS(192, 48, 56, 2);
const uint16_t TCP_PORT = 5000;
const unsigned long SERIAL_BAUD = 115200;
const unsigned long SERIAL_WAIT_MS = 1500;
const unsigned long CLIENT_CHECK_MS = 250;
const unsigned long SENSOR_WARNING_MS = 2000;
const uint8_t SENSOR_ODR_50_HZ = 0x06; // KX134 ODR register setting for 50 Hz.

struct RangeSetting
{
  const char *command;
  uint8_t fullScaleG;
  uint8_t registerValue;
};

const RangeSetting SENSOR_RANGES[] = {
    {"RANGE 8", 8, SFE_KX134_RANGE8G},
    {"RANGE 16", 16, SFE_KX134_RANGE16G},
    {"RANGE 32", 32, SFE_KX134_RANGE32G},
    {"RANGE 64", 64, SFE_KX134_RANGE64G},
};
const RangeSetting *activeRange = &SENSOR_RANGES[3]; // Boot at +/-64 g.

char ssid[] = SECRET_SSID;
char pass[] = SECRET_PASS;

SparkFun_KX134 kxAccel;
outputData sensorData;
WiFiServer sensorServer(TCP_PORT);
WiFiClient sensorClient;

// Reuse the line buffer and send one complete line per Wi-Fi write. Individual
// print() calls to WiFiClient would each incur a separate modem transaction.
String sampleLine;
unsigned long lastClientCheckMs = 0;
unsigned long lastGoodReadingMs = 0;
unsigned long lastSensorWarningMs = 0;

char commandLine[32];
size_t commandLength = 0;
bool discardCommand = false;

// Stop on a startup fault and provide an indicator even without USB connected.
void haltWithError(const char *message)
{
  Serial.print("# ERROR: ");
  Serial.println(message);
  while (true)
  {
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
    delay(250);
  }
}

bool verifyRange(const RangeSetting &range)
{
  sfe_kx13x_cntl1_bitfield_t control;
  return kxAccel.readRegisterRegion(SFE_KX13X_CNTL1, &control.all, 1) == 0 &&
         control.bits.pc1 && control.bits.gsel == range.registerValue;
}

// Configure the sensor before starting the wireless service.
void initializeSensor()
{
  Wire1.begin(); // The UNO R4 WiFi's Qwiic connector is on Wire1, not Wire.

  if (!kxAccel.begin(Wire1))
    haltWithError("KX134 not detected. Check Qwiic cable and sensor address.");

  if (!kxAccel.softwareReset())
    haltWithError("KX134 software reset failed.");
  delay(5); // Allow the sensor to finish resetting before configuration.

  // Range and output-rate settings are applied with the sensor in standby.
  if (!kxAccel.enableAccel(false) ||
      !kxAccel.setRange(activeRange->registerValue) ||
      !kxAccel.setOutputDataRate(SENSOR_ODR_50_HZ) ||
      !kxAccel.enableDataEngine() ||
      !kxAccel.enableAccel() ||
      !verifyRange(*activeRange))
    haltWithError("KX134 configuration failed.");

  Serial.println("# KX134 ready: +/-64 g, configured ODR 50 Hz.");
}

void startAccessPoint()
{
  if (WiFi.status() == WL_NO_MODULE)
    haltWithError("Cannot communicate with the Wi-Fi module.");

  Serial.print("# Wi-Fi firmware: ");
  Serial.println(WiFi.firmwareVersion());
  WiFi.config(AP_ADDRESS);

  // Create a password-protected network; an existing router is not needed.
  if (WiFi.beginAP(ssid, pass) != WL_AP_LISTENING)
    haltWithError("Access point could not start. Check SSID and password.");

  sensorServer.begin();
  if (!sensorServer)
    haltWithError("TCP server could not start.");

  Serial.print("# Join Wi-Fi network: ");
  Serial.println(ssid);
  Serial.print("# Laptop Terminal command: nc ");
  Serial.print(WiFi.localIP());
  Serial.print(" ");
  Serial.println(TCP_PORT);
  Serial.println("# Waiting for one TCP client. Press Control-C to disconnect.");
}

// A range change requires standby. Read and discard the output registers
// after restarting to clear any old data-ready flag before the next sample.
bool applySensorRange(const RangeSetting &range)
{
  rawOutputData discardedSample;
  return kxAccel.enableAccel(false) &&
         kxAccel.setRange(range.registerValue) &&
         kxAccel.enableAccel() &&
         verifyRange(range) &&
         kxAccel.getRawAccelRegisterData(&discardedSample);
}

void sendToClient(const char *line, size_t length)
{
  if (sensorClient.write(reinterpret_cast<const uint8_t *>(line), length) != length)
  {
    Serial.println("# TCP write failed; closing client for reconnection.");
    sensorClient.stop();
    digitalWrite(LED_BUILTIN, LOW);
  }
}

void reportSensorRange(bool greeting)
{
  const char *header = greeting ? "# KX134; acceleration in g; configured ODR=50 Hz\r\n"
                                 "read_ms,x_g,y_g,z_g\r\n" : "";
  char report[100];
  int length = snprintf(report, sizeof(report), "# RANGE %u\r\n%s",
                        unsigned(activeRange->fullScaleG), header);
  Serial.print(report);
  sendToClient(report, size_t(length));
}

void handleRangeCommand(const char *command)
{
  for (const RangeSetting &requested : SENSOR_RANGES)
  {
    if (strcmp(command, requested.command) != 0)
      continue;

    if (&requested != activeRange)
    {
      if (!applySensorRange(requested))
      {
        if (applySensorRange(*activeRange))
        {
          sensorClient.print("# RANGE ERROR Could not change range; previous range restored.\r\n");
          return;
        }
        sensorClient.print("# RANGE ERROR Sensor configuration failed. Power cycle the Arduino.\r\n");
        sensorClient.stop();
        haltWithError("KX134 range change and recovery failed.");
      }
      activeRange = &requested;
    }
    reportSensorRange(false);
    return;
  }
  sensorClient.print("# RANGE ERROR Use RANGE 8, RANGE 16, RANGE 32, or RANGE 64.\r\n");
}

// Poll at the client check interval and retain incomplete lines across polls.
// Limit the bytes processed each time so an input burst cannot monopolize the loop.
void readClientCommands()
{
  int available = sensorClient.available();
  uint8_t input[64];
  if (available <= 0)
    return;
  size_t toRead = available < int(sizeof(input)) ? size_t(available) : sizeof(input);
  int count = sensorClient.read(input, toRead);
  for (int i = 0; i < count && sensorClient; ++i)
  {
    const char ch = input[i];
    if (ch == '\n')
    {
      if (discardCommand)
        sensorClient.print("# RANGE ERROR Command too long or invalid.\r\n");
      else if (commandLength > 0)
      {
        if (commandLine[commandLength - 1] == '\r')
          --commandLength;
        commandLine[commandLength] = '\0';
        if (commandLength > 0)
          handleRangeCommand(commandLine);
      }
      commandLength = 0;
      discardCommand = false;
    }
    else if (!discardCommand)
    {
      if (ch == '\0' || commandLength >= sizeof(commandLine) - 1)
        discardCommand = true;
      else
        commandLine[commandLength++] = ch;
    }
  }
}

// Limit connection checks to reduce traffic over the internal modem link.
// There is no blocking client loop, so sensor polling continues between checks.
void serviceClient()
{
  const unsigned long now = millis();
  if (now - lastClientCheckMs < CLIENT_CHECK_MS)
    return;
  lastClientCheckMs = now;

  if (sensorClient)
  {
    if (!sensorClient.connected())
    {
      sensorClient.stop();
      digitalWrite(LED_BUILTIN, LOW);
      Serial.println("# TCP client disconnected; waiting for reconnection.");
    }
    else
    {
      readClientCommands();
    }
    return;
  }

  // accept() accepts a connection without requiring the laptop to send a byte.
  sensorClient = sensorServer.accept();
  if (sensorClient)
  {
    commandLength = 0;
    discardCommand = false;
    digitalWrite(LED_BUILTIN, HIGH);
    Serial.println("# TCP client connected.");
    reportSensorRange(true);
  }
}

// Poll for a fresh sample, then mirror the same CSV line to USB and TCP.
void streamSensorReading()
{
  if (kxAccel.dataReady() && kxAccel.getAccelData(&sensorData))
  {
    lastGoodReadingMs = millis();
    sampleLine = String(lastGoodReadingMs);
    sampleLine += ',';
    sampleLine += String(sensorData.xData, 4);
    sampleLine += ',';
    sampleLine += String(sensorData.yData, 4);
    sampleLine += ',';
    sampleLine += String(sensorData.zData, 4);
    sampleLine += "\r\n";

    Serial.print(sampleLine);
    if (sensorClient)
      sendToClient(sampleLine.c_str(), sampleLine.length());
  }

  // Make a stalled sensor visible instead of leaving a silently blank stream.
  const unsigned long now = millis();
  if (now - lastGoodReadingMs >= SENSOR_WARNING_MS &&
      now - lastSensorWarningMs >= SENSOR_WARNING_MS)
  {
    lastSensorWarningMs = now;
    const char warning[] = "# WARNING: No successful sensor read for 2 seconds.\r\n";
    Serial.print(warning);
    if (sensorClient)
      sensorClient.print(warning);
  }
}

void setup()
{
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  Serial.begin(SERIAL_BAUD);

  // A bounded wait keeps standalone power operation independent of USB.
  const unsigned long waitStartedMs = millis();
  while (!Serial && millis() - waitStartedMs < SERIAL_WAIT_MS)
    delay(10);

  Serial.println("# KX134 Wireless Sensor Readout Test");
  sampleLine.reserve(96);
  initializeSensor();
  startAccessPoint();
  Serial.println("read_ms,x_g,y_g,z_g");
  lastGoodReadingMs = millis();
}

void loop()
{
  serviceClient();
  streamSensorReading();
  delay(1); // Short polling pause; the configured sensor ODR controls new data.
}
