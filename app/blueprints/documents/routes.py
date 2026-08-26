import os

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.blueprints.documents import bp
from app.extensions import db
from app.models import Document, UserSetting
from app.utils import (
    DOC_TYPES,
    INLINE_DOC_EXT,
    PLAINTEXT_DOC_EXT,
    admin_required,
    pdf_page_ratio,
)


PAGE_SIZE_OPTIONS = UserSetting.DOC_PAGE_SIZE_OPTIONS


def _fill_page_ratios(documents):
    """Work out the page shape of any PDF on this page that hasn't been
    measured yet, and remember it. Only ever runs once per document: new
    uploads get measured the first time they're listed, and everything
    already stored is skipped."""
    pending = [
        d
        for d in documents
        if d.page_ratio is None and d.file_path and d.file_path.lower().endswith(".pdf")
    ]
    if not pending:
        return
    for doc in pending:
        full = os.path.join(current_app.config["UPLOAD_FOLDER"], doc.file_path)
        ratio = pdf_page_ratio(full)
        if ratio:
            doc.page_ratio = ratio
    db.session.commit()


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    doc_type = request.args.get("doc_type", "").strip()
    page_size_param = request.args.get("page_size", type=int)
    default_page_size = current_user.setting.doc_page_size if current_user.setting else PAGE_SIZE_OPTIONS[1]
    page_size = page_size_param if page_size_param in PAGE_SIZE_OPTIONS else default_page_size
    preview_mode = current_user.setting.doc_preview_mode if current_user.setting else UserSetting.DOC_PREVIEW_MODES[0]

    # Only offer kinds that actually occur, so the filter can't list options
    # that would always come back empty (today it's overwhelmingly datasheets).
    used_types = [
        row[0]
        for row in db.session.query(Document.doc_type).distinct().all()
        if row[0]
    ]
    type_options = [t for t in DOC_TYPES if t in used_types]

    query = Document.query
    if q:
        query = query.filter(Document.title.ilike(f"%{q}%"))
    if doc_type in type_options:
        query = query.filter(Document.doc_type == doc_type)
    query = query.order_by(Document.title)

    total = query.count()
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(request.args.get("page", 1, type=int), 1), total_pages)
    documents = query.offset((page - 1) * page_size).limit(page_size).all()
    _fill_page_ratios(documents)

    return render_template(
        "documents/index.html",
        documents=documents,
        q=q,
        doc_type=doc_type,
        type_options=type_options,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=page_size,
        page_size_options=PAGE_SIZE_OPTIONS,
        preview_mode=preview_mode,
    )


@bp.route("/<int:doc_id>/file")
@login_required
def file(doc_id):
    """Serve a document's file under its own title.

    Uploads are stored under a UUID name, which nobody minds while
    everything is a PDF you just read on screen — but a reference-design
    archive or a Word file is something you actually save, and
    "7f3a9b….zip" on your disk is useless. Anything the browser can't
    display is sent as a download; the rest opens in place.
    """
    doc = Document.query.get_or_404(doc_id)
    if not doc.file_path:
        abort(404)

    ext = doc.file_path.rsplit(".", 1)[-1].lower()
    # secure_filename can reduce a title to nothing (e.g. all-CJK); fall
    # back to the stored name rather than serving a bare extension.
    stem = secure_filename(doc.title) or f"document-{doc.id}"
    directory, stored = os.path.split(doc.file_path)

    kwargs = {"download_name": f"{stem}.{ext}"}
    if ext not in INLINE_DOC_EXT:
        kwargs["as_attachment"] = True
    if ext in PLAINTEXT_DOC_EXT:
        kwargs["mimetype"] = "text/plain"

    return send_from_directory(
        os.path.join(current_app.config["UPLOAD_FOLDER"], directory), stored, **kwargs
    )


@bp.route("/<int:doc_id>/update", methods=["POST"])
@login_required
def update(doc_id):
    doc = Document.query.get_or_404(doc_id)
    title = request.form.get("title", "").strip()
    if not title:
        flash("Title cannot be empty", "danger")
        return redirect(url_for("documents.index"))
    doc.title = title
    doc.doc_type = request.form.get("doc_type", doc.doc_type)
    if doc.url is not None:
        doc.url = request.form.get("url", "").strip() or None
    db.session.commit()
    flash(f"Renamed to '{title}'", "success")
    return redirect(url_for("documents.index"))


def _delete_document(doc):
    if doc.file_path:
        full = os.path.join(current_app.config["UPLOAD_FOLDER"], doc.file_path)
        if os.path.exists(full):
            os.remove(full)
    db.session.delete(doc)


@bp.route("/<int:doc_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(doc_id):
    doc = Document.query.get_or_404(doc_id)
    title = doc.title
    _delete_document(doc)
    db.session.commit()
    flash(f"Deleted '{title}'", "success")
    return redirect(url_for("documents.index"))


@bp.route("/bulk-delete", methods=["POST"])
@login_required
@admin_required
def bulk_delete():
    ids = request.form.getlist("doc_ids", type=int)
    docs = Document.query.filter(Document.id.in_(ids)).all() if ids else []
    if not docs:
        flash("Select at least one document to delete", "warning")
        return redirect(url_for("documents.index"))
    for doc in docs:
        _delete_document(doc)
    db.session.commit()
    flash(f"Deleted {len(docs)} document(s)", "success")
    return redirect(url_for("documents.index"))
