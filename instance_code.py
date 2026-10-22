from flask import Flask, request, jsonify
import threading
import socket
import struct
from datetime import datetime
import boto3
import json
import time
from decimal import Decimal

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -------- GOOGLE SHEETS --------
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open("tracker_data").sheet1

# -------- AWS --------
s3 = boto3.client('s3')
BUCKET_NAME = "iot-s3-data-bms-project-2026"

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('iot_test_table_v2')

# -------- FLASK --------
app = Flask(__name__)

# -------- GLOBAL --------
latest_esp = {}
latest_tracker = {}
current_imei = "UNKNOWN"

lock = threading.Lock()

# 🔥 REAL-TIME TRACKING
last_esp_time = 0
last_tracker_time = 0
TIMEOUT = 15


# ---------------- STORAGE ----------------
def push_all(data):

    print("\n================= 📤 FINAL DATA =================")
    print(json.dumps(data, indent=2))
    print("================================================")

    try:
        row = [
            data.get("device_id",""),
            data.get("latitude",0),
            data.get("longitude",0),
            data.get("speed",0),
            data.get("altitude",0),
            data.get("timestamp",""),
            data.get("voltage",0),
            data.get("current",0),
            data.get("soc",0),
            data.get("cycles",0),
            data.get("satellites",0),
            data.get("temperature",0),
            data.get("capacity",0),
            data.get("charge_limit",0),
            data.get("discharge_limit",0),
            data.get("charge_current_limit",0),
            data.get("avg_cell",0),
            data.get("min_cell",0),
            data.get("max_cell",0),
            data.get("cell_diff",0),
            data.get("alarm_flags",0),
            data.get("warning_flags",0)
        ]

        sheet.append_row(row)
        print("✅ DATA STORED IN GOOGLE SHEETS")

    except Exception as e:
        print("❌ SHEETS ERROR:", e)

    try:
        item = {
            "device_id": str(data.get("device_id","UNKNOWN")),
            "ts": int(time.time())
        }

        for k,v in data.items():
            if isinstance(v,float):
                item[k] = Decimal(str(v))
            else:
                item[k] = v

        table.put_item(Item=item)
        print("✅ DATA STORED IN DYNAMODB")

    except Exception as e:
        print("❌ DYNAMODB ERROR:", e)

    try:
        safe_ts = str(data["timestamp"]).replace(" ","_").replace(":","-")
        key = f"{data['device_id']}/{safe_ts}.json"

        s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=json.dumps(data))
        print(f"✅ DATA STORED IN S3 → {key}")

    except Exception as e:
        print("❌ S3 ERROR:", e)

    print("================================================\n")


# ---------------- BUILD COMBINED ----------------
def build_combined(esp, tracker, timestamp):
    device_id = current_imei if current_imei != "UNKNOWN" else esp.get("device_id","UNKNOWN")

    return {
        "device_id": device_id,

        "latitude": tracker.get("latitude",0),
        "longitude": tracker.get("longitude",0),
        "altitude": tracker.get("altitude",0),
        "speed": tracker.get("speed",0),
        "satellites": tracker.get("satellites",0),

        "voltage": esp.get("voltage",0),
        "current": esp.get("current",0),
        "soc": esp.get("soc",0),
        "cycles": esp.get("cycles",0),
        "temperature": esp.get("temperature",0),
        "capacity": esp.get("capacity",0),

        "charge_limit": esp.get("charge_voltage_limit",0),
        "discharge_limit": esp.get("discharge_voltage_limit",0),
        "charge_current_limit": esp.get("charge_current_limit",0),

        "avg_cell": esp.get("avg_cell_voltage",0),
        "min_cell": esp.get("min_cell_voltage",0),
        "max_cell": esp.get("max_cell_voltage",0),
        "cell_diff": esp.get("cell_diff_mv",0),

        "alarm_flags": esp.get("alarm_flags",0),
        "warning_flags": esp.get("warning_flags",0),

        "timestamp": timestamp
    }


# ---------------- ESP ----------------
@app.route('/data', methods=['POST'])
def receive_data():
    global latest_esp, last_esp_time

    data = request.get_json()

    print("\n📡 ESP DATA:")
    print(json.dumps(data, indent=2))

    with lock:
        latest_esp.update(data)
        if "battery_id" in latest_esp:
            latest_esp["device_id"] = latest_esp["battery_id"]

        last_esp_time = time.time()

        esp = latest_esp.copy()
        tracker = latest_tracker.copy()

    now = time.time()

    esp_active = (now - last_esp_time) < TIMEOUT
    tracker_active = (now - last_tracker_time) < TIMEOUT

    print(f"\nSTATUS → ESP:{esp_active} TRACKER:{tracker_active}")

    if esp_active and tracker_active:
        combined = build_combined(esp, tracker, str(datetime.utcnow()))

    elif esp_active:
        combined = build_combined(esp, {}, str(datetime.utcnow()))

    elif tracker_active:
        combined = build_combined({}, tracker, str(datetime.utcnow()))
    else:
        return jsonify({"status":"no active device"})

    push_all(combined)

    return jsonify({"status":"success"}), 200


# ---------------- TRACKER ----------------
def start_tracker_server():
    global current_imei, latest_tracker, last_tracker_time

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("0.0.0.0",5000))
    server.listen(5)

    print("🚀 Tracker running on 5000")

    def decode_avl(data):
        global last_tracker_time

        offset = 8
        offset += 1
        record_count = data[offset]
        offset += 1

        for _ in range(record_count):

            timestamp = struct.unpack(">Q", data[offset:offset+8])[0]
            offset += 8

            offset += 1

            longitude = struct.unpack(">i", data[offset:offset+4])[0]/10000000
            offset += 4

            latitude = struct.unpack(">i", data[offset:offset+4])[0]/10000000
            offset += 4

            altitude = struct.unpack(">h", data[offset:offset+2])[0]
            offset += 2

            offset += 2

            satellites = data[offset]
            offset += 1

            speed = struct.unpack(">h", data[offset:offset+2])[0]
            offset += 2

            try:
                gps_time = str(datetime.utcfromtimestamp(timestamp/1000))
            except:
                print("❌ INVALID TIMESTAMP")
                return 0

            print(f"\n📍 TRACKER: {latitude},{longitude} Speed:{speed}")

            with lock:
                latest_tracker.update({
                    "latitude":latitude,
                    "longitude":longitude,
                    "altitude":altitude,
                    "speed":speed,
                    "satellites":satellites
                })

                last_tracker_time = time.time()

                esp = latest_esp.copy()

            now = time.time()

            esp_active = (now - last_esp_time) < TIMEOUT
            tracker_active = (now - last_tracker_time) < TIMEOUT

            if esp_active and tracker_active:
                combined = build_combined(esp, latest_tracker, gps_time)

            elif tracker_active:
                combined = build_combined({}, latest_tracker, gps_time)

            elif esp_active:
                combined = build_combined(esp, {}, gps_time)
            else:
                return 0

            push_all(combined)

            offset += 1
            offset += 1

            count = data[offset]; offset+=1; offset+=count*2
            count = data[offset]; offset+=1; offset+=count*3
            count = data[offset]; offset+=1; offset+=count*5
            count = data[offset]; offset+=1; offset+=count*9

        return record_count

    while True:
        conn, addr = server.accept()
        print("\nTracker connected:", addr)

        while True:
            data = conn.recv(4096)
            if not data:
                break

            if data.startswith(b'\x00\x0f'):
                current_imei = data[2:].decode()
                print("IMEI:", current_imei)
                conn.send(b'\x01')
                continue

            try:
                records = decode_avl(data)
                conn.send(struct.pack(">I", records))
            except Exception as e:
                print("Decode error:", e)

        conn.close()


# ---------------- MAIN ----------------
if __name__ == '__main__':
    threading.Thread(target=start_tracker_server, daemon=True).start()
    print("🚀 Flask running on 8000")
    app.run(host='0.0.0.0', port=8000)
