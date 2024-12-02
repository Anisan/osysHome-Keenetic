from flask_wtf import  FlaskForm
from wtforms import StringField, SubmitField, IntegerField, DateTimeLocalField
from wtforms.validators import DataRequired, Optional


class RouterForm(FlaskForm):
    title = StringField('Title', validators=[DataRequired()])
    ip = StringField('IP', validators=[DataRequired()])
    port = IntegerField('Port', validators=[DataRequired()])
    login = StringField('Login',validators=[DataRequired()])
    password = StringField('Password',validators=[DataRequired()])
    linked_object = StringField('Linked object')
    submit = SubmitField('Submit')
