#include <SPI.h>
#include <mcp_can.h>
#include <WiFi.h>
#include <HTTPClient.h>

#define CAN_CS    5
#define NUM_CELLS 13

MCP_CAN CAN(CAN_CS);

const char* ssid       = "";
const char* password   = "";
const char* serverUrl  = "";
const char* battery_id = "354018111929984";

float        voltage    = 0;
float        current    = 0;
int          soc        = 0;
float        mosfetTemp = 0;
float        capacity   = 0;
unsigned int cycles     = 0;

float avgCell = 0;
float minCell = 0;
float maxCell = 0;

byte statusA = 0;
byte statusB = 0;
byte statusC = 0;
byte statusD = 0;

float chargeVoltageLimit    = 0;
float dischargeVoltageLimit = 0;
float chargeCurrentLimit    = 0;
float dischargeCurrentLimit = 0;

unsigned long lastSend        = 0;
unsigned long lastCANReceived = 0;
bool          batteryConnected = false;
unsigned int  lastRawCycles   = 0;

void resetAll() {
  voltage = current = soc = mosfetTemp = capacity = cycles = 0;
  avgCell = minCell = maxCell = 0;
  chargeVoltageLimit = dischargeVoltageLimit = 0;
  chargeCurrentLimit = dischargeCurrentLimit = 0;
  statusA = statusB = statusC = statusD = 0;
  lastRawCycles = 0;
}

void setup() {
  Serial.begin(115200);
  pinMode(CAN_CS, OUTPUT);

  if (CAN.begin(MCP_ANY, CAN_500KBPS, MCP_16MHZ) == CAN_OK)
    Serial.println("CAN INIT OK");
  else {
    Serial.println("CAN INIT FAIL");
    while (1);
  }
  CAN.setMode(MCP_NORMAL);

  WiFi.begin(ssid, password);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi Connected!");
}

void loop() {
  long unsigned int rxId;
  unsigned char     len = 0;
  unsigned char     buf[8];

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi Not Connected");
    delay(1000);
    return;
  }

  if (batteryConnected && millis() - lastCANReceived > 2000) {
    batteryConnected = false;
    resetAll();
    Serial.println("\n*** Battery Disconnected ***\n");
  }

  if (CAN.checkReceive() == CAN_MSGAVAIL) {
    CAN.readMsgBuf(&rxId, &len, buf);
    lastCANReceived  = millis();
    batteryConnected = true;

    // 0x351: Limits only (current removed from here)
    if (rxId == 0x351) {
      chargeVoltageLimit    = ((buf[1] << 8) | buf[0]) * 0.1;
      dischargeVoltageLimit = ((buf[5] << 8) | buf[4]) * 0.1;
      chargeCurrentLimit    = ((buf[7] << 8) | buf[6]) * 0.1;
    }

    // 0x355: SOC + Capacity
    if (rxId == 0x355) {
      soc = buf[0];
      unsigned int rawCap = (buf[5] << 8) | buf[4];
      capacity = rawCap * 0.00311;
    }

    // 0x356: Voltage + CURRENT (FIXED) + Temperature
    if (rxId == 0x356) {
      unsigned int rawV = (buf[1] << 8) | buf[0];
      voltage = rawV * 0.01;

      // FIXED: current from buf[3:2] signed * 0.1 (no offset)
      uint16_t rawCu = ((uint16_t)buf[3] << 8) | buf[2];
      int16_t  rawCs = (int16_t)rawCu;
      current = rawCs * 0.1f;

      int16_t rawTemp = (int16_t)(((uint16_t)buf[5] << 8) | buf[4]);
      mosfetTemp = rawTemp * 0.1;

      if (voltage > 10) {
        avgCell = voltage / NUM_CELLS;
        minCell = avgCell - 0.002;
        maxCell = avgCell + 0.002;
      }
    }

    // 0x359: Cycles + Alarm/Warning flags
    if (rxId == 0x359) {
      statusA = buf[0];
      statusB = buf[1];
      unsigned int rawCycles = (buf[3] << 8) | buf[2];
      if (rawCycles > 0 && rawCycles != lastRawCycles) {
        lastRawCycles = rawCycles;
        cycles        = rawCycles - 1;
      }
    }

    // 0x35C: Extended status
    if (rxId == 0x35C) {
      statusC = buf[0];
      statusD = buf[1];
    }

    // Print + Send on 0x359
    if (rxId == 0x359) {
      Serial.println("\n========== LIVE BMS DATA ==========");
      Serial.print("Voltage         : "); Serial.print(voltage, 2);                    Serial.println(" V");
      Serial.print("Current         : "); Serial.print(current, 2);                    Serial.println(" A");
      Serial.print("SOC             : "); Serial.print(soc);                           Serial.println(" %");
      Serial.print("Capacity        : "); Serial.print(capacity, 1);                   Serial.println(" Ah");
      Serial.print("Temperature     : "); Serial.print(mosfetTemp, 1);                 Serial.println(" C");
      Serial.print("Cycles          : "); Serial.println(cycles);
      Serial.println("── Voltage Limits ──────────────────");
      Serial.print("Charge Limit    : "); Serial.print(chargeVoltageLimit, 1);         Serial.println(" V");
      Serial.print("Discharge Limit : "); Serial.print(dischargeVoltageLimit, 1);      Serial.println(" V");
      Serial.print("Charge I Limit  : "); Serial.print(chargeCurrentLimit, 1);         Serial.println(" A");
      Serial.println("── Cell Estimates (13S) ────────────");
      Serial.print("Avg Cell        : "); Serial.print(avgCell, 3);                    Serial.println(" V");
      Serial.print("Min Cell (est)  : "); Serial.print(minCell, 3);                    Serial.println(" V");
      Serial.print("Max Cell (est)  : "); Serial.print(maxCell, 3);                    Serial.println(" V");
      Serial.print("Cell Diff (est) : "); Serial.print((maxCell - minCell) * 1000, 1); Serial.println(" mV");
      Serial.println("── Status ──────────────────────────");
      Serial.print("Alarm Flags     : 0x"); Serial.println(statusA, HEX);
      Serial.print("Warning Flags   : 0x"); Serial.println(statusB, HEX);
      Serial.print("Battery         : "); Serial.println(batteryConnected ? "Connected" : "Disconnected");
      Serial.println("====================================");

      if (millis() - lastSend > 5000) {
        HTTPClient http;
        http.begin(serverUrl);
        http.addHeader("Content-Type", "application/json");

        String jsonData = "{";
        jsonData += "\"battery_id\":\""            + String(battery_id)                        + "\",";
        jsonData += "\"voltage\":"                  + String(voltage, 2)                        + ",";
        jsonData += "\"current\":"                  + String(current, 2)                        + ",";
        jsonData += "\"soc\":"                      + String(soc)                               + ",";
        jsonData += "\"temperature\":"              + String(mosfetTemp, 1)                     + ",";
        jsonData += "\"capacity\":"                 + String(capacity, 1)                       + ",";
        jsonData += "\"cycles\":"                   + String(cycles)                            + ",";
        jsonData += "\"charge_voltage_limit\":"     + String(chargeVoltageLimit, 1)             + ",";
        jsonData += "\"discharge_voltage_limit\":"  + String(dischargeVoltageLimit, 1)          + ",";
        jsonData += "\"charge_current_limit\":"     + String(chargeCurrentLimit, 1)             + ",";
        jsonData += "\"avg_cell_voltage\":"          + String(avgCell, 3)                        + ",";
        jsonData += "\"min_cell_voltage\":"          + String(minCell, 3)                        + ",";
        jsonData += "\"max_cell_voltage\":"          + String(maxCell, 3)                        + ",";
        jsonData += "\"cell_diff_mv\":"              + String((maxCell - minCell) * 1000, 1)     + ",";
        jsonData += "\"alarm_flags\":"               + String(statusA)                           + ",";
        jsonData += "\"warning_flags\":"             + String(statusB)                           + ",";
        jsonData += "\"timestamp\":"                 + String(millis());
        jsonData += "}";

        int code = http.POST(jsonData);
        Serial.print("Sent HTTP: "); Serial.println(code);
        http.end();
        lastSend = millis();
      }
    }
  }
}
