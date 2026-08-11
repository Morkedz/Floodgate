#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <Adafruit_ADS1X15.h>
#include <ArduinoJson.h>
#include "secrets.h"

// ==========================================
// 1. NETWORK & SYSTEM CONFIGURATION
// ==========================================
const char* WIFI_SSID = SECRET_SSID;
const char* WIFI_PASSWORD = SECRET_PASS;

const char* MQTT_BROKER = "broker.hivemq.com";
const int MQTT_PORT = 1883;
const char* MQTT_TOPIC = TOPIC;

// Sleep configuration constants
#define uS_TO_S_FACTOR 1000000ULL  /* Conversion factor for microseconds to seconds */
#define DEFAULT_SLEEP_TIME 900     /* Default: 15 minutes (900 seconds) */
#define HIGH_RISK_SLEEP_TIME 120   /* High Risk: 2 minutes (120 seconds) */

// Dynamic sleep time stored in RTC memory to survive deep sleep cycles
RTC_DATA_ATTR int sleepDuration = DEFAULT_SLEEP_TIME;

// ==========================================
// 2. HARDWARE PIN DEFINITIONS
// ==========================================
#define I2C_SDA_PIN 21              // SDA for BMP280 & ADS1115 ADC
#define I2C_SCL_PIN 22              // SCL for BMP280 & ADS1115 ADC
#define LED_BUILTIN_PIN 2           // Status LED pin

// ==========================================
// 3. OBJECT INITIALIZATION & GLOBAL STATE
// ==========================================
WiFiClient espClient;
PubSubClient mqttClient(espClient);

Adafruit_BMP280 bmp;   // BMP280 Barometric Pressure Sensor
Adafruit_ADS1115 ads;  // ADS1115 16-Bit ADC

bool bmpAvailable = false;
bool adsAvailable = false;

// ==========================================
// 4. MQTT CALLBACK FOR RISK UPDATES
// ==========================================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<512> doc;
  DeserializationError error = deserializeJson(doc, payload, length);

  if (error) {
    Serial.print("[MQTT Callback] JSON Parsing Failed: ");
    Serial.println(error.f_str());
    return;
  }

  // Check if message came from the Python OpenWeather Bridge
  if (doc.containsKey("source") && strcmp(doc["source"], "openweather_api") == 0) {
    int highRisk = doc["high risk"] | 0;

    if (highRisk == 1) {
      sleepDuration = HIGH_RISK_SLEEP_TIME; // 2 minutes
      Serial.println("\n[RISK ALERT] High Risk Weather Forecast Detected!");
      Serial.printf("[RISK ALERT] Updated Sleep Duration to %d seconds (2 mins).\n", sleepDuration);
    } else {
      sleepDuration = DEFAULT_SLEEP_TIME;   // 15 minutes
      Serial.println("\n[RISK ALERT] Low/Normal Risk Weather. Setting Sleep Duration to 15 mins.");
    }
  }
}

// ==========================================
// 5. NETWORK & MQTT HELPERS
// ==========================================
void setupWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttemptTime = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttemptTime < 10000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    Serial.print(" IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi Connection Failed! Proceeding with cycle anyway.");
  }
}

void reconnectMQTT() {
  if (!mqttClient.connected()) {
    Serial.print("Connecting to MQTT Broker...");
    String clientId = "ESP32-FloodGate-" + String(random(0xffff), HEX);

    if (mqttClient.connect(clientId.c_str())) {
      Serial.println(" Connected!");
      
      // Subscribe to the shared channel to receive OpenWeather risk broadcasts
      mqttClient.subscribe(MQTT_TOPIC);
      Serial.print("[MQTT] Subscribed to topic: ");
      Serial.println(MQTT_TOPIC);

      digitalWrite(LED_BUILTIN_PIN, LOW);
      delay(200);
      digitalWrite(LED_BUILTIN_PIN, HIGH);
      delay(200);
      digitalWrite(LED_BUILTIN_PIN, LOW);
      delay(200);
      digitalWrite(LED_BUILTIN_PIN, HIGH);
    } else {
      Serial.print(" Failed, rc=");
      Serial.println(mqttClient.state());
    }
  }
}

// ==========================================
// 6. SENSOR INITIALIZATION & SAMPLING
// ==========================================
void setupSensors() {
  pinMode(LED_BUILTIN_PIN, OUTPUT);

  // Initialize I2C bus explicitly
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  delay(100); // Give I2C lines time to settle on wake

  // Retry loop for BMP280
  for (int i = 0; i < 3; i++) {
    if (bmp.begin(0x76) || bmp.begin(0x77)) {
      bmpAvailable = true;
      Serial.println("BMP found");
      break;
    } else {
      Serial.println("BMP not found");
    }
    delay(100);
  }

  // Retry loop for ADS1115
  ads.setGain(GAIN_ONE);  // +/- 4.096V range
  for (int i = 0; i < 3; i++) {
    if (ads.begin(0x48)) {
      adsAvailable = true;
      Serial.println("ADS found");
      break;
    } else {
      Serial.println("ADS not found");
    }
    delay(100);
  }
}

void readAndPublishSensors() {
  float atmPressureHPa = 1013.25;
  float ambientTempC = 20.0;
  float transducerVolts = 0.0;
  float depth = 0.0f;

  // --- Step A: Read BMP280 Atmospheric Data ---
  if (bmpAvailable) {
    ambientTempC = bmp.readTemperature();
    atmPressureHPa = bmp.readPressure() / 100.0F;
  }

  // --- Step B: Read ADS1115 with 10-Sample Averaging ---
  if (adsAvailable) {
    delay(50); // Let power rail settle

    int32_t adcSum = 0;
    const int SAMPLES = 10;

    for (int i = 0; i < SAMPLES; i++) {
      adcSum += ads.readADC_SingleEnded(0);
      delay(10); // 10ms between samples filters out RF noise
    }

    int16_t adcAvg = adcSum / SAMPLES;
    transducerVolts = ads.computeVolts(adcAvg);
    Serial.print("[ADC] Measured Volts: ");
    Serial.println(transducerVolts, 4);

    const float zero_depth_volt = .4794f;
    depth = (transducerVolts - zero_depth_volt) * 2.75f;
  } else {
    Serial.println("[WARNING] ADS1115 unavailable! Check I2C connections.");
    transducerVolts = 0.472f;
    depth = 0.0f;
  }

  // --- Step C: Build & Publish JSON ---
  StaticJsonDocument<256> doc;
  doc["device_id"] = "ESP32-FloodGate";
  doc["water_depth"] = round(depth * 1000.0f) / 1000.0f;
  doc["atm_pressure_hpa"] = round(atmPressureHPa * 10.0f) / 10.0f;
  doc["ambient_temp_c"] = round(ambientTempC * 10.0f) / 10.0f;
  doc["status"] = adsAvailable ? "OK" : "ADC_ERROR";

  char jsonBuffer[256];
  serializeJson(doc, jsonBuffer);

  if (mqttClient.connected()) {
    mqttClient.publish(MQTT_TOPIC, jsonBuffer);
    mqttClient.loop();
    Serial.print("[MQTT] Published Payload: ");
    Serial.println(jsonBuffer);
  }
}

// ==========================================
// 7. MAIN SETUP & SLEEP LOOP
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n[Boot] Wake up from deep sleep...");

  setupSensors();
  setupWiFi();
  
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);

  reconnectMQTT();

  // Listen for 1.5 seconds to catch any weather risk payload broadcast
  if (mqttClient.connected()) {
    Serial.println("[MQTT] Listening briefly for weather risk updates...");
    unsigned long startListen = millis();
    while (millis() - startListen < 1500) {
      mqttClient.loop();
      delay(10);
    }
  }

  // 1. Take sensor readings and publish telemetry
  readAndPublishSensors();

  // 2. Enter Deep Sleep with dynamic duration
  Serial.printf("[Sleep] Entering deep sleep for %d seconds...\n\n", sleepDuration);
  Serial.flush();

  esp_sleep_enable_timer_wakeup(sleepDuration * uS_TO_S_FACTOR);
  esp_deep_sleep_start();
}

void loop() {
  // Empty - Code executes once in setup() then enters deep sleep
}