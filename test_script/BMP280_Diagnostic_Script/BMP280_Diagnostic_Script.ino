#include <Wire.h>
#include <Adafruit_BMP280.h>

// ==========================================
// PIN CONFIGURATION (ESP32 Standard I2C)
// ==========================================
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22

Adafruit_BMP280 bmp; // I2C interface instance
bool sensorFound = false;
uint8_t detectedAddress = 0x00;

// ==========================================
// HELPER: I2C BUS SCANNER
// ==========================================
void scanI2CBus() {
  Serial.println("\n-------------------------------------------");
  Serial.println("🔍 [STEP 1] Scanning I2C Bus...");
  Serial.println("-------------------------------------------");
  
  byte error, address;
  int deviceCount = 0;

  for (address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    error = Wire.endTransmission();

    if (error == 0) {
      Serial.printf("  └─ ► Active I2C Device Found at Address: 0x%02X", address);
      if (address == 0x76) Serial.print(" (Default BMP280 Address)");
      if (address == 0x77) Serial.print(" (Alternate BMP280 Address)");
      if (address == 0x48) Serial.print(" (ADS1115 ADC)");
      Serial.println();
      deviceCount++;
    } else if (error == 4) {
      Serial.printf("  └─ ❌ Unknown error at address 0x%02X\n", address);
    }
  }

  if (deviceCount == 0) {
    Serial.println("  ❌ NO I2C DEVICES FOUND!");
    Serial.println("     Troubleshooting Checklist:");
    Serial.println("     1. Check VCC (3.3V) and GND connections.");
    Serial.println("     2. Verify SDA is on GPIO 21 and SCL is on GPIO 22.");
    Serial.println("     3. Ensure CSB pin is pulled HIGH (connected to 3.3V) to force I2C mode.");
  } else {
    Serial.printf("✅ Scan complete. Found %d device(s).\n", deviceCount);
  }
  Serial.println("-------------------------------------------\n");
}

// ==========================================
// HELPER: SENSOR INITIALIZATION & PROBING
// ==========================================
void initBMP280() {
  Serial.println("🔍 [STEP 2] Attempting BMP280 Initialization...");

  // Probe Address 0x76 first
  if (bmp.begin(0x76)) {
    sensorFound = true;
    detectedAddress = 0x76;
    Serial.println("✅ SUCCESS: BMP280 initialized at address 0x76!");
  } 
  // Fallback to Address 0x77
  else if (bmp.begin(0x77)) {
    sensorFound = true;
    detectedAddress = 0x77;
    Serial.println("✅ SUCCESS: BMP280 initialized at address 0x77!");
  } 
  else {
    sensorFound = false;
    Serial.println("❌ FAILURE: Unable to communicate with BMP280!");
    Serial.println("   Common Causes:");
    Serial.println("   • CSB Pin Floating: Connect CSB directly to 3.3V.");
    Serial.println("   • SDO Pin Floating: Connect SDO to GND (for 0x76) or 3.3V (for 0x77).");
    Serial.println("   • Wrong Chip ID: If using a BME280 instead of BMP280, use Adafruit_BME280 library.");
    return;
  }

  /* Default sampling settings for general atmospheric monitoring */
  bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,     /* Operating Mode */
                  Adafruit_BMP280::SAMPLING_X2,     /* Temp. oversampling */
                  Adafruit_BMP280::SAMPLING_X16,    /* Pressure oversampling */
                  Adafruit_BMP280::FILTER_X16,      /* Filtering */
                  Adafruit_BMP280::STANDBY_MS_500); /* Standby time */
                  
  Serial.println("✅ Sensor configuration parameters set successfully.");
}

// ==========================================
// MAIN SETUP
// ==========================================
void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(10); } // Wait for Serial Monitor to connect

  delay(1000);
  Serial.println("\n===========================================");
  Serial.println("   BMP280 ISOLATED DIAGNOSTIC TOOL");
  Serial.println("===========================================");

  // Initialize Hardware I2C
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  
  // Step 1: Scan bus for raw hardware presence
  scanI2CBus();

  // Step 2: Attempt library driver initialization
  initBMP280();
}

// ==========================================
// MAIN LOOP: READINGS & VALIDATION
// ==========================================
void loop() {
  if (!sensorFound) {
    Serial.println("⚠️ Sensor not initialized. Retrying scan in 5 seconds...");
    delay(5000);
    scanI2CBus();
    initBMP280();
    return;
  }

  // Read raw parameters
  float tempC = bmp.readTemperature();
  float pressurePa = bmp.readPressure();
  float pressureHPa = pressurePa / 100.0F;
  float approxAltitudeM = bmp.readAltitude(1013.25); // Baseline standard sea level pressure

  Serial.println("-------------------------------------------");
  Serial.printf("📍 Device Address : 0x%02X\n", detectedAddress);
  
  // Temperature Check
  if (isnan(tempC) || tempC < -40.0 || tempC > 85.0) {
    Serial.printf("❌ Temp Reading  : INVALID (%.2f °C)\n", tempC);
  } else {
    Serial.printf("🌡️ Temperature   : %.2f °C  (%.2f °F)\n", tempC, (tempC * 9.0 / 5.0) + 32.0);
  }

  // Pressure Check
  if (isnan(pressureHPa) || pressureHPa < 300.0 || pressureHPa > 1100.0) {
    Serial.printf("❌ Pressure      : OUT OF RANGE (%.2f hPa)\n", pressureHPa);
  } else {
    Serial.printf("📊 Pressure     : %.2f hPa (%.2f Pa)\n", pressureHPa, pressurePa);
    Serial.printf("⛰️ Approx Altitude: %.2f m\n", approxAltitudeM);
  }

  delay(2000); // Read every 2 seconds
}