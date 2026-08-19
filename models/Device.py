from app.database import Column, SurrogatePK, db


class KeeneticDevice(SurrogatePK, db.Model):
    __tablename__ = "keenetic_devices"
    router_id = Column(db.Integer())
    title = Column(db.String(100))
    ip = Column(db.String(100))
    mac = Column(db.String(100))
    linked_object = Column(db.String(100))
    icon = Column(db.String(100))
    hostname = Column(db.String(100))
    interface = Column(db.String(100))
    ssid = Column(db.String(100))
    ap = Column(db.String(100))
    registered = Column(db.Integer)
    access = Column(db.String(50))
    device_hint = Column(db.String(100))
    sync_live = Column(db.String(255))
