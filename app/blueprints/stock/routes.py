from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.stock import bp
from app.extensions import db
from app.models import Lot, Part, StockTransaction, User, UserSetting

# Movement kinds, in the order they're offered as a filter. Labels come from
# the shared `tx_type` Jinja filter so they can't drift from what the log and
# the part detail page actually render.
TX_TYPES = ("in", "out", "return", "move_in", "move_out", "adjust", "initial")


def _location_total(part_id, location_id):
    return (
        db.session.query(db.func.coalesce(db.func.sum(Lot.quantity_remaining), 0))
        .filter_by(part_id=part_id, location_id=location_id)
        .scalar()
        or 0
    )


def _create_lot(part_id, location_id, qty, unit_price, currency, reason, ref_no, tx_type="in", note=None):
    """A new purchase batch (stock-in, or the increase side of an adjustment), plus its own transaction row."""
    lot = Lot(
        part_id=part_id,
        location_id=location_id,
        quantity_received=qty,
        quantity_remaining=qty,
        unit_price=unit_price,
        currency=currency,
        note=note,
    )
    db.session.add(lot)
    db.session.flush()
    db.session.add(
        StockTransaction(
            part_id=part_id,
            location_id=location_id,
            lot_id=lot.id,
            type=tx_type,
            quantity_delta=qty,
            balance_after=_location_total(part_id, location_id),
            unit_price=unit_price,
            currency=currency,
            reason=reason or None,
            ref_no=ref_no or None,
            operator_id=current_user.id,
        )
    )
    return lot


def _consume_lot(lot_id, qty_needed, tx_type, reason, ref_no=None):
    """Take `qty_needed` out of one specific, user-chosen batch."""
    lot = Lot.query.get(lot_id)
    if lot is None:
        raise ValueError("Selected batch not found")
    if qty_needed > lot.quantity_remaining:
        raise ValueError(
            f"Insufficient stock in that batch: need {qty_needed}, only {lot.quantity_remaining} available"
        )

    lot.quantity_remaining -= qty_needed
    db.session.flush()
    db.session.add(
        StockTransaction(
            part_id=lot.part_id,
            location_id=lot.location_id,
            lot_id=lot.id,
            type=tx_type,
            quantity_delta=-qty_needed,
            balance_after=_location_total(lot.part_id, lot.location_id),
            unit_price=lot.unit_price,
            currency=lot.currency,
            reason=reason or None,
            ref_no=ref_no or None,
            operator_id=current_user.id,
        )
    )
    return lot


@bp.route("/<int:part_id>/in", methods=["POST"])
@login_required
def stock_in(part_id):
    part = Part.query.get_or_404(part_id)
    location_id = request.form.get("location_id", type=int)
    qty = request.form.get("quantity", type=int)
    reason = request.form.get("reason", "").strip()
    ref_no = request.form.get("ref_no", "").strip()
    price_raw = request.form.get("unit_price", "").strip()
    unit_price = float(price_raw) if price_raw else None
    currency = request.form.get("currency", "").strip() or part.currency

    if not location_id or not qty or qty <= 0:
        flash("Please provide a valid location and quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    _create_lot(part_id, location_id, qty, unit_price, currency, reason, ref_no)
    db.session.commit()
    flash(f"Stock-in successful: +{qty} (new batch)", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/<int:part_id>/out", methods=["POST"])
@login_required
def stock_out(part_id):
    lot_id = request.form.get("lot_id", type=int)
    qty = request.form.get("quantity", type=int)
    reason = request.form.get("reason", "").strip()
    ref_no = request.form.get("ref_no", "").strip()

    if not lot_id or not qty or qty <= 0:
        flash("Please select a batch and a valid quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot = Lot.query.get(lot_id)
    if lot is None or lot.part_id != part_id:
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    try:
        _consume_lot(lot_id, qty, "out", reason, ref_no)
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    db.session.commit()
    flash(f"Stock-out successful: -{qty}", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/<int:part_id>/return", methods=["POST"])
@login_required
def stock_return(part_id):
    """Put some quantity back into the exact batch it was originally taken
    from — e.g. you took out more than you ended up using."""
    lot_id = request.form.get("lot_id", type=int)
    qty = request.form.get("quantity", type=int)
    reason = request.form.get("reason", "").strip()

    if not lot_id or not qty or qty <= 0:
        flash("Please select a batch and a valid quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot = Lot.query.get(lot_id)
    if lot is None or lot.part_id != part_id:
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    returnable = lot.quantity_received - lot.quantity_remaining
    if qty > returnable:
        flash(
            f"Can't return more than was taken out of that batch: {returnable} outstanding, {qty} requested",
            "danger",
        )
        return redirect(url_for("parts.detail", part_id=part_id))

    lot.quantity_remaining += qty
    db.session.flush()
    db.session.add(
        StockTransaction(
            part_id=part_id,
            location_id=lot.location_id,
            lot_id=lot.id,
            type="return",
            quantity_delta=qty,
            balance_after=_location_total(part_id, lot.location_id),
            unit_price=lot.unit_price,
            currency=lot.currency,
            reason=reason or None,
            operator_id=current_user.id,
        )
    )
    db.session.commit()
    flash(f"Return successful: +{qty} back into that batch", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/<int:part_id>/move", methods=["POST"])
@login_required
def stock_move(part_id):
    lot_id = request.form.get("lot_id", type=int)
    to_location_id = request.form.get("to_location_id", type=int)
    qty = request.form.get("quantity", type=int)
    reason = request.form.get("reason", "").strip()

    if not lot_id or not to_location_id or not qty or qty <= 0:
        flash("Please select a source batch, a destination location, and a valid quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot = Lot.query.get(lot_id)
    if lot is None or lot.part_id != part_id:
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))
    if lot.location_id == to_location_id:
        flash("Source and destination location cannot be the same", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    source_location_name = lot.location.path_name()
    try:
        _consume_lot(lot_id, qty, "move_out", reason)
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    # The moved quantity becomes its own batch at the destination, keeping the
    # original price/currency/received date — it's the same physical stock,
    # just relocated.
    new_lot = Lot(
        part_id=part_id,
        location_id=to_location_id,
        quantity_received=qty,
        quantity_remaining=qty,
        unit_price=lot.unit_price,
        currency=lot.currency,
        received_at=lot.received_at,
        note=f"Moved from {source_location_name}",
    )
    db.session.add(new_lot)
    db.session.flush()
    db.session.add(
        StockTransaction(
            part_id=part_id,
            location_id=to_location_id,
            lot_id=new_lot.id,
            type="move_in",
            quantity_delta=qty,
            balance_after=_location_total(part_id, to_location_id),
            unit_price=lot.unit_price,
            currency=lot.currency,
            reason=reason or None,
            operator_id=current_user.id,
        )
    )

    db.session.commit()
    flash(f"Move successful: {qty} unit(s)", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/<int:part_id>/adjust", methods=["POST"])
@login_required
def stock_adjust(part_id):
    """Correct the recorded remaining quantity of one specific batch.

    To log stock that has no existing batch at all (a fresh find with no
    purchase history), use Stock In instead — its price field is optional,
    so it works fine for "unknown cost" arrivals too.
    """
    lot_id = request.form.get("lot_id", type=int)
    new_qty = request.form.get("new_quantity", type=int)
    reason = request.form.get("reason", "").strip() or "Stock count adjustment"

    if not lot_id or new_qty is None or new_qty < 0:
        flash("Please select a batch and a valid quantity", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot = Lot.query.get(lot_id)
    if lot is None or lot.part_id != part_id:
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    delta = new_qty - lot.quantity_remaining
    if delta == 0:
        flash("No change — that batch already shows that quantity", "info")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot.quantity_remaining = new_qty
    db.session.flush()
    db.session.add(
        StockTransaction(
            part_id=part_id,
            location_id=lot.location_id,
            lot_id=lot.id,
            type="adjust",
            quantity_delta=delta,
            balance_after=_location_total(part_id, lot.location_id),
            unit_price=lot.unit_price,
            currency=lot.currency,
            reason=reason,
            operator_id=current_user.id,
        )
    )
    db.session.commit()
    flash(f"Batch adjusted to {new_qty}", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/<int:part_id>/lots/<int:lot_id>/price", methods=["POST"])
@login_required
def edit_lot_price(part_id, lot_id):
    """Correct a batch's recorded price/currency — for fixing data-entry
    mistakes (e.g. a wrong currency at import time), not for logging a
    genuine price change (that's just what Stock In records on the next
    purchase). Also rewrites any transactions tied to this batch so the
    stock log doesn't keep showing the old, wrong value.
    """
    lot = Lot.query.get(lot_id)
    if lot is None or lot.part_id != part_id:
        flash("Selected batch not found", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    price_raw = request.form.get("unit_price", "").strip()
    currency = request.form.get("currency", "").strip()
    if not price_raw or not currency:
        flash("Please provide both a price and a currency", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    try:
        new_price = float(price_raw)
    except ValueError:
        flash("Invalid price", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))
    if new_price < 0:
        flash("Price can't be negative", "danger")
        return redirect(url_for("parts.detail", part_id=part_id))

    old_price, old_currency = lot.unit_price, lot.currency
    if old_price == new_price and old_currency == currency:
        flash("No change — that batch already has that price", "info")
        return redirect(url_for("parts.detail", part_id=part_id))

    lot.unit_price = new_price
    lot.currency = currency
    for t in StockTransaction.query.filter_by(lot_id=lot.id).all():
        t.unit_price = new_price
        t.currency = currency
    db.session.commit()
    flash(f"Batch price corrected: {old_price} {old_currency} → {new_price} {currency}", "success")
    return redirect(url_for("parts.detail", part_id=part_id))


@bp.route("/transactions")
@login_required
def transactions():
    """The stock log, filterable by part / movement type / operator.

    It used to be an unfiltered paginated dump, which made it the one list
    page you couldn't ask a question of — and it's the page you arrive at
    *with* a question ("when did that batch go out, and who took it").
    """
    q = request.args.get("q", "").strip()
    tx_type = request.args.get("type", "").strip()
    operator_id = request.args.get("operator_id", type=int)

    query = StockTransaction.query
    if q:
        like = f"%{q}%"
        query = query.join(StockTransaction.part).filter(
            db.or_(Part.mpn.ilike(like), Part.marking.ilike(like))
        )
    if tx_type in TX_TYPES:
        query = query.filter(StockTransaction.type == tx_type)
    if operator_id:
        query = query.filter(StockTransaction.operator_id == operator_id)

    page = max(request.args.get("page", 1, type=int), 1)
    page_size = request.args.get("page_size", type=int)
    if page_size not in UserSetting.DOC_PAGE_SIZE_OPTIONS:
        page_size = 50
    pagination = query.order_by(StockTransaction.created_at.desc()).paginate(
        page=page, per_page=page_size, error_out=False
    )

    return render_template(
        "stock/transactions.html",
        pagination=pagination,
        q=q,
        tx_type=tx_type,
        operator_id=operator_id,
        tx_types=TX_TYPES,
        operators=User.query.order_by(User.username).all(),
        page_size=page_size,
        page_size_options=UserSetting.DOC_PAGE_SIZE_OPTIONS,
    )
