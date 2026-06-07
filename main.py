"""
Solaren — Cloud Backend
FastAPI server που τρέχει στο Render.com
Συνδέει το dashboard με Sungrow / Huawei / GoodWe / Fronius APIs
"""
import os, time, json, hashlib, logging, requests
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("solaren")

app = FastAPI(title="Solaren API")

# CORS — επιτρέπει στο dashboard να καλεί τον server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Σερβίρει το dashboard HTML ──
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/")
def root():
    return FileResponse("index.html")

# ══════════════════════════════
#  SUNGROW
# ══════════════════════════════
_sungrow_token = None
_sungrow_expires = None

def sungrow_login():
    global _sungrow_token, _sungrow_expires
    r = requests.post("https://gateway.isolarcloud.com.hk/openapi/login", json={
        "appkey": os.getenv("SUNGROW_APP_ID"),
        "token":  os.getenv("SUNGROW_TOKEN"),
    }, timeout=10)
    data = r.json()
    if data.get("result_code") == "1":
        _sungrow_token = data["result_data"]["token"]
        _sungrow_expires = datetime.now() + timedelta(hours=2)

def get_sungrow_token():
    if not _sungrow_token or datetime.now() > (_sungrow_expires or datetime.min):
        sungrow_login()
    return _sungrow_token

@app.get("/api/sungrow/sites")
def sungrow_sites():
    try:
        token = get_sungrow_token()
        r = requests.post(
            "https://gateway.isolarcloud.com.hk/openapi/getPowerStationList",
            json={"token": token, "page_size": 100, "cur_page": 1},
            headers={"token": token}, timeout=15
        )
        stations = r.json().get("result_data", {}).get("pageList", [])
        return {"ok": True, "sites": [
            {
                "id":     str(s["ps_id"]),
                "name":   s.get("ps_name", f"Sungrow-{s['ps_id']}"),
                "brand":  "sungrow",
                "kw":     round(float(s.get("curr_power", 0)) / 1000, 2),
                "cap":    round(float(s.get("installed_power_map", 0)), 1),
                "status": "ok" if s.get("status") == 1 else "error",
                "err":    s.get("alarm_msg"),
            } for s in stations
        ]}
    except Exception as e:
        log.error(f"Sungrow error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  HUAWEI FUSIONSOLAR
# ══════════════════════════════
_huawei_token = None
_huawei_expires = None

def get_huawei_token():
    global _huawei_token, _huawei_expires
    if _huawei_token and datetime.now() < (_huawei_expires or datetime.min):
        return _huawei_token
    # Force fresh login
    _huawei_token = None
    r = requests.post(
        "https://eu5.fusionsolar.huawei.com/thirdData/login",
        json={"userName": os.getenv("HUAWEI_USER"), "systemCode": os.getenv("HUAWEI_PASS")},
        timeout=10
    )
    data = r.json()
    log.info(f"Huawei login response: {data.get('success')} / {data.get('failCode')}")
    if data.get("success"):
        _huawei_token = r.cookies.get("XSRF-TOKEN")
        _huawei_expires = datetime.now() + timedelta(minutes=30)  # 30 λεπτά για ασφάλεια
        log.info(f"Huawei token refreshed: {_huawei_token[:10] if _huawei_token else 'None'}...")
    return _huawei_token

@app.get("/api/huawei/kpi-debug")
def huawei_kpi_debug():
    """Δείχνει την ακριβή απάντηση του KPI endpoint"""
    try:
        token = get_huawei_token()
        if not token:
            return {"error": "Login failed - check HUAWEI_USER and HUAWEI_PASS"}
        
        headers = {"XSRF-TOKEN": token}

        # Πάρε πρώτα τις εγκαταστάσεις
        r = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
            json={}, headers=headers, timeout=15
        )
        raw = r.json()
        stations = raw.get("data", [])
        
        # Έλεγξε αν data είναι λίστα
        if not isinstance(stations, list):
            return {"error": "data is not a list", "raw": raw}
        
        if not stations:
            return {"error": "empty station list", "raw": raw}
            
        codes = [s.get("stationCode","") for s in stations[:3] if isinstance(s, dict)]

        # Τώρα δοκίμασε το KPI
        rp = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationRealKpi",
            json={"stationCodes": ",".join(codes)},
            headers=headers, timeout=15
        )
        kpi_raw = rp.json()
        return {
            "token_ok": bool(token),
            "codes_sent": codes,
            "station_count": len(stations),
            "kpi_response": kpi_raw,
            "status_code": rp.status_code
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}

@app.get("/api/huawei/debug")
def huawei_debug():
    """Δείχνει την ακριβή απάντηση του Huawei API για debugging"""
    try:
        token = get_huawei_token()
        r = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
            json={}, headers={"XSRF-TOKEN": token}, timeout=15
        )
        data = r.json()
        stations = data.get("data", [])
        # Επιστρέφει τα πρώτα 2 για να δούμε τα πεδία
        return {
            "total": len(stations),
            "sample": stations[:2] if stations else [],
            "raw_keys": list(stations[0].keys()) if stations else []
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/huawei/sites")
def huawei_sites():
    try:
        token = get_huawei_token()
        headers = {"XSRF-TOKEN": token}

        # Βήμα 1: Πάρε λίστα εγκαταστάσεων
        r = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
            json={}, headers=headers, timeout=15
        )
        data = r.json()
        stations = data.get("data", [])
        if not isinstance(stations, list):
            stations = []

        # Αν άδειο, δοκίμασε ξανά με νέο token
        if not stations:
            _huawei_token = None
            _huawei_expires = None
            token = get_huawei_token()
            headers = {"XSRF-TOKEN": token}
            r = requests.post(
                "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
                json={}, headers=headers, timeout=15
            )
            data = r.json()
            stations = data.get("data", [])
            if not isinstance(stations, list):
                stations = []

        # Βήμα 2: Πάρε παραγωγή σε πραγματικό χρόνο για όλες μαζί
        codes = [s.get("stationCode","") for s in stations if s.get("stationCode")]
        power_map = {}
        if codes:
            # Huawei δέχεται max 100 codes ανά κλήση
            for i in range(0, len(codes), 100):
                batch = codes[i:i+100]
                rp = requests.post(
                    "https://eu5.fusionsolar.huawei.com/thirdData/getStationRealKpi",
                    json={"stationCodes": ",".join(batch)},
                    headers=headers, timeout=15
                )
                kpi_data = rp.json().get("data", [])
                if isinstance(kpi_data, list):
                    for k in kpi_data:
                        code = k.get("stationCode","")
                        kpi  = k.get("dataItemMap", {})
                        # day_power = παραγωγή σήμερα kWh
                        # month_power = παραγωγή μήνα kWh
                        # real_health_state: 1=ok, 2=warn, 3=error
                        power = kpi.get("day_power") or 0
                        health = kpi.get("real_health_state") or 1
                        power_map[code] = {
                            "kw": round(float(power), 2),
                            "health": int(health),
                            "month_power": round(float(kpi.get("month_power") or 0), 2),
                            "total_power": round(float(kpi.get("total_power") or 0), 2),
                        }

        sites = []
        for s in stations:
            code   = s.get("stationCode","")
            name   = s.get("stationName", f"Huawei-{code}")
            cap    = float(s.get("capacity") or 0)
            info   = power_map.get(code, {})
            kw     = info.get("kw", 0.0)
            health = info.get("health", 1)

            # real_health_state: 1=λειτουργεί, 2=προειδοποίηση, 3=σφάλμα
            if health == 3:
                status = "error"
            elif health == 2:
                status = "warn"
            else:
                status = "ok"

            sites.append({
                "id":          code,
                "name":        name,
                "brand":       "huawei",
                "kw":          kw,
                "cap":         round(cap, 1),
                "status":      status,
                "err":         None if status == "ok" else "Σφάλμα εγκατάστασης",
                "month_power": info.get("month_power", 0),
                "total_power": info.get("total_power", 0),
            })

        return {"ok": True, "sites": sites, "raw_count": len(stations)}
    except Exception as e:
        log.error(f"Huawei error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  GOODWE SEMS
# ══════════════════════════════
_goodwe_jwt = None
_goodwe_uid = None

def get_goodwe_token():
    global _goodwe_jwt, _goodwe_uid
    if _goodwe_jwt:
        return _goodwe_jwt, _goodwe_uid
    r = requests.post(
        "https://www.semsportal.com/api/v1/Common/CrossLogin",
        json={"account": os.getenv("GOODWE_ACCOUNT"), "pwd": os.getenv("GOODWE_TOKEN"),
              "is_local": True, "agreement_agreement": 1},
        headers={"Token": json.dumps({"uid":"","user_id":"","timestamp":"",
                                      "token":"","client":"ios","version":"","language":"en"})},
        timeout=10
    )
    data = r.json()
    if data.get("code") == "100":
        _goodwe_uid = data["data"]["uid"]
        _goodwe_jwt = data["data"]["token"]
    return _goodwe_jwt, _goodwe_uid

def goodwe_header():
    jwt, uid = get_goodwe_token()
    return {"Token": json.dumps({
        "uid": uid, "user_id": uid,
        "timestamp": str(int(time.time())),
        "token": jwt, "client": "ios",
        "version": "v2.1.0", "language": "en"
    })}

@app.get("/api/goodwe/sites")
def goodwe_sites():
    try:
        r = requests.post(
            "https://www.semsportal.com/api/v2/PowerStation/GetMonitorDetailByPowerstationId",
            json={"powerstation_id": "all"}, headers=goodwe_header(), timeout=15
        )
        stations = r.json().get("data", {}).get("powerstation_list", [])
        return {"ok": True, "sites": [
            {
                "id":    str(s["id"]),
                "name":  s.get("name", f"GoodWe-{s['id']}"),
                "brand": "goodwe",
                "kw":    round(float(s.get("pac", 0)) / 1000, 2),
                "cap":   round(float(s.get("capacity", 0)), 1),
                "status":"ok" if int(s.get("status",1))==1 else ("warn" if int(s.get("status",1))==2 else "error"),
                "err":   s.get("alarm_msg"),
            } for s in stations
        ]}
    except Exception as e:
        log.error(f"GoodWe error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  FRONIUS SOLAR API
# ══════════════════════════════
@app.get("/api/fronius/sites")
def fronius_sites():
    try:
        api_key = os.getenv("FRONIUS_API_KEY")
        base    = os.getenv("FRONIUS_BASE_URL", "https://api.solarweb.com/swqapi")
        headers = {"AccessKeyId": os.getenv("FRONIUS_ACCESS_KEY_ID",""),
                   "AccessKeyValue": os.getenv("FRONIUS_ACCESS_KEY_VALUE",""),
                   "Content-Type": "application/json"}
        r = requests.get(f"{base}/pvsystems", headers=headers, timeout=15)
        systems = r.json().get("Data", [])
        sites = []
        for s in systems:
            sid = s.get("PvSystemId")
            # Get live data
            live = requests.get(f"{base}/pvsystems/{sid}/aggsdata/day",
                                headers=headers, timeout=10).json()
            channels = live.get("Data", {}).get("Channels", [])
            power = next((c.get("Value",0) for c in channels if "Power" in c.get("ChannelName","")), 0)
            sites.append({
                "id":    str(sid),
                "name":  s.get("Name", f"Fronius-{sid}"),
                "brand": "fronius",
                "kw":    round(float(power)/1000, 2) if power else 0,
                "cap":   round(float(s.get("PeakPower", 0)), 1),
                "status":"ok" if s.get("IsActive") else "error",
                "err":   None if s.get("IsActive") else "System offline",
            })
        return {"ok": True, "sites": sites}
    except Exception as e:
        log.error(f"Fronius error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  UNIFIED ENDPOINT
# ══════════════════════════════
@app.get("/api/all-sites")
def all_sites():
    """Ένα endpoint που επιστρέφει όλες τις εγκαταστάσεις από όλα τα brands"""
    all_data = []
    for brand_fn in [sungrow_sites, huawei_sites, goodwe_sites, fronius_sites]:
        result = brand_fn()
        if result.get("ok"):
            all_data.extend(result["sites"])
        else:
            log.warning(f"Brand failed: {result.get('error')}")

    # Ανίχνευση σφαλμάτων
    alerts = [
        {
            "site_id":   s["id"],
            "site_name": s["name"],
            "brand":     s["brand"],
            "severity":  "critical" if s["status"] == "error" else "warning",
            "message":   s.get("err") or "Χαμηλή απόδοση",
            "timestamp": datetime.now().isoformat(),
        }
        for s in all_data if s["status"] in ("error", "warn")
    ]

    return {
        "ok":        True,
        "timestamp": datetime.now().isoformat(),
        "sites":     all_data,
        "alerts":    alerts,
        "summary": {
            "total":   len(all_data),
            "ok":      sum(1 for s in all_data if s["status"] == "ok"),
            "errors":  sum(1 for s in all_data if s["status"] == "error"),
            "warnings":sum(1 for s in all_data if s["status"] == "warn"),
            "total_kw":round(sum(s["kw"] for s in all_data), 2),
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
