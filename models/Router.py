from app.database import Column, SurrogatePK, db


class Router(SurrogatePK, db.Model):
    __tablename__ = "keenetic_routers"
    title = Column(db.String(100))
    model = Column(db.String(100))
    ip = Column(db.String(100))
    port = Column(db.Integer())
    login = Column(db.String(100))
    password = Column(db.String(100))
    linked_object = Column(db.String(100))
    linked_method = Column(db.String(100))
    poll_log = Column(db.Integer())
    poll_vpn = Column(db.Integer())
    log_to_file = Column(db.Integer())
    icon = Column(db.String(100))
    sync_live = Column(db.String(255))
    firmware_version = Column(db.String(100))
