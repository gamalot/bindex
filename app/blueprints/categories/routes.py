from flask import jsonify, redirect, render_template, request, url_for, flash
from flask_login import login_required

from app.blueprints.categories import bp
from app.extensions import db
from app.models import AttributeDef, Category, CategoryAttributeOrder
from app.utils import admin_required


@bp.route("/")
@login_required
def index():
    roots = Category.query.filter_by(parent_id=None).order_by(Category.sort_order).all()
    return render_template("categories/index.html", roots=roots)


@bp.route("/create", methods=["POST"])
@login_required
@admin_required
def create():
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id") or None
    if not name:
        flash("Category name cannot be empty", "danger")
        return redirect(url_for("categories.index"))

    siblings_count = Category.query.filter_by(parent_id=parent_id).count()
    node = Category(name=name, parent_id=parent_id, sort_order=siblings_count)
    db.session.add(node)
    db.session.commit()
    flash(f"Category '{name}' created", "success")
    return redirect(url_for("categories.index"))


@bp.route("/<int:cat_id>/update", methods=["POST"])
@login_required
@admin_required
def update(cat_id):
    node = Category.query.get_or_404(cat_id)
    node.name = request.form.get("name", node.name).strip()
    db.session.commit()
    flash("Category updated", "success")
    return redirect(url_for("categories.index"))


@bp.route("/<int:cat_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(cat_id):
    node = Category.query.get_or_404(cat_id)
    if node.children:
        flash("Remove or move out its sub-categories first", "danger")
        return redirect(url_for("categories.index"))
    if node.parts:
        flash("This category still has parts assigned, cannot delete", "danger")
        return redirect(url_for("categories.index"))
    db.session.delete(node)
    db.session.commit()
    flash("Category deleted", "success")
    return redirect(url_for("categories.index"))


@bp.route("/reorder", methods=["POST"])
@login_required
@admin_required
def reorder():
    """Drag-and-drop reorder callback. payload: {moves: [{id, parent_id, sort_order}, ...]}"""
    payload = request.get_json(silent=True) or {}
    moves = payload.get("moves", [])
    for m in moves:
        node = Category.query.get(m.get("id"))
        if not node:
            continue
        node.parent_id = m.get("parent_id")
        node.sort_order = m.get("sort_order", 0)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/<int:cat_id>/attributes", methods=["GET", "POST"])
@login_required
@admin_required
def attributes(cat_id):
    node = Category.query.get_or_404(cat_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "").strip()
        data_type = request.form.get("data_type", "text")
        enum_options = request.form.get("enum_options", "").strip()
        searchable = request.form.get("searchable") == "on"
        if name:
            order = len(node.attribute_defs)
            db.session.add(
                AttributeDef(
                    category_id=node.id,
                    name=name,
                    unit=unit or None,
                    data_type=data_type,
                    enum_options=enum_options or None,
                    sort_order=order,
                    searchable=searchable,
                )
            )
            db.session.commit()
            flash(f"Added attribute '{name}' to '{node.name}'", "success")
        return redirect(url_for("categories.attributes", cat_id=cat_id))

    return render_template("categories/attributes.html", node=node)


@bp.route("/<int:cat_id>/attributes/reorder", methods=["POST"])
@login_required
@admin_required
def reorder_attributes(cat_id):
    """Drag-and-drop reorder callback for one category's full effective
    attribute list (its own attributes plus everything inherited from
    ancestors). payload: {order: [attr_id, ...]} — the complete list
    currently shown, in the desired order. Saved as a per-category
    CategoryAttributeOrder override so repositioning an inherited attribute
    here doesn't affect any other category that inherits the same one; see
    Category.effective_attribute_defs."""
    node = Category.query.get_or_404(cat_id)
    payload = request.get_json(silent=True) or {}
    order = payload.get("order", [])
    valid_ids = {ad.id for ad in node.all_attribute_defs}
    existing = {o.attribute_def_id: o for o in node.attribute_order_overrides}

    for index, attr_id in enumerate(order):
        if attr_id not in valid_ids:
            continue
        row = existing.get(attr_id)
        if row is None:
            # Upsert rather than delete-then-reinsert: a row may already
            # exist here just to carry this category's searchable_override
            # for an inherited attribute, and a full delete would wipe that
            # out every time the list gets dragged.
            row = CategoryAttributeOrder(category_id=cat_id, attribute_def_id=attr_id)
            db.session.add(row)
        row.position = index
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/attributes/<int:attr_id>/searchable", methods=["POST"])
@login_required
@admin_required
def toggle_attribute_searchable(attr_id):
    """Quick AJAX toggle for the per-attribute 'show in search filters'
    checkbox, so it doesn't require opening the full edit modal."""
    attr = AttributeDef.query.get_or_404(attr_id)
    payload = request.get_json(silent=True) or {}
    attr.searchable = bool(payload.get("searchable"))
    db.session.commit()
    return jsonify({"ok": True, "searchable": attr.searchable})


@bp.route("/<int:cat_id>/attributes/<int:attr_id>/searchable_override", methods=["POST"])
@login_required
@admin_required
def toggle_attribute_searchable_override(cat_id, attr_id):
    """Per-category override of an *inherited* attribute's 'show in search
    filters' flag — lets a subcategory opt it in/out without touching the
    attribute's own global default (which is what the owning category's
    checkbox controls) or affecting any other category, ancestor or
    sibling, that also inherits the same attribute."""
    payload = request.get_json(silent=True) or {}
    value = bool(payload.get("searchable"))
    row = CategoryAttributeOrder.query.filter_by(category_id=cat_id, attribute_def_id=attr_id).first()
    if row is None:
        row = CategoryAttributeOrder(category_id=cat_id, attribute_def_id=attr_id)
        db.session.add(row)
    row.searchable_override = value
    db.session.commit()
    return jsonify({"ok": True, "searchable": value})


@bp.route("/<int:cat_id>/attributes/<int:attr_id>/hidden_override", methods=["POST"])
@login_required
@admin_required
def toggle_attribute_hidden_override(cat_id, attr_id):
    """Per-category 'hide this inherited attribute' toggle — once hidden, it
    drops out of Category.effective_attribute_defs entirely, so it stops
    appearing anywhere for this category's parts (edit/detail page, search
    facets, list Characteristics column). Existing PartAttributeValue rows
    for it are left alone (un-hiding brings them back), and no other
    category that inherits the same attribute is affected."""
    row = CategoryAttributeOrder.query.filter_by(category_id=cat_id, attribute_def_id=attr_id).first()
    if row is None:
        row = CategoryAttributeOrder(category_id=cat_id, attribute_def_id=attr_id)
        db.session.add(row)
    payload = request.get_json(silent=True) or {}
    row.hidden = bool(payload.get("hidden"))
    db.session.commit()
    return jsonify({"ok": True, "hidden": row.hidden})


@bp.route("/attributes/<int:attr_id>/update", methods=["POST"])
@login_required
@admin_required
def update_attribute(attr_id):
    attr = AttributeDef.query.get_or_404(attr_id)
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip()
    data_type = request.form.get("data_type", "text")
    enum_options = request.form.get("enum_options", "").strip()
    if not name:
        flash("Attribute name cannot be empty", "danger")
        return redirect(url_for("categories.attributes", cat_id=attr.category_id))

    attr.name = name
    attr.unit = unit or None
    attr.data_type = data_type
    attr.enum_options = enum_options or None
    attr.searchable = request.form.get("searchable") == "on"

    # Keep existing values' cached numeric field in sync with the (possibly
    # just-changed) data type, so range/facet filtering keeps working.
    if data_type in ("number", "engineering"):
        for v in attr.values:
            try:
                v.value_number = float(v.value_text) if v.value_text is not None else None
            except (TypeError, ValueError):
                v.value_number = None
    else:
        for v in attr.values:
            v.value_number = None

    db.session.commit()
    flash(f"Attribute '{name}' updated", "success")
    return redirect(url_for("categories.attributes", cat_id=attr.category_id))


@bp.route("/attributes/<int:attr_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_attribute(attr_id):
    attr = AttributeDef.query.get_or_404(attr_id)
    cat_id = attr.category_id
    db.session.delete(attr)
    db.session.commit()
    flash("Attribute deleted", "success")
    return redirect(url_for("categories.attributes", cat_id=cat_id))
