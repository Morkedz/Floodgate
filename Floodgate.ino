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

// Sleep configuration: 60 seconds interval (adjust as needed)
#define uS_TO_S_FACTOR 1000000ULL  /* Conversion factor for micro-seconds to seconds */
#define TIME_TO_SLEEP  15          /* Time ESP32 will stay in deep sleep (seconds) */

// ==========================================
// 2. HARDWARE PIN DEFINITIONS
// ==========================================
#define MOSFET_BOOST_ENABLE_PIN 18  // Controls IRLZ44N MOSFET to toggle MT3608 12V boost
#define I2C_SDA_PIN 21              // SDA for BMP280 & ADS1115 ADC
#define I2C_SCL_PIN 22              // SCL for BMP280 & ADS1115 ADC

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
// 4. NETWORK & MQTT HELPERS
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
      digitalWrite(2,LOW);
      delay(1000);
      digitalWrite(2,HIGH);
      delay(1000);
      digitalWrite(2,LOW);
      delay(1000);
      digitalWrite(2,HIGH);
    } else {
      Serial.print(" Failed, rc=");
      digitalWrite(2,LOW);
      delay(1000);
      digitalWrite(2,HIGH);
      Serial.println(mqttClient.state());
    }
  }
}

// ==========================================
// 5. SENSOR INITIALIZATION & SAMPLING
// ==========================================
void setupSensors() {
  pinMode(MOSFET_BOOST_ENABLE_PIN, OUTPUT);
  digitalWrite(MOSFET_BOOST_ENABLE_PIN, LOW);  // Keep 12V off initially

  pinMode(2,OUTPUT);

  // Initialize I2C bus explicitly
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  delay(100); // Give I2C lines time to settle on wake

  // Retry loop for BMP280 (helps catch it if it was booting slow)
  for (int i = 0; i < 3; i++) {
    if (bmp.begin(0x76) || bmp.begin(0x77)) {
      bmpAvailable = true;
      Serial.println("BMP found");
      break;
    }else{
      Serial.println("BMP not found");
    }
    delay(100);
  }

  // Retry loop for ADS1115
  ads.setGain(GAIN_ONE);  // +/- 4.096V range
  for (int i = 0; i < 3; i++) {
    if (ads.begin(0x48)) {
      adsAvailable = true;
      Serial.println("Ads found");
      break;
    }else{
      Serial.println("Ads not found");
    }
    delay(100);
  }
}

void readAndPublishSensors() {
  float atmPressureHPa = 1013.25;
  float ambientTempC = 20.0;
  float transducerVolts = 0.0;
  float depth;

  // --- Step A: Read BMP280 Atmospheric Data ---
  if (bmpAvailable) {
    ambientTempC = bmp.readTemperature();
    atmPressureHPa = bmp.readPressure();
  }

  // --- Step B: Read 12V Submersible Pressure Transducer ---
  digitalWrite(MOSFET_BOOST_ENABLE_PIN, HIGH);
  delay(50);  // Allow 50ms for 12V boost stabilization

  if (adsAvailable) {
    int16_t adcRaw = ads.readADC_SingleEnded(0);
    transducerVolts = ads.computeVolts(adcRaw);
    depth = (transducerVolts <= 0.4f) ? 0.0f : (transducerVolts - 0.472f) * 3.125f;
  } else {
    transducerVolts = 0.50 + ((random(-50, 50)) / 1000.0);
  }

  digitalWrite(MOSFET_BOOST_ENABLE_PIN, LOW);  // Shut off 12V boost immediately

  // --- Step C: Build JSON Telemetry Payload ---
  StaticJsonDocument<256> doc;
  doc["device_id"] = "ESP32-FloodGate";
  doc["water_depth"] = round(depth * 1000.0) / 1000.0;
  doc["atm_pressure_hpa"] = round(atmPressureHPa * 10.0) / 10.0;
  doc["ambient_temp_c"] = round(ambientTempC * 10.0) / 10.0;
  doc["status"] = "OK";

  char jsonBuffer[256];
  serializeJson(doc, jsonBuffer);

  // --- Step D: Publish to MQTT Broker ---
  reconnectMQTT();
  if (mqttClient.connected()) {
    mqttClient.publish(MQTT_TOPIC, jsonBuffer);
    mqttClient.loop();
    Serial.print("[MQTT] Published Payload: ");
    Serial.println(jsonBuffer);
  }
}

// ==========================================
// 6. MAIN SETUP & SLEEP LOOP
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n[Boot] Wake up from deep sleep...");

  setupSensors();
  setupWiFi();
  
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);

  // 1. Take sensor readings and publish them right away
  readAndPublishSensors();

  // 2. Prepare and enter Deep Sleep immediately after publishing
  Serial.print("[Sleep] Going into deep sleep for ");
  Serial.print(TIME_TO_SLEEP);
  Serial.println(" seconds...\n");
  Serial.flush();

  esp_sleep_enable_timer_wakeup(TIME_TO_SLEEP * uS_TO_S_FACTOR);
  esp_deep_sleep_start();
}

void loop() {
  
}