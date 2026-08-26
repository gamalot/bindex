from flask import Blueprint

bp = Blueprint("cart", __name__, url_prefix="/cart")

from app.blueprints.cart import routes  # noqa: E402,F401
