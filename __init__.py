"""

"""
import datetime
from flask import redirect, render_template
from sqlalchemy import delete, or_
from app.database import session_scope, row2dict, get_now_to_utc
from app.core.lib.object import updatePropertyThread
from app.core.main.BasePlugin import BasePlugin
from plugins.Keenetic.keenetic import ApiRouter
from plugins.Keenetic.models.Router import Router
from plugins.Keenetic.models.Device import KeeneticDevice
from plugins.Keenetic.forms.RouterForm import RouterForm
from plugins.Keenetic.forms.DeviceForm import DeviceForm
from plugins.Keenetic.forms.SettingForms import SettingsForm


class Keenetic(BasePlugin):

    def __init__(self, app):
        super().__init__(app, __name__)
        self.title = "Keenetic"
        self.description = """Keenetic: get devices info"""
        self.system = True
        self.actions = ['cycle','search','widget']
        self.category = "Devices"
        self.version = "0.1"
        self.routers = {}

    def initialization(self):
        pass

    def admin(self, request):
        op = request.args.get("op", None)
        if op == "delete":
            router_id = int(request.args.get("router", 0))
            device_id = int(request.args.get("device", 0))
            with session_scope() as session:
                if router_id > 0:
                    qry = delete(Router).where(Router.id == router_id)
                if device_id > 0:
                    qry = delete(KeeneticDevice).where(KeeneticDevice.id == device_id)
                session.execute(qry)
                session.commit()
            return redirect("Keenetic")
        elif op == "add":
            form = RouterForm()
            if form.validate_on_submit():
                router = Router()
                form.populate_obj(router)
                with session_scope() as session:
                    session.add(router)
                    session.commit()
                return redirect("Keenetic")
            return self.render("keenetic_router.html", {"form": form})
        elif op == "edit":
            router_id = request.args.get("router", None)
            device_id = request.args.get("device", None)
            if router_id:
                with session_scope() as session:
                    router = session.get(Router, router_id)
                    form = RouterForm(obj=router)
                    if form.validate_on_submit():
                        form.populate_obj(router)
                        session.commit()
                        return redirect("Keenetic")
                    return self.render("keenetic_router.html", {"form": form})
            if device_id:
                with session_scope() as session:
                    device = session.get(KeeneticDevice, device_id)
                    form = DeviceForm(obj=device)
                    if form.validate_on_submit():
                        form.populate_obj(device)
                        session.commit()
                        return redirect("?router=" + str(device.router_id))
                    return self.render("keenetic_device.html", {"form": form, "router_id": device.router_id})

        router_id = request.args.get("router", None)
        if router_id:
            router = Router.query.filter(Router.id == router_id).one_or_404()
            devices = KeeneticDevice.query.filter(KeeneticDevice.router_id == router_id).all()
            devices = [row2dict(device) for device in devices]
            content = {
                "router": router,
                "devices": devices,
            }
            return self.render("keenetic_devices.html", content)

        settings = SettingsForm()
        if request.method == 'GET':
            settings.interval.data = self.config.get('interval',5)
        else:
            if settings.validate_on_submit():
                self.config["interval"] = settings.interval.data
                self.saveConfig()
                return redirect("Keenetic")
            
        routers = Router.query.all()
        routers = [row2dict(router) for router in routers]
        content = {
            "routers": routers,
            "form": settings,
        }
        return self.render("keenetic_main.html", content)

    def search(self, query: str) -> list:
        res = []
        routers = Router.query.filter(or_(Router.title.contains(query),Router.ip.contains(query),Router.linked_object.contains(query))).all()
        for router in routers:
            res.append({"url":f'Keenetic?op=edit&router={router.id}', "title": f'Router: {router.title}', "tags": [{"name":"Keenetic","color":"info"}]})
        devices = KeeneticDevice.query.filter(or_(KeeneticDevice.title.contains(query),KeeneticDevice.linked_object.contains(query))).all()
        for device in devices:
            res.append({"url":f'Keenetic?op=edit&device={device.id}', "title":f'Device: {device.title}', "tags":[{"name":"Keenetic","color":"warning"}]})
        return res

    def widget(self):
        content = {}
        with session_scope() as session:
            content['routers'] = session.query(Router).count()
            content['devices'] = session.query(KeeneticDevice).count()
        return render_template("widget_keenetic.html",**content)

    def cyclic_task(self):
        with session_scope() as session:
            routers = session.query(Router).all()
            for router in routers:
                ip = router.ip
                if ip not in self.routers:
                    port = router.port
                    if not port:
                        port = 80
                    self.routers[ip] = ApiRouter(router.login, router.password, ip, port)
                if not self.routers[ip].isAuth:
                    self.routers[ip].auth()
                info = self.routers[ip].info
                if info:
                    router.model = info['show']['version']['model']
                    router.online = 1
                    router.updated = get_now_to_utc()
                    try:
                        inet = session.query(KeeneticDevice).filter(KeeneticDevice.router_id == router.id, KeeneticDevice.mac == '0.0.0.0.0.0', KeeneticDevice.title == "Internet").one_or_none()
                        if not inet:
                            inet = KeeneticDevice(router_id=router.id, mac="0.0.0.0.0.0", title='Internet')
                            session.add(inet)
                        inet.updated = get_now_to_utc()
                        inet.online = 1 if info['show']['internet']['status']['internet'] else 0
                        if inet.online == 1:
                            interface = info['show']['internet']['status']['gateway']['interface']
                            inet.ip = info['show']['interface'][interface]['address']
                        else:
                            inet.ip = ""
                        if inet.linked_object:
                            updatePropertyThread(inet.linked_object + ".ip",inet.ip, self.name)
                            updatePropertyThread(inet.linked_object + ".online",inet.online, self.name)
                    except Exception as ex:
                        self.logger.error("Error get status internet",ex)
                else:
                    router.online = 0
                    router.updated = get_now_to_utc()
                session.commit()
                if router.linked_object:
                    updatePropertyThread(router.linked_object + ".online", router.online, self.name)
                
                if not self.routers[ip].isAuth:
                    continue

                devs = self.routers[ip].devices
                #self.logger.debug(info, devs)
                for dev in devs:
                    rec = session.query(KeeneticDevice).filter(KeeneticDevice.router_id == router.id, KeeneticDevice.mac == dev.mac).one_or_none()
                    if not rec:
                        rec = session.query(KeeneticDevice).filter(KeeneticDevice.router_id == router.id, KeeneticDevice.title == dev.name).one_or_none()
                        if rec:
                            rec.mac = dev.mac
                        else:
                            rec = KeeneticDevice(router_id=router.id, mac=dev.mac)
                            session.add(rec)
                    rec.updated = get_now_to_utc()
                    rec.ip = dev.ip
                    rec.title = dev.name
                    rec.online = 1 if dev.link == 'up' else 0
                    if rec.linked_object:
                        updatePropertyThread(rec.linked_object + ".ip",rec.ip, self.name)
                        updatePropertyThread(rec.linked_object + ".online",rec.online, self.name)
                        #updatePropertyThread(rec.linked_object+".online",rec.online)
                session.commit()

        self.event.wait(float(self.config.get('interval',5.0)))
