from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import Optional


class VpnForm(FlaskForm):
    title = StringField("Title")
    icon = StringField("Icon", validators=[Optional()])
    linked_object = StringField("Linked object")
    linked_method = StringField("Linked method", validators=[Optional()])
    submit = SubmitField("Submit")
