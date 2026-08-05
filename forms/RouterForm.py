from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, StringField, SubmitField
from wtforms.validators import DataRequired, Optional


class RouterForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired()])
    ip = StringField("IP", validators=[DataRequired()])
    port = IntegerField("Port", validators=[DataRequired()])
    login = StringField("Login", validators=[DataRequired()])
    password = StringField("Password", validators=[DataRequired()])
    icon = StringField("Icon", validators=[Optional()])
    linked_object = StringField("Linked object")
    linked_method = StringField("Linked method", validators=[Optional()])
    poll_log = BooleanField("Poll journal", default=False)
    log_to_file = BooleanField("Write journal to file", default=False)
    submit = SubmitField("Submit")
