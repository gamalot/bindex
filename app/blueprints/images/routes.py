import os

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.images import bp
from app.extensions import db
from app.models import Part, PartPhoto, Photo, UserSetting
from app.utils import admin_required

PAGE_SIZE_OPTIONS = UserSetting.DOC_PAGE_SIZE_OPTIONS


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    cover = request.args.get("cover", "").strip()  # "" = all, "1" = cover only, "0" = non-cover only
    page_size_param = request.args.get("page_size", type=int)
    default_page_size = current_user.setting.img_page_size if current_user.setting else PAGE_SIZE_OPTIONS[1]
    page_size = page_size_param if page_size_param in PAGE_SIZE_OPTIONS else default_page_size
    preview_mode = current_user.setting.img_preview_mode if current_user.setting else UserSetting.DOC_PREVIEW_MODES[0]

    query = Photo.query
    if q:
        query = query.filter(Photo.part_links.any(PartPhoto.part.has(Part.mpn.ilike(f"%{q}%"))))
    if cover == "1":
        query = query.filter(Photo.part_links.any(PartPhoto.is_primary.is_(True)))
    elif cover == "0":
        query = query.filter(~Photo.part_links.any(PartPhoto.is_primary.is_(True)))
    query = query.order_by(Photo.uploaded_at.desc())

    total = query.count()
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(request.args.get("page", 1, type=int), 1), total_pages)
    photos = query.offset((page - 1) * page_size).limit(page_size).all()

    return render_template(
        "images/index.html",
        photos=photos,
        q=q,
        cover=cover,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=page_size,
        page_size_options=PAGE_SIZE_OPTIONS,
        preview_mode=preview_mode,
    )


def _delete_photo(photo):
    """Fully deletes the photo (and its file) — unlike the per-part "unlink"
    action on the edit page, this removes it for every part sharing it.
    PartPhoto rows go with it via Photo.part_links' cascade."""
    for rel in (photo.file_path, photo.thumbnail_path):
        if rel:
            full = os.path.join(current_app.config["UPLOAD_FOLDER"], rel)
            if os.path.exists(full):
                os.remove(full)
    db.session.delete(photo)


@bp.route("/<int:photo_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    _delete_photo(photo)
    db.session.commit()
    flash("Photo deleted", "success")
    return redirect(url_for("images.index"))


@bp.route("/bulk-delete", methods=["POST"])
@login_required
@admin_required
def bulk_delete():
    ids = request.form.getlist("photo_ids", type=int)
    photos = Photo.query.filter(Photo.id.in_(ids)).all() if ids else []
    if not photos:
        flash("Select at least one photo to delete", "warning")
        return redirect(url_for("images.index"))
    for photo in photos:
        _delete_photo(photo)
    db.session.commit()
    flash(f"Deleted {len(photos)} photo(s)", "success")
    return redirect(url_for("images.index"))
