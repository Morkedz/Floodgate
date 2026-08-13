// FloodGate — ESP32 HTTP sensor sender for the Pi 5 edge brain
//
// Reads BMP280 (temp + pressure) and the DFRobot KIT0139 water-level
// transducer via ADS1115, then POSTs a JSON reading to the edge brain.
//
// IMPORTANT (alignment with Floodgate-main): this sketch emits the EXACT
// payload schema of the main project's floodgate_firmware.ino, so the edge
// brain, the main MQTT bridge and the Supabase dashboard all share one wire
// format:
//
//     {"device_id": "ESP32-FloodGate",
//      "water_depth": 0.245,          // METERS  (frontend charts "Depth (m)")
//      "atm_pressure_hpa": 1013.2,    // hPa
//      "ambient_temp_c": 24.1,        // deg C
//      "status": "OK"}                // "OK" | "ADC_ERROR"
//
// Calibration constants are copied from floodgate_firmware.ino:
//     depth_m = (volts - 0.4794) * 2.75
// and the same I2C pins (21/22), GAIN_ONE range and 10-sample averaging are
// used. If you'd rather not touch the ESP32 at all, the Pi can instead
// subscribe to the main MQTT topic (edge_brain.py --mqtt) and the existing
// floodgate_firmware.ino works unchanged — this HTTP sketch is the
// alternative direct path (5 s cadence, no broker needed).
//
// Libraries (Arduino Library Manager):
//   Adafruit BMP280, Adafruit ADS1X15, ArduinoJson

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <Adafruit_ADS1X15.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASS";
const char* PI_URL    = "http://192.168.1.50:8080/reading";  // <- Pi 5 IP

// Same I2C wiring as floodgate_firmware.ino (main project)
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22

Adafruit_BMP280 bmp;
Adafruit_ADS1115 ads;
bool bmpOk = false, adsOk = false;

// KIT0139 water transducer calibration — copied from floodgate_firmware.ino:
//   depth_m = (volts - zero_depth_volt) * 2.75
// Re-calibrate against two known water depths for your installation.
const float ZERO_DEPTH_VOLT = 0.4794f;   // volts with transducer in air
const float M_PER_VOLT      = 2.75f;     // meters of water per volt

void setup() {
  Serial.begin(115200);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  delay(100);

  bool bmp_ok = false, ads_ok = false;
  for (int i = 0; i < 3 && !bmp_ok; i++) {
    bmp_ok = bmp.begin(0x76) || bmp.begin(0x77);
    if (!bmp_ok) delay(100);
  }
  ads.setGain(GAIN_ONE);                 // +/- 4.096V, 1 bit = 0.125 mV
  for (int i = 0; i < 3 && !ads_ok; i++) {
    ads_ok = ads.begin(0x48);
    if (!ads_ok) delay(100);
  }
  bmpOk = bmp_ok; adsOk = ads_ok;
  Serial.printf("BMP280 %s, ADS1115 %s\n", bmpOk ? "OK" : "FAIL",
                adsOk ? "OK" : "FAIL");

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nWiFi connected: " + WiFi.localIP().toString());
}

void loop() {
  float ambientTempC = 20.0;
  float atmPressureHPa = 1013.25;
  if (bmpOk) {
    ambientTempC = bmp.readTemperature();             // deg C
    atmPressureHPa = bmp.readPressure() / 100.0F;     // hPa
  }

  // 10-sample averaged ADC read (same as floodgate_firmware.ino)
  int32_t adcSum = 0;
  const int SAMPLES = 10;
  for (int i = 0; i < SAMPLES; i++) {
    adcSum += ads.readADC_SingleEnded(0);
    delay(10);
  }
  int16_t adcAvg = adcSum / SAMPLES;
  float volts = ads.computeVolts(adcAvg);

  bool ads_ok = adsOk && (volts > 0.05f);
  float depthM = ads_ok ? max(0.0f, (volts - ZERO_DEPTH_VOLT) * M_PER_VOLT) : 0.0f;

  // --- Build the payload in the MAIN project's schema ---
  StaticJsonDocument<256> doc;
  doc["device_id"]       = "ESP32-FloodGate";
  doc["water_depth"]     = round(depthM * 1000.0f) / 1000.0f;      // meters
  doc["atm_pressure_hpa"]= round(atmPressureHPa * 10.0f) / 10.0f;  // hPa
  doc["ambient_temp_c"]  = round(ambientTempC * 10.0f) / 10.0f;    // deg C
  doc["status"]          = ads_ok ? "OK" : "ADC_ERROR";

  String body; serializeJson(doc, body);

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(PI_URL);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(body);
    Serial.printf("POST %d  %s\n", code, body.c_str());
    http.end();
  }
  delay(5000);
}
