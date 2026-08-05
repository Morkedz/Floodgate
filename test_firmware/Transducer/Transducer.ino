#include <Wire.h>
#include <Adafruit_ADS1X15.h>

// ==========================================
// ESP32 I2C PIN CONFIGURATION
// ==========================================
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22

// Initialize ADS1115 ADC object
Adafruit_ADS1115 ads;

void setup() {
  Serial.begin(115200);
  delay(1000); // Allow time for Serial Monitor to initialize
  
  Serial.println("\n--- [TEST] Direct ADS1115 Transducer Reader ---");

  // Initialize I2C bus
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  delay(100);

  // Set gain to GAIN_ONE (+/- 4.096V range, 1 bit = 0.125mV)
  // Perfectly matches the DFRobot board's 0.4V to 2.0V output range
  ads.setGain(GAIN_ONE);

  // Start ADS1115 at default address 0x48
  if (ads.begin(0x48)) {
    Serial.println("[SUCCESS] ADS1115 detected at I2C address 0x48.");
  } else {
    Serial.println("[ERROR] ADS1115 NOT found! Check SDA (21), SCL (22), 3.3V, and GND wiring.");
    while (1) {
      delay(1000); // Halt execution if sensor isn't found
    }
  }

  Serial.println("Reading Channel A0 continuously...\n");
}

void loop() {
  // Read raw 16-bit ADC value from Channel 0 (AIN0)
  int16_t rawADC = ads.readADC_SingleEnded(0);

  // Convert raw value to voltage
  float voltage = ads.computeVolts(rawADC);

  // Display raw ADC count and calculated voltage
  Serial.print("Raw ADC: ");
  Serial.print(rawADC);
  Serial.print("\t | Measured Voltage: ");
  Serial.print(voltage, 4);
  Serial.println(" V");

  // Read twice every second
  delay(500);
}