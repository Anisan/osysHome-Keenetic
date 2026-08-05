from app.database import Column, SurrogatePK, db


class KeeneticLogRule(SurrogatePK, db.Model):
    __tablename__ = "keenetic_log_rules"
    router_id = Column(db.Integer())
    title = Column(db.String(100))
    pattern = Column(db.String(255))
    write_to_file = Column(db.Integer())
    linked_object = Column(db.String(100))
    linked_method = Column(db.String(100))
    active = Column(db.Integer())
