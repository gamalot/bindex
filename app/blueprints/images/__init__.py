from flask import Blueprint

bp = Blueprint("images", __name__, url_prefix="/images")

from app.blueprints.images import routes  # noqa: E402,F401
