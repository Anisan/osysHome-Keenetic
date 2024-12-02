from flask_wtf import  FlaskForm
from wtforms import StringField, SubmitField, TextAreaField, DateTimeLocalField

class DeviceForm(FlaskForm):
    title = StringField('Title')
    ip = StringField('IP')
    linked_object = StringField('Linked object')
    submit = SubmitField('Submit')
