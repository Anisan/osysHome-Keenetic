from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, SubmitField
from wtforms.validators import DataRequired, Optional


class LogRuleForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired()])
    pattern = StringField("Pattern (regexp)", validators=[DataRequired()])
    write_to_file = BooleanField("Write to journal file", default=False)
    linked_object = StringField("Linked object", validators=[Optional()])
    linked_method = StringField("Linked method", validators=[Optional()])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Submit")
