from flask import Blueprint

bp = Blueprint("parts", __name__, url_prefix="/parts")

from app.blueprints.parts import routes  # noqa: E402,F401
