from hashlib import md5, sha256
from json import loads

from requests import Session

from app.configuration import Config

class ConnectedDevice:
    def __init__(self, dictionary):
        self.__dict__ = {
            key.replace("-", "_"): value for key, value in dictionary.items()
        }

    def __getattr__(self, attr):
        return self.__dict__.get(attr)
        
    def __str__(self):
        return f"ConnectedDevice({self.__dict__})"

class ApiRouter:
    def __init__(self, username="admin", password="", host="192.168.1.1", port=80):
        self.__session = Session()
        self.__endpoint = f"http://{host}:{port}"
        if port == 443:
            self.__endpoint = f"https://{host}:{port}"
        self.__username = username
        self.__password = password
        self.isAuth = False
        self.auth()

    def auth(self):
        try:
            response = self.get("/auth")
            if response.status_code == 401:
                realm = response.headers["X-NDM-Realm"]
                password = f"{self.__username}:{realm}:{self.__password}"
                password = md5(password.encode("utf-8"))
                challenge = response.headers["X-NDM-Challenge"]
                password = challenge + password.hexdigest()
                password = sha256(password.encode("utf-8")).hexdigest()
                response = self.post("/auth", {"login": self.__username, "password": password})
                self.isAuth = response.status_code == 200
            else:
                self.isAuth = response.status_code == 200
        except Exception:
            self.isAuth = False
        return self.isAuth

    def get(self, address, params={}):
        return self.__session.get(self.__endpoint + address, params=params, timeout=Config.HTTP_REQUEST_TIMEOUT)

    def post(self, address, data):
        return self.__session.post(self.__endpoint + address, json=data, timeout=Config.HTTP_REQUEST_TIMEOUT)

    @property
    def devices(self):
        try:
            response = self.get("/rci/show/ip/hotspot")
            if response.ok:
                devices = loads(response.text)["host"]
                return list(
                    map(ConnectedDevice, devices)
                )
        except Exception as ex:
            print(ex)
            
        self.isAuth = False
        return []
            
    @property
    def connected_devices(self):
        try:
            response = self.get("/rci/show/ip/hotspot")
            if response.ok:
                devices = loads(response.text)["host"]
                return list(
                    filter(lambda device: device.active, map(ConnectedDevice, devices))
                )
        except Exception as ex:
            print(ex)
        self.isAuth = False
        return []

    @property
    def info(self):
        try:
            params = {"show": {"system": {}, "version": {}, "identification": {}, "ip":{"hotspot":{}}, "internet":{"status":{}}, "interface": {}}}
            response = self.post("/rci/", params)
            if response.ok:
                info = loads(response.text)
                return info
        except Exception as ex:
            print(ex)
            self.isAuth = False
        return None

    def interface_stat(self, interface_name):
        try:
            params = [{"show": {"interface": {"stat": [{"name": interface_name}]}}}]
            response = self.post("/rci/", params)
            if response.ok:
                data = loads(response.text)
                if isinstance(data, list):
                    data = data[0] if data else {}
                stat = data.get("show", {}).get("interface", {}).get("stat")
                if isinstance(stat, list) and len(stat) > 0 and isinstance(stat[0], dict):
                    return stat[0]
                if isinstance(stat, dict):
                    return stat
        except Exception as ex:
            print(ex)
            self.isAuth = False
        return {}

    def system_resources(self):
        try:
            params = {"show": {"system": {}}}
            response = self.post("/rci/", params)
            if response.ok:
                data = loads(response.text)
                if isinstance(data, list):
                    data = data[0] if data else {}
                system_data = data.get("show", {}).get("system", {})
                cpu_value = system_data.get("cpuload")
                if isinstance(cpu_value, str):
                    try:
                        cpu_value = int(float(cpu_value))
                    except ValueError:
                        cpu_value = None
                elif isinstance(cpu_value, (int, float)):
                    cpu_value = int(cpu_value)
                else:
                    cpu_value = None

                ram_value = None
                memory_value = system_data.get("memory")
                if isinstance(memory_value, str) and "/" in memory_value:
                    used_value, total_value = memory_value.split("/", 1)
                    try:
                        used_value = int(used_value)
                        total_value = int(total_value)
                        if total_value > 0:
                            ram_value = int((used_value * 100) / total_value)
                    except ValueError:
                        ram_value = None
                elif isinstance(memory_value, (int, float)):
                    ram_value = int(memory_value)
                uptime_value = system_data.get("uptime")
                if isinstance(uptime_value, str):
                    try:
                        uptime_value = int(float(uptime_value))
                    except ValueError:
                        uptime_value = None
                return {
                    "cpu": cpu_value,
                    "ram": ram_value,
                    "uptime": uptime_value,
                }
        except Exception as ex:
            print(ex)
            self.isAuth = False
        return {"cpu": None, "ram": None, "uptime": None}
   