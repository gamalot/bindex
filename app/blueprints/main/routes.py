from flask_login import current_user, login_required
from flask import redirect, render_template, url_for

from app.blueprints.main import bp
from app.extensions import db
from app.models import Part, Lot, StockTransaction, Category
from app.utils import home_endpoint


@bp.route("/")
@login_required
def dashboard():
    # This is "/", so a bookmark or the nav brand still lands here even
    # though the Dashboard link is admin-only — send non-admins on to
    # where their nav actually starts rather than showing a page they
    # have no way back to.
    if not current_user.is_admin:
        return redirect(url_for(home_endpoint()))

    total_parts = Part.query.count()
    total_qty = db.session.execute(
        db.select(db.func.coalesce(db.func.sum(Lot.quantity_remaining), 0))
    ).scalar() or 0
    low_stock = []
    for p in Part.query.filter(Part.min_stock_alert.isnot(None)).all():
        if p.total_quantity <= (p.min_stock_alert or 0):
            low_stock.append(p)
    recent_tx = (
        StockTransaction.query.order_by(StockTransaction.created_at.desc()).limit(15).all()
    )
    uncategorized = Category.query.filter_by(name="Uncategorized", parent_id=None).first()
    uncategorized_filter = Part.category_id.is_(None)
    if uncategorized:
        uncategorized_filter = db.or_(uncategorized_filter, Part.category_id == uncategorized.id)
    uncategorized_count = Part.query.filter(uncategorized_filter).count()
    category_count = Category.query.count()

    return render_template(
        "main/dashboard.html",
        total_parts=total_parts,
        total_qty=total_qty,
        low_stock=low_stock,
        recent_tx=recent_tx,
        uncategorized_count=uncategorized_count,
        category_count=category_count,
    )
