from app.database import Column, Model, SurrogatePK, db

class Router(SurrogatePK, db.Model):
    __tablename__ = 'keenetic_routers'
    title = Column(db.String(100))
    model = Column(db.String(100))
    ip = Column(db.String(100))
    port = Column(db.Integer())
    login = Column(db.String(100))
    password = Column(db.String(100))
    online = Column(db.Integer())
    linked_object = Column(db.String(100))
    updated = Column(db.DateTime)
