from flask import Blueprint

bp = Blueprint("categories", __name__, url_prefix="/categories")

from app.blueprints.categories import routes  # noqa: E402,F401
