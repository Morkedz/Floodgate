# FloodGate: Edge-Computing IoT Flood & Clog Detection Gateway
FloodGate is an end-to-end urban runoff monitoring system designed to track water depth and atmospheric conditions at storm drain infrastructure. By uniting edge microcontrollers, local gateway bridging, cloud time-series storage, and client-side risk heuristics, FloodGate provides real-time flood warnings and automatic clog anomaly detection.


## Core Components
1. **Edge Sensing** (ESP32): Reads analog pressure sensor inputs to measure water depth and transmits JSON payloads over MQTT every 3 seconds.

2. **Gateway Bridge** (floodgatebridgeandweather.py): Runs as a background service on a Raspberry Pi 4. Subscribes to local MQTT topics, enriches sensor payloads with live barometric pressure from OpenWeatherMap, and commits records to Supabase over REST.

3. **Cloud Database** (Supabase): Stores historical time-series telemetry.

4. **Web UI & Risk Engine** (index.html + app.js): A zero-build, client-side web application served by the Pi's Python web server. Polls Supabase every 3 seconds to update Chart.js line charts and calculate dynamic risk percentages.


## Tech Stack
- **Hardware:** ESP32, Raspberry Pi 4, Analog Water Depth / Pressure Sensor.

- **Messaging & Discovery:** MQTT (paho-mqtt), mDNS (avahi-daemon).

- **Gateway Runtime:** Python 3 (supabase-py, requests, python-dotenv).

- **Database:** Supabase (PostgreSQL REST API).

- **Frontend:** Vanilla JavaScript (ES6+), HTML5, CSS3, Chart.js.

- **Process Management:** Linux systemd.


## Key Features & Risk Logic Engine
1. ### NOAA-Informed Flood Risk Engine
Evaluates static water depth combined with dynamic rates of rise ($dD/dt$) and barometric pressure drops ($dP/dt$):
- **Static Threshold:** $0.8\text{ m}$ depth is designated as critical municipal curb inundation ($100\%$ baseline).
- **Rate-of-Rise Calculus:** Uses a rolling 5-sample window. Inflow velocity $> 0.02\text{ m/sample}$ triggers a dynamic momentum bonus to account for flash flooding.
- **Barometric Drop:** Drops $> 0.5\text{ hPa}$ indicate incoming low-pressure storm cells and proactively boost flood risk alerts.

2. ### Clog Anomaly Detection Engine
Identifies debris blockages by evaluating water depth against atmospheric drivers:
- **Trigger:** Activates when water depth remains elevated ($> 0.15\text{ m}$).
- **Clear-Sky Anomaly:** High standing water during stable/high barometric pressure ($\Delta P \le 0.2\text{ hPa}$) indicates physical blockage (leaves/trash) and increases the clog score.
- **Storm Deduction:** If barometric pressure drops significantly, the clog score is reduced since high water depth is expected during active rain.

3. ### Frontend Fault Tolerance
- **Negative Reading Clamping:** Dry sensor hardware uncalibration (e.g., $-1.3\text{ m}$) is safely clamped using Math.max(0, depth) in app.js to prevent visual chart distortion or invalid risk calculations.


## Installation & Environment Configuration
This guide provides full instructions to deploy FloodGate across the ESP32 sensor hardware, Raspberry Pi gateway, and client interface.

System Prerequisites
1. ### Hardware Requirements
- ESP32 Development Board (NodeMCU or similar)

- Raspberry Pi 4 (Running Raspberry Pi OS Debian-based 64-bit)

- Analog Water Pressure/Depth Sensor (Connected to ESP32 Pin GPIO 34)

- BMP280 Barometric Sensor

- Micro-USB / USB-C Cables and Wi-Fi Network

2. ### Software Requirements
Development Machine: VS Code with PlatformIO Extension OR Arduino IDE

Raspberry Pi Gateway: Python 3.10+, pip, git, and avahi-daemon

**Step 1: ESP32 Firmware Setup & Calibration**
Clone the repository on your local computer:

```Bash 
git clone https://github.com/Morkedz/Floodgate.git
cd Floodgate/firmware
```

Open the firmware source file (firmware/src/main.cpp or firmware/FloodGate.ino) and update your network credentials:


```C++ 
const char* WIFI_SSID   = "YOUR_WIFI_SSID";
const char* WIFI_PASS   = "YOUR_WIFI_PASSWORD";
const char* MQTT_SERVER = "192.168.1.150"; // Replace with your Pi's local IP address
```

Sensor Calibration:
To ensure dry-sensor offset or environmental zero-point variations do not affect readings, adjust the offset multiplier in the sensor formula before uploading:


```C++ 
// Formula: Depth (m) = (Voltage - ZeroOffset) * ScaleFactor
float voltage = (rawAnalog / 4095.0) * 3.3;
float waterDepth = (voltage - 0.5) * 2.0; // Adjust 0.5 (zero offset) based on dry calibration
```
Connect your ESP32 via USB and upload the firmware using PlatformIO (Upload) or Arduino IDE (Select board ESP32 Dev Module and click Upload).

**Step 2: Raspberry Pi Gateway Deployment**
Log into your Raspberry Pi via SSH or direct terminal to set up the backend and frontend services.

1. Clone Repository to Full Path
Clone the project directly into the pi user's home directory so file paths match systemd configurations:

```Bash 
cd /home/pi
git clone https://github.com/Morkedz/Floodgate.git
cd /home/pi/Floodgate
```
Ensure your directory structure matches this full path layout:

```Plaintext
/home/pi/Floodgate/
├── backend/
│   └── floodgatebridgeandweather.py
├── frontend/
│   ├── index.html
│   └── app.js
├── firmware/
│   └── src/
├── .env
└── README.md
```

2. Install Required Packages
Update system repositories and install system packages and Python libraries:

```Bash 
# Update system and install system services
sudo apt update
sudo apt install python3 python3-pip avahi-daemon mosquitto mosquitto-clients -y

# Enable local network mDNS discovery (floodgate.local)
sudo systemctl enable --now avahi-daemon

# Install Python dependencies
pip3 install paho-mqtt supabase requests python-dotenv
```

3. Configure Environment Variables
Create the .env file in the root project directory:

```Bash 
nano /home/pi/Floodgate/.env
```
Paste and update the following settings:

```Code snippet
SUPABASE_URL="https://YOUR_SUPABASE_PROJECT.supabase.co"
SUPABASE_KEY="YOUR_SUPABASE_ANON_KEY"
OPENWEATHER_API_KEY="YOUR_OPENWEATHER_API_KEY"
CITY_NAME="YourCityName"
MQTT_BROKER="localhost"
MQTT_PORT=1883
MQTT_TOPIC="floodgate/telemetry"
```
4. Configure Supabase Cloud Schema
Log into your Supabase Dashboard, open the SQL Editor, and run the following schema script to create the time-series table:

```SQL
CREATE TABLE telemetry (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    water_depth FLOAT NOT NULL,
    baro_pressure FLOAT NOT NULL,
    temperature FLOAT,
    weather_condition TEXT
);
```
Step 3: Headless Boot Services Setup (systemd)
To make FloodGate plug-and-play, configure systemd daemons to automatically launch the MQTT-to-Cloud Bridge and Web Dashboard on Pi boot.

1. Create Bridge Background Service
```Bash 
sudo nano /etc/systemd/system/floodgate-bridge.service
```
Paste the following configuration:

```Ini, TOML
[Unit]
Description=FloodGate Python Bridge Service
After=network.target mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Floodgate
ExecStart=/usr/bin/python3 /home/pi/Floodgate/backend/floodgatebridgeandweather.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
2. Create Web Dashboard Service
```Bash 
sudo nano /etc/systemd/system/floodgate-web.service
```
Paste the following configuration:

```Ini, TOML
[Unit]
Description=FloodGate Web Dashboard Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Floodgate/frontend
ExecStart=/usr/bin/python3 -m http.server 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
3. Enable and Start Services
Register and execute both daemons:

```Bash 
# Reload systemd configuration
sudo systemctl daemon-reload

# Enable services to launch on system boot
sudo systemctl enable floodgate-bridge.service
sudo systemctl enable floodgate-web.service

# Start services immediately
sudo systemctl start floodgate-bridge.service
sudo systemctl start floodgate-web.service
```
Step 4: Plug-and-Play System Calibration & Verification
Once configured, the system operates completely headless:

Powering On: Power the Raspberry Pi first. Allow 30 seconds for systemd services and the MQTT broker to initialize.

Powering Edge Node: Power the ESP32. It will automatically connect to Wi-Fi and begin publishing sensor telemetry to the Pi over MQTT.

Dry-Calibration Verification: Verify that dry-sensor baseline readings display close to 0.00 m on the dashboard. The frontend algorithm will automatically clamp minor sensor fluctuations below 0.00 m using internal logic safety parameters.

Diagnostic Verification Commands
Check Service Statuses:

```Bash 
sudo systemctl status floodgate-bridge.service
sudo systemctl status floodgate-web.service
```
Inspect Live Telemetry Logs:

```Bash 
journalctl -u floodgate-bridge.service -f
```
Accessing the UI
Open any browser on a device connected to the same Wi-Fi network and navigate to:

mDNS URL: http://floodgate.local:8080

Direct IP URL: http://<PI_IP_ADDRESS>:8080