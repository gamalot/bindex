from flask import Blueprint

bp = Blueprint("stock", __name__, url_prefix="/stock")

from app.blueprints.stock import routes  # noqa: E402,F401
