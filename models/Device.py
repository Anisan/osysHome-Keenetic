from app.database import Column, Model, SurrogatePK, db

class KeeneticDevice(SurrogatePK, db.Model):
    __tablename__ = 'keenetic_devices'
    router_id = Column(db.Integer())
    title = Column(db.String(100))
    ip = Column(db.String(100))
    mac = Column(db.String(100))
    online = Column(db.Integer)
    linked_object = Column(db.String(100))
    updated = Column(db.DateTime)
