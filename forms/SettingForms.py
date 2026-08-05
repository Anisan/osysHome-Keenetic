from flask_wtf import FlaskForm
from wtforms import IntegerField, SubmitField
from wtforms.validators import DataRequired, NumberRange


class SettingsForm(FlaskForm):
    interval = IntegerField("Update interval", validators=[DataRequired()], default=5)
    firmware_check_interval = IntegerField(
        "Firmware check interval",
        validators=[DataRequired()],
        default=3600,
    )
    journal_buffer_limit = IntegerField(
        "Journal buffer size",
        validators=[DataRequired(), NumberRange(min=10, max=5000)],
        default=200,
    )
    submit = SubmitField("Submit")
