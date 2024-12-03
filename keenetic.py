from hashlib import md5, sha256
from json import loads

from requests import Session

class ConnectedDevice:
    def __init__(self, dictionary):
        self.__dict__ = {
            key.replace("-", "_"): value for key, value in dictionary.items()
        }

    def __getattr__(self, attr):
        return self.__dict__.get(attr)

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
        return self.__session.get(self.__endpoint + address, params=params)

    def post(self, address, data):
        return self.__session.post(self.__endpoint + address, json=data)

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
   