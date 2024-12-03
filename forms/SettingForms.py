from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField
from wtforms.validators import DataRequired

# Определение класса формы
class SettingsForm(FlaskForm):
    interval = IntegerField('Update interval', validators=[DataRequired()], default=5)
    submit = SubmitField('Submit')