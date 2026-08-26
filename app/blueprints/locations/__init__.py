from flask import Blueprint

bp = Blueprint("locations", __name__, url_prefix="/locations")

from app.blueprints.locations import routes  # noqa: E402,F401
