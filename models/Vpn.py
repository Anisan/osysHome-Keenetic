from app.database import Column, SurrogatePK, db


class KeeneticVpn(SurrogatePK, db.Model):
    __tablename__ = "keenetic_vpn"
    router_id = Column(db.Integer())
    key = Column(db.String(100))
    role = Column(db.String(20))
    vpn_type = Column(db.String(50))
    title = Column(db.String(100))
    icon = Column(db.String(100))
    linked_object = Column(db.String(100))
    linked_method = Column(db.String(100))
    sync_live = Column(db.String(255))
