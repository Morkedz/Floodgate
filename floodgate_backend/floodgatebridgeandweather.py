import json
import os
import time
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from supabase import create_client, Client
import requests
from datetime import datetime, timedelta

load_dotenv()

# --- CONFIGURATION ---
API_KEY = os.getenv("API_KEY")  # OpenWeather API Key
CITY = "Potomac"
COUNTRY_CODE = "US"
FORECAST_HOURS = 1
UNITS = "metric"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "umd/cpse/floodgate/telemetry"  # Must match secrets.h in firmware

def get_forecast(mqtt_client, city, hours_from_now=1):
    url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?q={city},{COUNTRY_CODE}"
        f"&appid={API_KEY}"
        f"&units={UNITS}"
    )

    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        target_time = datetime.utcnow() + timedelta(hours=hours_from_now)

        closest = min(
            data["list"],
            key=lambda x: abs(
                datetime.strptime(x["dt_txt"], "%Y-%m-%d %H:%M:%S") - target_time
            ),
        )

        weather = closest["weather"][0]
        main = closest["main"]
        wind = closest["wind"]

        #package forecast into dictionary
        if (weather['id'] > 199 and weather['id'] < 230) or (weather['id'] > 500 and weather['id'] < 532) or (weather['id'] > 600 and weather['id'] < 623):
            forecast_payload = {
                "source": "openweather_api",
                "city": city,
                "forecast_time_utc": closest['dt_txt'],
                "high risk": 1
            }
        else:
            forecast_payload = {
                "source": "openweather_api",
                "city": city,
                "forecast_time_utc": closest['dt_txt'],
                "high risk": 0
            }
        #convert dictionary to json string and publish
        payload_json = json.dumps(forecast_payload)
        mqtt_client.publish(MQTT_TOPIC, payload_json)
        print(f"[MQTT] Published forecast payload: {payload_json}")

    except Exception as e:
        print(f"[Error] Failed to fetch or publish forecast: {e}")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- MQTT CALLBACKS ---
def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected successfully to {MQTT_BROKER} with code {rc}")
    client.subscribe(MQTT_TOPIC)
    print(f"[MQTT] Subscribed to topic: {MQTT_TOPIC}")

def on_message(client, userdata, msg):
    try:
        # Decode and parse incoming JSON payload
        raw_payload = msg.payload.decode("utf-8")
        data = json.loads(raw_payload)
        print(f"[MQTT Payload]: {data}")

        # Optional: Prevent your bridge from trying to send OpenWeather data into 
        # the Supabase sensor_readings table if it doesn't match the schema.
        if data.get("source") == "openweather_api":
            print("[Bridge] Skipping Supabase insert for weather forecast data.")
            return

        # Map JSON keys to Supabase columns
        db_record = {
            "device_id": data.get("device_id"),
            "water_depth": data.get("water_depth"),
            "atm_pressure_hpa": data.get("atm_pressure_hpa"),
            "ambient_temp_c": data.get("ambient_temp_c"),
            "status": data.get("status")
        }

        # Insert into database
        response = supabase.table("sensor_readings").insert(db_record).execute()
        print("[Supabase] Inserted record successfully.")

    except Exception as e:
        print(f"[Error] Failed to process message: {e}")

# --- START BRIDGE WORKER ---
if __name__ == "__main__":
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    print("[Bridge] Connecting to MQTT...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    try:
        # This is your new repeating loop
        while True:
            # 1. Fetch and publish the weather
            get_forecast(client, CITY, FORECAST_HOURS)
            
            # 2. Wait before fetching again (e.g., 3600 seconds = 1 hour)
            # OpenWeather limits free API calls, so don't set this too low!
            update_interval = 600 
            print(f"[Bridge] Waiting {update_interval} seconds for next weather update...")
            time.sleep(update_interval)

    except KeyboardInterrupt:
        # Safely shut down if you press Ctrl+C
        print("\n[Bridge] Shutting down...")
        client.loop_stop()
        client.disconnect()