from flask import jsonify, redirect, render_template, request, url_for, flash
from flask_login import login_required

from app.blueprints.locations import bp
from app.extensions import db
from app.models import Location
from app.trees import load_location_tree, location_stats
from app.utils import admin_required


@bp.route("/")
@login_required
def index():
    roots, children = load_location_tree()
    return render_template(
        "locations/index.html",
        roots=roots,
        children=children,
        stats=location_stats(children),
    )


@bp.route("/create", methods=["POST"])
@login_required
@admin_required
def create():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    parent_id = request.form.get("parent_id") or None
    if not name:
        flash("Location name cannot be empty", "danger")
        return redirect(url_for("locations.index"))

    siblings_count = Location.query.filter_by(parent_id=parent_id).count()
    node = Location(name=name, code=code or None, parent_id=parent_id, sort_order=siblings_count)
    db.session.add(node)
    db.session.commit()
    flash(f"Location '{name}' created", "success")
    return redirect(url_for("locations.index"))


@bp.route("/<int:loc_id>/update", methods=["POST"])
@login_required
@admin_required
def update(loc_id):
    node = Location.query.get_or_404(loc_id)
    node.name = request.form.get("name", node.name).strip()
    node.code = request.form.get("code", node.code)
    db.session.commit()
    flash("Location updated", "success")
    return redirect(url_for("locations.index"))


@bp.route("/<int:loc_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(loc_id):
    node = Location.query.get_or_404(loc_id)
    if node.children:
        flash("Remove or move out its sub-locations first", "danger")
        return redirect(url_for("locations.index"))
    if node.lots:
        flash("This location still has stock records, cannot delete", "danger")
        return redirect(url_for("locations.index"))
    db.session.delete(node)
    db.session.commit()
    flash("Location deleted", "success")
    return redirect(url_for("locations.index"))


@bp.route("/reorder", methods=["POST"])
@login_required
@admin_required
def reorder():
    payload = request.get_json(silent=True) or {}
    moves = payload.get("moves", [])
    for m in moves:
        node = Location.query.get(m.get("id"))
        if not node:
            continue
        node.parent_id = m.get("parent_id")
        node.sort_order = m.get("sort_order", 0)
    db.session.commit()
    return jsonify({"ok": True})
