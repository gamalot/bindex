from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.cart import bp
from app.blueprints.stock.routes import _consume_lot
from app.extensions import db
from app.models import CartItem, Lot, Part


def _cart_items():
    return (
        CartItem.query.filter_by(user_id=current_user.id)
        .join(Lot)
        .order_by(Lot.location_id, CartItem.added_at)
        .all()
    )


@bp.route("/")
@login_required
def index():
    return render_template("cart/index.html", items=_cart_items())


@bp.route("/print")
@login_required
def print_list():
    # Grouped for picking: walk the shelf in one pass rather than bouncing
    # around by whatever order things were added to the cart in.
    items = sorted(_cart_items(), key=lambda i: (i.lot.location.path_name(), i.lot.part.mpn))
    return render_template("cart/print.html", items=items)


@bp.route("/add", methods=["POST"])
@login_required
def add():
    part_id = request.form.get("part_id", type=int)
    lot_id = request.form.get("lot_id", type=int)
    qty = request.form.get("quantity", type=int)

    if not lot_id or not qty or qty <= 0:
        flash("Please select a batch and a valid quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot = Lot.query.get(lot_id)
    if lot is None or (part_id and lot.part_id != part_id):
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    item = CartItem.query.filter_by(user_id=current_user.id, lot_id=lot_id).first()
    existing_qty = item.quantity if item else 0
    new_qty = min(existing_qty + qty, lot.quantity_remaining)
    if new_qty <= 0:
        flash("That batch has no stock remaining", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    if item is None:
        item = CartItem(user_id=current_user.id, lot_id=lot_id, quantity=new_qty)
        db.session.add(item)
    else:
        item.quantity = new_qty
    if new_qty < existing_qty + qty:
        flash(f"Only {lot.quantity_remaining} available in that batch — capped the cart quantity", "warning")
    else:
        flash("Added to cart", "success")
    db.session.commit()
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/bulk-add", methods=["POST"])
@login_required
def bulk_add():
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    if not parts:
        flash("Select at least one part to add to the cart", "warning")
        return redirect(url_for("parts.index"))

    added, no_stock, ambiguous = [], [], []
    for p in parts:
        lots = [lot for lot in p.active_lots if lot.quantity_remaining > 0]
        if not lots:
            no_stock.append(p.mpn)
            continue
        if len(lots) > 1:
            ambiguous.append(p.mpn)
            continue
        lot = lots[0]
        item = CartItem.query.filter_by(user_id=current_user.id, lot_id=lot.id).first()
        if item is None:
            db.session.add(CartItem(user_id=current_user.id, lot_id=lot.id, quantity=1))
        else:
            item.quantity = min(item.quantity + 1, lot.quantity_remaining)
        added.append(p.mpn)

    db.session.commit()

    if added:
        flash(f"Added {len(added)} part(s) to the cart", "success")
    if ambiguous:
        flash(
            "These have stock split across more than one batch, so they weren't auto-added — "
            "pick the specific batch on each part's own page: " + ", ".join(ambiguous),
            "warning",
        )
    if no_stock:
        flash("No stock to add for: " + ", ".join(no_stock), "warning")
    return redirect(url_for("parts.index"))


@bp.route("/items/<int:item_id>/update", methods=["POST"])
@login_required
def update_item(item_id):
    item = CartItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
    qty = request.form.get("quantity", type=int)
    if not qty or qty <= 0:
        db.session.delete(item)
        db.session.commit()
        flash("Removed from cart", "success")
        return redirect(url_for("cart.index"))

    if qty > item.lot.quantity_remaining:
        qty = item.lot.quantity_remaining
        flash(f"Only {item.lot.quantity_remaining} available in that batch — capped the quantity", "warning")
    item.quantity = qty
    db.session.commit()
    return redirect(url_for("cart.index"))


@bp.route("/items/<int:item_id>/remove", methods=["POST"])
@login_required
def remove_item(item_id):
    item = CartItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash("Removed from cart", "success")
    return redirect(url_for("cart.index"))


@bp.route("/checkout", methods=["POST"])
@login_required
def checkout():
    items = _cart_items()
    if not items:
        flash("Your cart is empty", "warning")
        return redirect(url_for("cart.index"))

    reason = request.form.get("reason", "").strip()

    # All-or-nothing: re-check every batch still has enough left (something
    # else may have taken stock since this was added to the cart) before
    # touching anything, so a shortfall on one item never leaves the rest
    # half-applied.
    short = [
        f"{item.lot.part.mpn} (need {item.quantity}, only {item.lot.quantity_remaining} left)"
        for item in items
        if item.quantity > item.lot.quantity_remaining
    ]
    if short:
        flash("Not enough stock for: " + "; ".join(short) + " — adjust the quantities and try again", "danger")
        return redirect(url_for("cart.index"))

    try:
        for item in items:
            _consume_lot(item.lot_id, item.quantity, "out", reason)
            db.session.delete(item)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("cart.index"))

    flash(f"Stock-out complete for {len(items)} item(s)", "success")
    return redirect(url_for("stock.transactions"))
