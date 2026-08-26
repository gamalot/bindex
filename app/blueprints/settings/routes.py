from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.settings import bp
from app.extensions import db
from app.models import Part, UserSetting
from app.utils import SEARCH_ENGINES

# A fixed baseline plus whatever currencies are actually in use, so this list
# can't silently drift out of sync with real data the way a hardcoded list did.
COMMON_CURRENCIES = ("CNY", "USD", "AUD", "HKD", "EUR")

LIST_COLUMN_LABELS = {
    "thumbnail": "Thumbnail",
    "location": "Location",
    "manufacturer": "Manufacturer",
    "package": "Package",
    "marking": "Marking",
    "category": "Category",
    "characteristics": "Characteristics",
    "description": "Description",
    "unit_price": "Unit Price",
}


def _ensure_setting():
    if current_user.setting is None:
        s = UserSetting(user_id=current_user.id)
        db.session.add(s)
        db.session.commit()
    return current_user.setting


def _currency_options():
    in_use = {
        row[0] for row in db.session.query(Part.currency).filter(Part.currency.isnot(None)).distinct().all()
    }
    return sorted(in_use | set(COMMON_CURRENCIES))


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    setting = _ensure_setting()
    if request.method == "POST":
        theme = request.form.get("theme", "system")
        page_size = request.form.get("page_size", type=int) or 25
        default_price_currency = request.form.get("default_price_currency", "AUD").strip()
        default_search_engine = request.form.get("default_search_engine", "google").strip()
        if theme in ("light", "dark", "system"):
            setting.theme = theme
        setting.page_size = max(min(page_size, 1000), 5)
        if default_price_currency:
            setting.default_price_currency = default_price_currency
        if default_search_engine in SEARCH_ENGINES:
            setting.default_search_engine = default_search_engine
        setting.visible_columns = request.form.getlist("visible_columns")

        # The Documents and Images pages keep their own page size and
        # preview style. They were only reachable from the inline controls
        # on those pages, so Settings looked like it governed the whole app
        # while actually governing just the parts list.
        for field, options in (
            ("doc_page_size", UserSetting.DOC_PAGE_SIZE_OPTIONS),
            ("img_page_size", UserSetting.DOC_PAGE_SIZE_OPTIONS),
        ):
            value = request.form.get(field, type=int)
            if value in options:
                setattr(setting, field, value)
        for field in ("doc_preview_mode", "img_preview_mode"):
            value = request.form.get(field, "").strip()
            if value in ("panel", "modal"):
                setattr(setting, field, value)

        db.session.commit()
        flash("Settings saved", "success")
        return redirect(url_for("settings.index"))

    return render_template(
        "settings/index.html",
        setting=setting,
        currency_options=_currency_options(),
        list_columns=UserSetting.LIST_COLUMNS,
        list_column_labels=LIST_COLUMN_LABELS,
        search_engines=SEARCH_ENGINES,
        page_size_options=UserSetting.DOC_PAGE_SIZE_OPTIONS,
    )


@bp.route("/theme", methods=["POST"])
@login_required
def quick_theme():
    """Lightweight endpoint used by the navbar's theme toggle buttons."""
    setting = _ensure_setting()
    theme = (request.get_json(silent=True) or {}).get("theme", "system")
    if theme in ("light", "dark", "system"):
        setting.theme = theme
        db.session.commit()
    return jsonify({"ok": True, "theme": setting.theme})


@bp.route("/page-size", methods=["POST"])
@login_required
def quick_page_size():
    """Lightweight endpoint used by the parts list's "Per page" dropdown, so
    the choice sticks as a permanent preference instead of only living in
    that page's URL query string (lost on Reset / navigating away)."""
    setting = _ensure_setting()
    page_size = (request.get_json(silent=True) or {}).get("page_size")
    if isinstance(page_size, int) and 5 <= page_size <= 1000:
        setting.page_size = page_size
        db.session.commit()
    return jsonify({"ok": True, "page_size": setting.page_size})


@bp.route("/doc-page-size", methods=["POST"])
@login_required
def quick_doc_page_size():
    """Lightweight endpoint used by the Documents page's "Per page" dropdown."""
    setting = _ensure_setting()
    page_size = (request.get_json(silent=True) or {}).get("page_size")
    if page_size in UserSetting.DOC_PAGE_SIZE_OPTIONS:
        setting.doc_page_size = page_size
        db.session.commit()
    return jsonify({"ok": True, "page_size": setting.doc_page_size})


@bp.route("/doc-preview-mode", methods=["POST"])
@login_required
def quick_doc_preview_mode():
    """Lightweight endpoint used by the Documents page's preview-mode toggle
    (inline side panel vs. a floating window sized off the document itself)."""
    setting = _ensure_setting()
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode in UserSetting.DOC_PREVIEW_MODES:
        setting.doc_preview_mode = mode
        db.session.commit()
    return jsonify({"ok": True, "mode": setting.doc_preview_mode})


@bp.route("/img-page-size", methods=["POST"])
@login_required
def quick_img_page_size():
    """Lightweight endpoint used by the Images page's "Per page" dropdown."""
    setting = _ensure_setting()
    page_size = (request.get_json(silent=True) or {}).get("page_size")
    if page_size in UserSetting.DOC_PAGE_SIZE_OPTIONS:
        setting.img_page_size = page_size
        db.session.commit()
    return jsonify({"ok": True, "page_size": setting.img_page_size})


@bp.route("/img-preview-mode", methods=["POST"])
@login_required
def quick_img_preview_mode():
    """Lightweight endpoint used by the Images page's preview-mode toggle."""
    setting = _ensure_setting()
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode in UserSetting.DOC_PREVIEW_MODES:
        setting.img_preview_mode = mode
        db.session.commit()
    return jsonify({"ok": True, "mode": setting.img_preview_mode})
