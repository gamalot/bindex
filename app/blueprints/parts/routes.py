import os
import time
import uuid

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from PIL import Image as PILImage
from sqlalchemy.orm import joinedload, selectinload

from app.blueprints.parts import bp
from app.extensions import db
from app.models import (
    Category,
    Document,
    Location,
    Lot,
    Part,
    PartAttributeValue,
    Photo,
    PartPhoto,
    UserSetting,
)
from app.exchange_rates import ensure_fresh_rates, get_rate_map
from app.lcsc import LCSC_ERRORS, download_lcsc_image, product_images, search_exact, search_suggestions
from app.smart_crop import suggest_crop_box
from app.trees import flatten_tree_from, load_category_tree, load_location_tree
from app.utils import (
    DEFAULT_SEARCH_ENGINE,
    SEARCH_ENGINES,
    admin_required,
    collect_descendant_ids,
    format_engineering,
    format_number,
    format_number_commas,
    make_thumbnail,
    save_upload,
)


# ---------------------------------------------------------------------------
# List / combined search (exact + fuzzy)
# ---------------------------------------------------------------------------
SORT_FIELDS = {
    "mpn": lambda p: (p.mpn or "").lower(),
    "location": lambda p: (p.location_codes[0].lower() if p.location_codes else ""),
    "manufacturer": lambda p: (p.manufacturer or "").lower(),
    "package": lambda p: (p.package or "").lower(),
    "marking": lambda p: (p.marking or "").lower(),
    "category": lambda p: (p.category.name.lower() if p.category else ""),
    "qty": lambda p: p.total_quantity,
    "price": lambda p: p.default_unit_price,
    "updated": lambda p: p.updated_at or p.created_at,
}
DEFAULT_SORT_FIELD = "updated"
DEFAULT_SORT_DIR = "desc"
PAGE_SIZE_OPTIONS = (25, 50, 100, 200, 500, 1000)


def _sort_parts(parts, sort):
    field, _, direction = sort.rpartition("_")
    if field not in SORT_FIELDS:
        field, direction = DEFAULT_SORT_FIELD, DEFAULT_SORT_DIR
    reverse = direction != "asc"

    if field == "price":
        # Prices are in mixed currencies and aren't directly comparable, so sorting
        # converts each to AUD first (only for ordering — the display is untouched).
        # Rates are refreshed at most once a day; a failed refresh just keeps
        # whatever was cached before, so this never blocks the page on network issues.
        ensure_fresh_rates(logger=current_app.logger)
        rates = get_rate_map()

        def to_aud(p):
            amount, currency = p.display_price
            if amount is None:
                return None
            currency = currency or "AUD"
            rate = 1.0 if currency == "AUD" else rates.get(currency)
            return None if rate is None else amount * rate

        # Parts with no price, or a currency we have no rate for, always sort last.
        priced = [(to_aud(p), p) for p in parts]
        unpriced = [p for v, p in priced if v is None]
        priced = [(v, p) for v, p in priced if v is not None]
        priced.sort(key=lambda t: t[0], reverse=reverse)
        parts = [p for _, p in priced] + unpriced
    else:
        key_func = SORT_FIELDS[field]
        parts = sorted(parts, key=key_func, reverse=reverse)

    return parts, field, direction


def _preload_list_relations(page_items):
    """Fetch everything the list template reads off each row, in one go.

    Each row touches the part's lots (stock, locations, price), its photos
    and its attribute values — all lazy, so rendering cost three queries
    per row. That's tolerable at 25 rows and not at 500: a 669-row page
    was ~2400 queries.

    It runs on the page slice rather than as options() on the main query
    because the list is sorted and paginated in Python (some sort keys are
    computed properties), so the main query has no idea which rows will be
    shown — eager-loading there would pull relations for every match just
    to display a handful. These are the same identity-mapped objects, so
    loading them here populates the rows the template is about to render.
    """
    if not page_items:
        return
    Part.query.options(
        selectinload(Part.lots).joinedload(Lot.location),
        selectinload(Part.photo_links).joinedload(PartPhoto.photo),
        selectinload(Part.attribute_values),
        joinedload(Part.category),
    ).filter(Part.id.in_([p.id for p in page_items])).all()


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    exact = request.args.get("exact") == "1"
    category_id = request.args.get("category_id", type=int)
    include_subcategories = request.args.get("include_subcategories", "1") != "0"
    location_id = request.args.get("location_id", type=int)
    # Not user-selectable per search — always the personal-settings default,
    # see Settings > Default currency for price-range search.
    currency = (current_user.setting.default_price_currency if current_user.setting else None) or "AUD"
    in_stock_only = request.args.get("in_stock") == "1"
    price_min = request.args.get("price_min", type=float)
    price_max = request.args.get("price_max", type=float)
    sort = request.args.get("sort", "updated_desc")
    page = max(request.args.get("page", 1, type=int), 1)

    page_size_param = request.args.get("page_size", type=int)
    default_page_size = (current_user.setting.page_size if current_user.setting else None) or current_app.config[
        "DEFAULT_PAGE_SIZE"
    ]
    page_size = page_size_param if page_size_param in PAGE_SIZE_OPTIONS else default_page_size

    query = Part.query

    if q:
        if exact:
            query = query.filter(
                db.or_(Part.mpn == q, Part.marking == q, Part.manufacturer == q)
            )
        else:
            like = f"%{q}%"
            query = query.filter(
                db.or_(
                    Part.mpn.ilike(like),
                    Part.marking.ilike(like),
                    Part.manufacturer.ilike(like),
                    Part.description.ilike(like),
                )
            )

    if category_id:
        cat = Category.query.get(category_id)
        if cat:
            if include_subcategories:
                query = query.filter(Part.category_id.in_(collect_descendant_ids(cat)))
            else:
                query = query.filter(Part.category_id == cat.id)

    if location_id:
        loc = Location.query.get(location_id)
        if loc:
            loc_ids = collect_descendant_ids(loc)
            query = query.join(Part.lots).filter(
                Lot.location_id.in_(loc_ids), Lot.quantity_remaining > 0
            )

    # Dynamic parametric attribute filters (only shown when a category is selected).
    # Number-type attributes (e.g. resistance) get a DigiKey-style multi-select of
    # the values that actually occur in the current result set (attr_<id>_values,
    # repeated) instead of a free-form min/max range; other types stay a substring
    # text match (attr_<id>).
    attr_defs = []
    if category_id:
        cat = Category.query.get(category_id)
        if cat:
            attr_defs = [ad for ad in cat.effective_attribute_defs if cat.is_attr_searchable(ad)]

    parts = query.distinct().all()

    # Unit-price range filter: every part's price (see Part.display_price —
    # the latest-lot price, falling back to the manual reference price) is
    # converted into the selected `currency` for comparison, rather than
    # requiring the part to already be priced in that currency. A part whose
    # native currency has no cached exchange rate can't be converted, so it's
    # excluded (consistent with how price-sorting also drops unrateable
    # parts to the end rather than guessing).
    if price_min is not None or price_max is not None:
        ensure_fresh_rates(logger=current_app.logger)
        rates = get_rate_map()

        def _price_in_currency(p):
            amount, native = p.display_price
            if amount is None:
                return None
            native = native or "AUD"
            if native == "AUD":
                aud = amount
            else:
                rate = rates.get(native)
                if rate is None:
                    return None
                aud = amount * rate
            if currency == "AUD":
                return aud
            target_rate = rates.get(currency)
            return None if not target_rate else aud / target_rate

        kept = []
        for p in parts:
            conv = _price_in_currency(p)
            if conv is None:
                continue
            if price_min is not None and conv < price_min:
                continue
            if price_max is not None and conv > price_max:
                continue
            kept.append(p)
        parts = kept

    if in_stock_only:
        parts = [p for p in parts if p.total_quantity != 0]

    # Manufacturer, Package, and every parametric attribute are DigiKey-style
    # faceted multi-selects — but each facet's *option list* has to reflect
    # every OTHER active filter, not just the ones that happened to run
    # before it. Narrowing `parts` sequentially (facet A narrows the pool,
    # then facet B's options are built from what's left) means a filter
    # applied later never narrows an earlier facet's displayed options —
    # e.g. picking T.C.=C0G/NP0 wouldn't stop Capacitance from still
    # listing values that only exist on X5R/X7R parts. So instead: build
    # every facet's selected-set up front, then for each facet compute its
    # options from `parts` filtered by every *other* facet ("leave one
    # out"), and only apply every facet together to get the final result set.
    #
    # A facet's raw per-part value is a single comparable string — for
    # number/engineering attributes that's format_number(value_number), the
    # exact same string used as each option's value=, so matching is plain
    # string equality against the selected set with no float-tolerance
    # dance needed.
    facet_specs = [
        {"key": "manufacturer", "param": "manufacturer_values", "get_raw": lambda p: p.manufacturer},
        {"key": "package", "param": "package_values", "get_raw": lambda p: p.package},
    ]
    for ad in attr_defs:
        if ad.data_type in ("number", "engineering"):
            def get_raw(p, ad=ad):
                v = next(
                    (v for v in p.attribute_values if v.attribute_def_id == ad.id and v.value_number is not None),
                    None,
                )
                return format_number(v.value_number) if v is not None else None
        else:
            def get_raw(p, ad=ad):
                return next(
                    (v.value_text for v in p.attribute_values if v.attribute_def_id == ad.id and v.value_text),
                    None,
                )
        facet_specs.append({"key": f"attr_{ad.id}", "param": f"attr_{ad.id}_values", "get_raw": get_raw, "ad": ad})

    for f in facet_specs:
        f["selected"] = set(request.args.getlist(f["param"]))

    def _apply_facets(parts_list, skip_key=None):
        for f in facet_specs:
            if f["key"] == skip_key or not f["selected"]:
                continue
            parts_list = [p for p in parts_list if f["get_raw"](p) in f["selected"]]
        return parts_list

    manufacturer_spec, package_spec = facet_specs[0], facet_specs[1]
    manufacturer_options = sorted(
        {p.manufacturer for p in _apply_facets(parts, skip_key="manufacturer") if p.manufacturer}
    )
    selected_manufacturer_values = sorted(manufacturer_spec["selected"])
    package_options = sorted({p.package for p in _apply_facets(parts, skip_key="package") if p.package})
    selected_package_values = sorted(package_spec["selected"])

    attr_value_options = {}
    selected_attr_values = {}
    for f in facet_specs[2:]:
        ad = f["ad"]
        scoped = _apply_facets(parts, skip_key=f["key"])
        if ad.data_type in ("number", "engineering"):
            values = sorted(
                {
                    v.value_number
                    for p in scoped
                    for v in p.attribute_values
                    if v.attribute_def_id == ad.id and v.value_number is not None
                }
            )
            # The option's value= (used for the URL/round-trip and the
            # selected-state check below) always stays the plain form; only
            # the visible label changes with the attribute's display style.
            display_fmt = format_engineering if ad.data_type == "engineering" else format_number_commas
            attr_value_options[ad.id] = [(format_number(v), display_fmt(v, ad.unit or "")) for v in values]
        else:
            values = sorted(
                {
                    v.value_text
                    for p in scoped
                    for v in p.attribute_values
                    if v.attribute_def_id == ad.id and v.value_text
                }
            )
            attr_value_options[ad.id] = [(val, val + (f" {ad.unit}" if ad.unit else "")) for val in values]
        selected_attr_values[ad.id] = sorted(f["selected"])

    parts = _apply_facets(parts)

    parts, sort_field, sort_dir = _sort_parts(parts, sort)

    total = len(parts)
    start = (page - 1) * page_size
    page_items = parts[start : start + page_size]
    total_pages = max((total + page_size - 1) // page_size, 1)
    _preload_list_relations(page_items)

    # Hide a classification column, or an individual attribute's piece
    # within Characteristics, only when *you* made it redundant — i.e. you
    # narrowed that specific field down to exactly one value through its
    # own control (the category dropdown, or a facet's multi-select).
    # Deliberately NOT triggered by values merely happening to coincide
    # (e.g. every part in Ferrite Beads happens to be 100MHz, but you never
    # touched the Frequency facet) — that coincidence might be exactly what
    # you're looking at the list to notice, so it stays visible. Selecting
    # more than one value in a facet also keeps its column, same as before —
    # you're still comparing between them, not filtering it down to one.
    def _all_same(get_value):
        return bool(parts) and len({get_value(p) for p in parts}) == 1

    # Every optional column also has to be turned on in the viewer's own
    # Settings page (Part list columns) — that preference is the master
    # switch; the auto-collapse rules below only narrow further from there.
    visible_columns = current_user.setting.visible_columns if current_user.setting else set(UserSetting.LIST_COLUMNS)

    col_visible = {
        "thumbnail": "thumbnail" in visible_columns,
        "location": "location" in visible_columns,
        "manufacturer": "manufacturer" in visible_columns and len(selected_manufacturer_values) != 1,
        "package": "package" in visible_columns and len(selected_package_values) != 1,
        # Marking has no filter/facet of its own, so nothing the user selects
        # could ever be "the cause" of it being uniform — never auto-hide it.
        "marking": "marking" in visible_columns,
        "category": "category" in visible_columns
        and not (category_id and _all_same(lambda p: p.category.name if p.category else "Uncategorized")),
        "characteristics": "characteristics" in visible_columns,
        "description": "description" in visible_columns,
        "unit_price": "unit_price" in visible_columns,
    }
    # Fixed columns regardless of preference: row-select checkbox, MPN, Stock.
    empty_colspan = 3 + sum(col_visible.values())

    # Same idea, one level down: drop an attribute's piece from the
    # Characteristics column only when its own facet was narrowed to
    # exactly one value — which already guarantees every part left in the
    # result set matches it, no separate uniformity scan needed.
    collapsed_attr_ids = {ad.id for ad in attr_defs if len(selected_attr_values.get(ad.id, [])) == 1}

    category_options = flatten_tree_from(*load_category_tree())
    location_options = flatten_tree_from(*load_location_tree())

    return render_template(
        "parts/index.html",
        parts=page_items,
        total=total,
        page=page,
        total_pages=total_pages,
        page_size=page_size,
        category_options=category_options,
        location_options=location_options,
        attr_defs=attr_defs,
        attr_value_options=attr_value_options,
        selected_attr_values=selected_attr_values,
        manufacturer_options=manufacturer_options,
        selected_manufacturer_values=selected_manufacturer_values,
        package_options=package_options,
        selected_package_values=selected_package_values,
        filters=request.args,
        selected_category_id=category_id,
        selected_location_id=location_id,
        include_subcategories=include_subcategories,
        sort_field=sort_field,
        sort_dir=sort_dir,
        page_size_options=PAGE_SIZE_OPTIONS,
        col_visible=col_visible,
        empty_colspan=empty_colspan,
        collapsed_attr_ids=collapsed_attr_ids,
    )


# ---------------------------------------------------------------------------
# Create / edit / delete
# ---------------------------------------------------------------------------
def _category_options():
    return flatten_tree_from(*load_category_tree())


def _search_url():
    """Base URL (MPN gets urlencoded and appended by the template) for the
    "search this MPN online" shortcut, per the viewer's own default-engine
    setting."""
    engine = current_user.setting.default_search_engine if current_user.setting else DEFAULT_SEARCH_ENGINE
    return SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_SEARCH_ENGINE])[1]


# A fixed baseline plus whatever currencies are actually in use, so this list
# can't silently drift out of sync with real data the way a hardcoded list did.
COMMON_CURRENCIES = ("CNY", "USD", "AUD", "HKD", "EUR")


def _currency_options():
    existing = {
        row[0]
        for row in db.session.query(Part.currency).filter(Part.currency.isnot(None)).distinct().all()
    }
    return sorted(set(COMMON_CURRENCIES) | existing)


def _manufacturer_options():
    rows = db.session.query(Part.manufacturer).filter(Part.manufacturer.isnot(None), Part.manufacturer != "").distinct().all()
    return sorted({row[0] for row in rows}, key=str.lower)


def _package_options():
    rows = db.session.query(Part.package).filter(Part.package.isnot(None), Part.package != "").distinct().all()
    return sorted({row[0] for row in rows}, key=str.lower)


def _apply_form(part, form):
    part.mpn = form.get("mpn", "").strip()
    part.manufacturer = form.get("manufacturer", "").strip() or None
    part.package = form.get("package", "").strip() or None
    part.marking = form.get("marking", "").strip() or None
    part.description = form.get("description", "").strip() or None
    part.category_id = form.get("category_id") or None
    part.is_suspect_fake = form.get("is_suspect_fake") == "on"
    price = form.get("default_unit_price", "").strip()
    part.default_unit_price = float(price) if price else None
    part.currency = form.get("currency", "CNY").strip() or "CNY"
    min_alert = form.get("min_stock_alert", "").strip()
    part.min_stock_alert = int(min_alert) if min_alert else None


def _apply_attributes(part, form):
    """Saves the attr_<id> fields the edit page renders alongside the basic
    fields, one combined form/save action rather than two."""
    if not part.category:
        return
    for ad in part.category.effective_attribute_defs:
        field = f"attr_{ad.id}"
        raw = form.get(field, "").strip()
        existing = PartAttributeValue.query.filter_by(part_id=part.id, attribute_def_id=ad.id).first()
        if not raw:
            if existing:
                db.session.delete(existing)
            continue
        if existing is None:
            existing = PartAttributeValue(part_id=part.id, attribute_def_id=ad.id)
            db.session.add(existing)
        existing.value_text = raw
        existing.value_number = None
        if ad.data_type in ("number", "engineering"):
            try:
                existing.value_number = float(raw)
            except ValueError:
                pass


# Sentinel for bulk-edit select fields: "leave this field alone" as distinct
# from any real value (including an explicit "clear this field" choice).
BULK_NO_CHANGE = "__nochange__"


def _common_attr(parts, attr):
    """The one value every part shares for `attr`, or None if they differ."""
    values = {getattr(p, attr) for p in parts}
    return values.pop() if len(values) == 1 else None


def _common_attribute_defs(parts):
    """Parametric attribute defs present on *every* selected part's category
    (by id — so an inherited attribute shared across categories still
    counts), in the first part's effective order. These are the only ones
    it's meaningful to bulk-edit; anything only some parts have is left off
    the form entirely rather than risk adding a nonsensical attribute to a
    part whose category doesn't define it."""
    if not parts:
        return []
    per_part = [
        {ad.id: ad for ad in p.category.effective_attribute_defs} if p.category else {}
        for p in parts
    ]
    common_ids = set(per_part[0])
    for defs in per_part[1:]:
        common_ids &= set(defs)
    return [ad for ad_id, ad in per_part[0].items() if ad_id in common_ids]


def _common_attr_value(parts, attr_id):
    """The one value every part shares for attribute `attr_id` (as stored
    text), or None if they differ or none have it — same convention as
    _common_attr, pre-fills the bulk-edit field only when unambiguous."""
    values = {next((v.value_text for v in p.attribute_values if v.attribute_def_id == attr_id), None) for p in parts}
    return values.pop() if len(values) == 1 else None


@bp.route("/bulk-edit", methods=["POST"])
@login_required
def bulk_edit():
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).order_by(Part.mpn).all() if ids else []
    if not parts:
        flash("Select at least one part to bulk edit", "warning")
        return redirect(url_for("parts.index"))

    common = {
        "manufacturer": _common_attr(parts, "manufacturer"),
        "category_id": _common_attr(parts, "category_id"),
        "package": _common_attr(parts, "package"),
        "marking": _common_attr(parts, "marking"),
        "default_unit_price": _common_attr(parts, "default_unit_price"),
        "currency": _common_attr(parts, "currency"),
        "description": _common_attr(parts, "description"),
        "min_stock_alert": _common_attr(parts, "min_stock_alert"),
        "is_suspect_fake": _common_attr(parts, "is_suspect_fake"),
    }
    attr_defs = _common_attribute_defs(parts)
    attr_common_values = {ad.id: _common_attr_value(parts, ad.id) for ad in attr_defs}

    linked_photo_counts = {}
    for p in parts:
        for photo in p.photos:
            linked_photo_counts[photo.id] = linked_photo_counts.get(photo.id, 0) + 1
    linked_photos = []
    if linked_photo_counts:
        photos_by_id = {ph.id: ph for ph in Photo.query.filter(Photo.id.in_(linked_photo_counts)).all()}
        linked_photos = [(photos_by_id[pid], count) for pid, count in linked_photo_counts.items()]

    return render_template(
        "parts/bulk_edit.html",
        parts=parts,
        common=common,
        category_options=_category_options(),
        currency_options=_currency_options(),
        attr_defs=attr_defs,
        attr_common_values=attr_common_values,
        linked_photos=linked_photos,
    )


@bp.route("/bulk-edit/apply", methods=["POST"])
@login_required
def bulk_edit_apply():
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    if not parts:
        flash("No parts selected", "warning")
        return redirect(url_for("parts.index"))

    form = request.form
    changed = []

    def apply_clearable(field, setter, cast=str):
        """Bulk-edit convention: a blank box means "leave every part's own
        value alone"; the "<field>_clear" checkbox is the only way to
        explicitly wipe it on every selected part (a value differing across
        parts renders the box blank too, so blank-by-itself can't double as
        "clear" without silently erasing data nobody meant to touch)."""
        clear = form.get(f"{field}_clear") == "1"
        raw = form.get(field, "").strip()
        if not raw and not clear:
            return False
        value = None if clear else cast(raw)
        for p in parts:
            setter(p, value)
        return True

    if apply_clearable("manufacturer", lambda p, v: setattr(p, "manufacturer", v)):
        changed.append("manufacturer")

    category_raw = form.get("category_id", BULK_NO_CHANGE)
    if category_raw != BULK_NO_CHANGE:
        changed.append("category")
        cat_id = int(category_raw) if category_raw else None
        for p in parts:
            p.category_id = cat_id

    if apply_clearable("package", lambda p, v: setattr(p, "package", v)):
        changed.append("package")

    if apply_clearable("marking", lambda p, v: setattr(p, "marking", v)):
        changed.append("marking")

    if apply_clearable("description", lambda p, v: setattr(p, "description", v)):
        changed.append("description")

    if apply_clearable("min_stock_alert", lambda p, v: setattr(p, "min_stock_alert", v), cast=int):
        changed.append("minimum stock alert")

    price_clear = form.get("default_unit_price_clear") == "1"
    price_raw = form.get("default_unit_price", "").strip()
    currency_raw = form.get("currency", BULK_NO_CHANGE)
    if price_clear:
        changed.append("manual reference price")
        for p in parts:
            p.default_unit_price = None
    elif price_raw:
        if currency_raw == BULK_NO_CHANGE:
            flash("A manual reference price was entered but no currency was picked, so it was skipped", "warning")
        else:
            changed.append("manual reference price")
            price_val = float(price_raw)
            for p in parts:
                p.default_unit_price = price_val
                p.currency = currency_raw
    elif currency_raw != BULK_NO_CHANGE:
        changed.append("currency")
        for p in parts:
            p.currency = currency_raw

    fake_raw = form.get("is_suspect_fake", BULK_NO_CHANGE)
    if fake_raw != BULK_NO_CHANGE:
        changed.append("suspect counterfeit flag")
        fake_val = fake_raw == "1"
        for p in parts:
            p.is_suspect_fake = fake_val

    # Parametric attributes common to every selected part's category (see
    # _common_attribute_defs) — recomputed from the submitted part_ids
    # rather than trusted from the form, so a tampered request can't sneak
    # in an attribute id that isn't actually valid for all of them. Blank
    # means "leave each part's own value alone", same convention as the
    # plain text fields above (unlike the single-part edit form, where
    # blank clears it — bulk edit never touches a field you didn't fill in).
    for ad in _common_attribute_defs(parts):
        field = f"attr_{ad.id}"
        if ad.data_type == "enum":
            raw = form.get(field, BULK_NO_CHANGE)
            if raw == BULK_NO_CHANGE:
                continue
        else:
            clear = form.get(f"{field}_clear") == "1"
            raw = "" if clear else form.get(field, "").strip()
            if not raw and not clear:
                continue
        changed.append(f"'{ad.name}'")
        for p in parts:
            existing = PartAttributeValue.query.filter_by(part_id=p.id, attribute_def_id=ad.id).first()
            if not raw:
                if existing:
                    db.session.delete(existing)
                continue
            if existing is None:
                existing = PartAttributeValue(part_id=p.id, attribute_def_id=ad.id)
                db.session.add(existing)
            existing.value_text = raw
            existing.value_number = None
            if ad.data_type in ("number", "engineering"):
                try:
                    existing.value_number = float(raw)
                except ValueError:
                    pass

    if not changed:
        flash("No fields were filled in — nothing was changed", "info")
        return redirect(url_for("parts.index"))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Bulk update failed: check that changing manufacturer wouldn't duplicate an existing MPN", "danger")
        return redirect(url_for("parts.index"))

    flash(f"Updated {', '.join(changed)} on {len(parts)} part(s)", "success")
    return redirect(url_for("parts.index"))


@bp.route("/bulk-edit/attach-document", methods=["POST"])
@login_required
def bulk_attach_document():
    """Link one existing shared Document to every selected part in one go —
    e.g. the same datasheet covers 107 resistance variants of the same
    series. Separate from bulk_edit_apply since this is an add-to-a-
    relationship action, not "set a field to a value"."""
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    doc_id = request.form.get("document_id", type=int)
    doc = Document.query.get(doc_id) if doc_id else None

    if not parts or not doc:
        flash("Select at least one part and a document to link", "warning")
        return redirect(url_for("parts.index"))

    linked = 0
    for p in parts:
        if doc not in p.documents:
            p.documents.append(doc)
            linked += 1
    db.session.commit()

    already = len(parts) - linked
    msg = f"Linked '{doc.title}' to {linked} part(s)"
    if already:
        msg += f" ({already} already had it)"
    flash(msg, "success")
    return redirect(url_for("parts.index"))


@bp.route("/bulk-edit/attach-photo", methods=["POST"])
@login_required
def bulk_attach_photo():
    """Link one existing shared Photo to every selected part in one go —
    mirrors bulk_attach_document. Becomes each part's cover photo only if
    that part doesn't already have one."""
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    photo_id = request.form.get("photo_id", type=int)
    photo = Photo.query.get(photo_id) if photo_id else None

    if not parts or not photo:
        flash("Select at least one part and a photo to link", "warning")
        return redirect(url_for("parts.index"))

    linked = 0
    for p in parts:
        already_linked = PartPhoto.query.filter_by(part_id=p.id, photo_id=photo.id).first()
        if not already_linked:
            _link_photo(p, photo, is_primary=len(p.photo_links) == 0)
            linked += 1
    db.session.commit()

    already = len(parts) - linked
    msg = f"Linked photo to {linked} part(s)"
    if already:
        msg += f" ({already} already had it)"
    flash(msg, "success")
    return redirect(url_for("parts.index"))


@bp.route("/bulk-edit/detach-photo", methods=["POST"])
@login_required
def bulk_detach_photo():
    """Unlink one shared Photo from every selected part in one go — the
    reverse of bulk_attach_photo. Like the single-part unlink, this never
    deletes the photo itself, even if it ends up linked to nobody."""
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    photo_id = request.form.get("photo_id", type=int)
    photo = Photo.query.get(photo_id) if photo_id else None

    if not parts or not photo:
        flash("Select at least one part and a photo to remove", "warning")
        return redirect(url_for("parts.index"))

    removed = 0
    for p in parts:
        link = PartPhoto.query.filter_by(part_id=p.id, photo_id=photo.id).first()
        if not link:
            continue
        was_primary = link.is_primary
        db.session.delete(link)
        db.session.flush()
        if was_primary:
            nxt = (
                PartPhoto.query.filter_by(part_id=p.id)
                .join(Photo)
                .order_by(Photo.uploaded_at)
                .first()
            )
            if nxt:
                nxt.is_primary = True
        removed += 1
    db.session.commit()

    flash(f"Removed photo from {removed} part(s) (the photo itself was not deleted)", "success")
    return redirect(url_for("parts.index"))


@bp.route("/bulk-edit/upload-photo", methods=["POST"])
@login_required
def bulk_upload_photo():
    """Uploads new photo(s) once and links each to every selected part —
    e.g. one reel/tape shot that actually covers all N selected variants,
    instead of uploading it N times from each part's own page."""
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    files = request.files.getlist("photo")
    if not parts or not files:
        flash("Select at least one part and a photo file", "warning")
        return redirect(url_for("parts.index"))

    added = 0
    for f in files:
        rel_path = save_upload(f, f"parts/{parts[0].id}/photos", current_app.config["ALLOWED_PHOTO_EXT"])
        if not rel_path:
            continue
        thumb = make_thumbnail(rel_path)
        photo = Photo(file_path=rel_path, thumbnail_path=thumb, uploaded_by=current_user.id)
        db.session.add(photo)
        db.session.flush()
        for p in parts:
            _link_photo(p, photo, is_primary=len(p.photo_links) == 0)
        added += 1

    if added:
        db.session.commit()
        flash(f"Uploaded {added} photo(s) and linked to {len(parts)} part(s)", "success")
    else:
        flash("No valid image file selected", "warning")
    return redirect(url_for("parts.index"))


@bp.route("/bulk-edit/lcsc-fetch", methods=["POST"])
@login_required
def bulk_lcsc_fetch():
    """Downloads LCSC image(s) once and links each to every selected part —
    the Bulk Edit equivalent of lcsc_fetch."""
    ids = request.form.getlist("part_ids", type=int)
    parts = Part.query.filter(Part.id.in_(ids)).all() if ids else []
    urls = request.form.getlist("image_url")
    if not parts or not urls:
        flash("Select at least one part and an image", "warning")
        return redirect(url_for("parts.index"))

    added = 0
    for url in urls:
        try:
            photo = _save_lcsc_photo(url, parts[0].id)
        except LCSC_ERRORS as e:
            flash(f"Failed to download an image: {e}", "danger")
            continue
        for p in parts:
            _link_photo(p, photo, is_primary=len(p.photo_links) == 0)
        added += 1

    if added:
        db.session.commit()
        flash(f"Downloaded {added} photo(s) from LCSC and linked to {len(parts)} part(s)", "success")
    return redirect(url_for("parts.index"))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        if not request.form.get("mpn", "").strip():
            flash("Part number (MPN) cannot be empty", "danger")
            return redirect(url_for("parts.new"))
        part = Part()
        _apply_form(part, request.form)
        db.session.add(part)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Save failed: a part with the same manufacturer + MPN may already exist", "danger")
            return redirect(url_for("parts.new"))
        # Remember the currency for the next new part — see
        # UserSetting.last_part_currency.
        if current_user.setting and part.currency:
            current_user.setting.last_part_currency = part.currency
            db.session.commit()
        flash(f"Part '{part.mpn}' created", "success")
        return redirect(url_for("parts.detail", part_id=part.id))

    return render_template(
        "parts/form.html",
        part=None,
        category_options=_category_options(),
        currency_options=_currency_options(),
        manufacturer_options=_manufacturer_options(),
        package_options=_package_options(),
        default_currency=(current_user.setting.last_part_currency if current_user.setting else None) or "CNY",
    )


@bp.route("/<int:part_id>/edit", methods=["GET", "POST"])
@login_required
def edit(part_id):
    part = Part.query.get_or_404(part_id)
    if request.method == "POST":
        _apply_form(part, request.form)
        _apply_attributes(part, request.form)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Save failed: a part with the same manufacturer + MPN may already exist", "danger")
            return redirect(url_for("parts.edit", part_id=part.id))
        flash("Changes saved", "success")
        return redirect(url_for("parts.edit", part_id=part.id))

    attr_defs = part.category.effective_attribute_defs if part.category else []
    values_by_def = {v.attribute_def_id: v for v in part.attribute_values}
    return render_template(
        "parts/form.html",
        part=part,
        category_options=_category_options(),
        currency_options=_currency_options(),
        manufacturer_options=_manufacturer_options(),
        package_options=_package_options(),
        attr_defs=attr_defs,
        values_by_def=values_by_def,
        search_url=_search_url(),
    )


@bp.route("/<int:part_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(part_id):
    part = Part.query.get_or_404(part_id)
    db.session.delete(part)
    db.session.commit()
    flash("Part deleted", "success")
    return redirect(url_for("parts.index"))


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
@bp.route("/<int:part_id>")
@login_required
def detail(part_id):
    part = Part.query.get_or_404(part_id)
    attr_defs = part.category.effective_attribute_defs if part.category else []
    values_by_def = {v.attribute_def_id: v for v in part.attribute_values}
    location_options = flatten_tree_from(*load_location_tree())
    return render_template(
        "parts/detail.html",
        part=part,
        attr_defs=attr_defs,
        values_by_def=values_by_def,
        location_options=location_options,
        currency_options=_currency_options(),
        search_url=_search_url(),
    )


# ---------------------------------------------------------------------------
# Photos (can be shared across parts: create new or link an existing one,
# same pattern as Documents — see PartPhoto in app/models.py)
# ---------------------------------------------------------------------------
def _link_photo(part, photo, is_primary):
    db.session.add(PartPhoto(part_id=part.id, photo_id=photo.id, is_primary=is_primary))


@bp.route("/<int:part_id>/photos", methods=["POST"])
@login_required
def upload_photo(part_id):
    part = Part.query.get_or_404(part_id)
    files = request.files.getlist("photo")
    added = 0
    for f in files:
        rel_path = save_upload(f, f"parts/{part.id}/photos", current_app.config["ALLOWED_PHOTO_EXT"])
        if not rel_path:
            continue
        thumb = make_thumbnail(rel_path)
        is_first = len(part.photo_links) == 0 and added == 0
        photo = Photo(file_path=rel_path, thumbnail_path=thumb, uploaded_by=current_user.id)
        db.session.add(photo)
        db.session.flush()
        _link_photo(part, photo, is_primary=is_first)
        added += 1
    if added:
        db.session.commit()
        flash(f"Uploaded {added} photo(s)", "success")
    else:
        flash("No valid image file selected", "warning")
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/photos/search.json")
@login_required
def search_photos():
    q = request.args.get("q", "").strip()
    query = Photo.query
    if q:
        query = query.join(PartPhoto).join(Part).filter(Part.mpn.ilike(f"%{q}%")).distinct()
    photos = query.order_by(Photo.uploaded_at.desc()).limit(20).all()
    return jsonify([
        {
            "id": p.id,
            "thumbnail_url": url_for("parts.uploaded_file", rel_path=p.thumbnail_path or p.file_path),
            "parts": ", ".join(part.mpn for part in p.parts) or "not linked to any part",
        }
        for p in photos
    ])


@bp.route("/<int:part_id>/photos/attach", methods=["POST"])
@login_required
def attach_photo(part_id):
    part = Part.query.get_or_404(part_id)
    photo_id = request.form.get("photo_id", type=int)
    photo = Photo.query.get(photo_id) if photo_id else None
    if not photo:
        flash("Photo to link was not found", "danger")
        return redirect(url_for("parts.edit", part_id=part_id))
    if photo.id in [p.id for p in part.photos]:
        flash("That photo is already linked to this part", "warning")
        return redirect(url_for("parts.edit", part_id=part_id))
    _link_photo(part, photo, is_primary=len(part.photo_links) == 0)
    db.session.commit()
    flash("Linked shared photo", "success")
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/<int:part_id>/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def delete_photo(part_id, photo_id):
    """Unlinks the photo from this part only — like detaching a shared
    document, the photo itself (and its file) isn't deleted, since other
    parts may still be using it. Full deletion is a deliberate action on
    the standalone Images management page."""
    link = PartPhoto.query.filter_by(part_id=part_id, photo_id=photo_id).first_or_404()
    was_primary = link.is_primary
    db.session.delete(link)
    db.session.flush()
    if was_primary:
        nxt = (
            PartPhoto.query.filter_by(part_id=part_id)
            .join(Photo)
            .order_by(Photo.uploaded_at)
            .first()
        )
        if nxt:
            nxt.is_primary = True
    db.session.commit()
    flash("Photo unlinked from this part (not deleted — other parts may still use it)", "success")
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/<int:part_id>/photos/<int:photo_id>/primary", methods=["POST"])
@login_required
def set_primary_photo(part_id, photo_id):
    PartPhoto.query.filter_by(part_id=part_id).update({"is_primary": False})
    link = PartPhoto.query.filter_by(part_id=part_id, photo_id=photo_id).first_or_404()
    link.is_primary = True
    db.session.commit()
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/photos/<int:photo_id>/crop-suggestion")
@login_required
def photo_crop_suggestion(photo_id):
    """Backs the "Recrop thumbnail" modal: a suggested crop box for a small
    component lost in a busy LCSC product-photo background. This is a rough
    heuristic (see app/smart_crop.py) — the frontend always shows it as an
    adjustable box for the user to confirm or drag, never applies it silently."""
    photo = Photo.query.get_or_404(photo_id)
    full = os.path.join(current_app.config["UPLOAD_FOLDER"], photo.file_path)
    try:
        with PILImage.open(full) as im:
            width, height = im.size
    except OSError:
        abort(404)

    saved = photo.crop_box_tuple
    if saved is not None:
        return jsonify({"width": width, "height": height, "box": list(saved), "source": "saved"})

    try:
        box = suggest_crop_box(full)
    except Exception:
        box = None
    return jsonify({"width": width, "height": height, "box": list(box) if box else None, "source": "suggested"})


@bp.route("/photos/<int:photo_id>/recrop", methods=["POST"])
@login_required
def photo_recrop(photo_id):
    """Re-generate a photo's thumbnail cropped to a user-confirmed box —
    the original full photo is never modified, only the derived thumbnail.
    The crop is shared by every part linked to this photo."""
    photo = Photo.query.get_or_404(photo_id)
    data = request.get_json(silent=True) or {}
    try:
        box = tuple(int(data[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid crop box"}), 400
    if box[2] <= box[0] or box[3] <= box[1]:
        return jsonify({"ok": False, "error": "Invalid crop box"}), 400

    thumb_rel = make_thumbnail(photo.file_path, box=box)
    if not thumb_rel:
        return jsonify({"ok": False, "error": "Failed to generate thumbnail"}), 500
    photo.thumbnail_path = thumb_rel
    photo.crop_box = ",".join(str(v) for v in box)
    db.session.commit()
    return jsonify({
        "ok": True,
        "thumbnail_url": url_for("parts.uploaded_file", rel_path=thumb_rel) + f"?t={int(time.time())}",
        # Echoed back so the page can refresh the crop overlay its preview
        # button carries, instead of showing the previous box until reload.
        "crop_box": photo.crop_box,
    })


def _lcsc_search_keyword(keyword):
    """Look up a keyword (usually a part's MPN) and return its exact-match
    product photos. If there's no exact match, fall back to a handful of
    similar candidates (same as LCSC's own search results page) for the
    user to pick from. Returns a JSON-serializable dict."""
    if not keyword:
        return {"found": False}
    try:
        exact_matches = search_exact(keyword)
    except LCSC_ERRORS:
        return {"found": False, "error": "LCSC lookup failed, try again"}

    exact_with_photos = []
    for product in exact_matches:
        images = product_images(product)
        if images:
            exact_with_photos.append(
                {
                    "product_code": product.get("productCode"),
                    "product_model": product.get("productModel"),
                    "brand": product.get("brandNameEn"),
                    "encap": product.get("encapStandard"),
                    "images": images,
                }
            )
        # Else: exact model match, but that exact SKU has no photo on LCSC —
        # a similar/related SKU (below) may still have a usable one.

    if len(exact_with_photos) == 1:
        return {"found": True, **exact_with_photos[0]}
    if len(exact_with_photos) > 1:
        # More than one listing exactly matches the keyword (different
        # manufacturers/variants sharing the same model number) — let the
        # user pick rather than silently guessing one.
        return {"found": True, "exact_matches": exact_with_photos}

    try:
        suggestions = search_suggestions(keyword)
    except LCSC_ERRORS:
        suggestions = []

    if not suggestions:
        return {
            "found": False,
            "error": "No exact match on LCSC for that keyword. Try adjusting it (remove a suffix, fix a typo).",
        }
    return {"found": False, "suggestions": suggestions}


@bp.route("/<int:part_id>/photos/lcsc-search")
@login_required
def lcsc_search(part_id):
    """AJAX endpoint backing the edit page's "Search LCSC" button (keyword
    usually the part's MPN, but editable so a suffix/typo can be tweaked)."""
    keyword = request.args.get("keyword", "").strip()
    result = _lcsc_search_keyword(keyword)
    return jsonify(result), (502 if result.get("error") == "LCSC lookup failed, try again" else 200)


@bp.route("/bulk-edit/lcsc-search")
@login_required
def bulk_lcsc_search():
    """Same as lcsc_search, but not tied to any one part — backs Bulk Edit's
    "Fetch a shared photo from LCSC" search, since the selected parts may
    not share a single MPN to default the keyword to."""
    keyword = request.args.get("keyword", "").strip()
    result = _lcsc_search_keyword(keyword)
    return jsonify(result), (502 if result.get("error") == "LCSC lookup failed, try again" else 200)


def _save_lcsc_photo(url, storage_part_id):
    """Downloads one LCSC image and saves it as a new, unlinked Photo — the
    caller links it to whichever part(s) should have it. Raises LCSC_ERRORS
    on failure. `storage_part_id` only picks which parts/<id>/photos/ folder
    the file lands in on disk; it has no bearing on which parts see it."""
    content, ext = download_lcsc_image(url)
    subdir = f"parts/{storage_part_id}/photos"
    target_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], subdir)
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    rel_path = f"{subdir}/{filename}"
    with open(os.path.join(target_dir, filename), "wb") as f:
        f.write(content)
    thumb_rel = make_thumbnail(rel_path)
    photo = Photo(file_path=rel_path, thumbnail_path=thumb_rel, uploaded_by=current_user.id)
    db.session.add(photo)
    db.session.flush()
    return photo


@bp.route("/<int:part_id>/photos/lcsc-fetch", methods=["POST"])
@login_required
def lcsc_fetch(part_id):
    part = Part.query.get_or_404(part_id)
    urls = request.form.getlist("image_url")
    if not urls:
        flash("No image selected", "warning")
        return redirect(url_for("parts.edit", part_id=part_id))

    existing_count = len(part.photo_links)
    added = 0
    for url in urls:
        try:
            photo = _save_lcsc_photo(url, part.id)
        except LCSC_ERRORS as e:
            flash(f"Failed to download an image: {e}", "danger")
            continue
        _link_photo(part, photo, is_primary=(existing_count == 0 and added == 0))
        added += 1

    if added:
        db.session.commit()
        flash(f"Downloaded {added} photo(s) from LCSC", "success")
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/uploads/<path:rel_path>")
@login_required
def uploaded_file(rel_path):
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], rel_path)


# ---------------------------------------------------------------------------
# Documents (can be shared across parts: create new or link an existing one)
# ---------------------------------------------------------------------------
@bp.route("/documents/search.json")
@login_required
def search_documents():
    q = request.args.get("q", "").strip()
    query = Document.query
    if q:
        query = query.filter(Document.title.ilike(f"%{q}%"))
    docs = query.order_by(Document.title).limit(20).all()
    return jsonify(
        [{"id": d.id, "title": d.title, "doc_type": d.doc_type, "url": d.url} for d in docs]
    )


@bp.route("/<int:part_id>/documents", methods=["POST"])
@login_required
def attach_document(part_id):
    part = Part.query.get_or_404(part_id)
    mode = request.form.get("mode", "new")

    if mode == "existing":
        doc_id = request.form.get("document_id", type=int)
        doc = Document.query.get(doc_id) if doc_id else None
        if not doc:
            flash("Document to link was not found", "danger")
            return redirect(url_for("parts.edit", part_id=part_id))
        if doc not in part.documents:
            part.documents.append(doc)
            db.session.commit()
            flash(f"Linked shared document '{doc.title}'", "success")
        return redirect(url_for("parts.edit", part_id=part_id))

    title = request.form.get("title", "").strip()
    doc_type = request.form.get("doc_type", "other")
    url = request.form.get("url", "").strip()
    file_storage = request.files.get("file")
    rel_path = None
    if file_storage and file_storage.filename:
        rel_path = save_upload(file_storage, "documents", current_app.config["ALLOWED_DOC_EXT"])

    if not title or (not url and not rel_path):
        flash("Please enter a document title and provide either a link or an uploaded file", "danger")
        return redirect(url_for("parts.edit", part_id=part_id))

    doc = Document(
        title=title,
        doc_type=doc_type,
        url=url or None,
        file_path=rel_path,
        uploaded_by=current_user.id,
    )
    doc.parts.append(part)
    db.session.add(doc)
    db.session.commit()
    flash(f"Document '{title}' added — it can be linked from other parts to share it", "success")
    return redirect(url_for("parts.edit", part_id=part_id))


@bp.route("/<int:part_id>/documents/<int:doc_id>/detach", methods=["POST"])
@login_required
def detach_document(part_id, doc_id):
    part = Part.query.get_or_404(part_id)
    doc = Document.query.get_or_404(doc_id)
    if doc in part.documents:
        part.documents.remove(doc)
        db.session.commit()
        flash("Unlinked (the document itself was not deleted, other parts can still use it)", "success")
    return redirect(url_for("parts.edit", part_id=part_id))
