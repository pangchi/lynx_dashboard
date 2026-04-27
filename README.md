# BriskHeat LYNX – Ultimate Real-Time Dashboard

An all-in-one Python-based monitoring and control interface for the BriskHeat LYNX Temperature Control System. This tool provides a live web dashboard, a WebSocket feed for real-time updates, and a full REST API for automation.

---

## 🚀 Features

* **Real-Time Dashboard:** High-performance web UI with color-coded status alerts (OK, Heating, Over-temp, No TC).
* **Live WebSockets:** Pushes updates to all connected clients every 8 seconds—no page refreshes required.
* **Full REST API:** Support for single and batch setpoint updates via JSON.
* **Auto-Installation:** Automatically detects and installs missing dependencies (`Flask`, `SocketIO`, `PyModbus`, `Eventlet`).
* **Mobile-Ready:** Fully responsive design that works on phones, tablets, and desktop browsers.

---

## 🛠️ Installation & Setup

1.  **Requirement:** Ensure `lynx_reader.py` (the core communication class) is located in the same directory as this script.
2.  **Configuration:** Open the script and edit the following variables to match your hardware:
    ```python
    OI_HOST = "192.168.200.20"  # Your OI Gateway IP Address
    OI_PORT = 502               # Default Modbus Port
    ```
3.  **Run:** Execute the script using Python 3:
    ```bash
    python3 your_script_name.py
    ```

---

## 🌐 How to Access

Once running, the dashboard is available on your local network:
* **Web Dashboard:** `http://localhost:5000` (or replace `localhost` with your PC's IP).
* **JSON Status:** `http://localhost:5000/api/status`

---

## 📡 API Reference

### 1. Get System Status
**Endpoint:** `GET /api/status`  
Returns a JSON object containing all active zones, current temperatures (PV), setpoints (SP), and status strings.

### 2. Update Setpoint (Single)
**Endpoint:** `POST /api/setpoint`  
**Payload:**
```json
{
  "line": 1,
  "zone": 1,
  "sp": 150.5
}
```

### 3. Update Setpoints (Batch)
**Endpoint:** `POST /api/setpoint`  
**Payload:**
```json
{
  "updates": [
    {"line": 1, "zone": 1, "sp": 100.0},
    {"line": 1, "zone": 2, "sp": 105.5}
  ]
}
```

---

## 🛡️ Technical Details

* **Async Mode:** Uses `eventlet` for high-concurrency WebSocket performance.
* **Polling Loop:** A background thread manages the Modbus connection to prevent the Web UI from freezing during network timeouts.
* **Error Handling:** Includes a built-in "Debug Mode" that silences terminal noise while logging critical communication errors to the dashboard.

> **Note:** The script will automatically attempt to install `flask`, `flask-socketio`, `pymodbus`, and `eventlet` on the first run if they are not detected.
