import os
import re
import uuid
import zlib
from decimal import Decimal
from functools import wraps

from flask import abort, current_app
from flask_login import current_user
from markupsafe import Markup
from PIL import Image
from werkzeug.utils import secure_filename


# {key: (label, url template — MPN gets urlencoded and appended)} for the
# "search this MPN online" shortcut on the part detail/edit pages, and the
# default-engine choice in Settings.
SEARCH_ENGINES = {
    "google": ("Google", "https://www.google.com/search?q="),
    "bing": ("Bing", "https://www.bing.com/search?q="),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q="),
    "baidu": ("Baidu", "https://www.baidu.com/s?wd="),
    "yandex": ("Yandex", "https://yandex.com/search/?text="),
}
DEFAULT_SEARCH_ENGINE = "google"

# Document kinds. Injected into every template as `doc_types` (see
# register_context_processors) rather than being retyped per page: these
# labels were once spelled out as an inline Jinja dict on the part detail
# page, again on the part edit page, and again as a literal <option> list,
# so a new kind meant finding four places. Adding five of them here later
# took no template change at all.
DOC_TYPES = {
    "datasheet": "Datasheet",
    "errata": "Errata",
    "appnote": "App Note",
    "user_manual": "User Manual",
    "ref_manual": "Reference Manual",
    "ref_design": "Reference Design",
    "drawing": "Drawing",
    "cert": "Certificate",
    "purchase_link": "Purchase Link",
    "other": "Other",
}

# Bootstrap Icons name per file extension, for the marker shown beside a
# document's title. Only the kinds ALLOWED_DOC_EXT actually accepts are
# listed; anything else falls back to a plain page. Several formats have no
# icon of their own (rtf/odt/ods/log/webp and every archive), so they borrow
# the nearest one that still reads correctly.
FILE_ICONS = {
    "pdf": "filetype-pdf",
    "md": "filetype-md", "markdown": "filetype-md",
    "txt": "filetype-txt", "log": "filetype-txt",
    "doc": "filetype-doc", "docx": "filetype-docx",
    "odt": "filetype-docx", "rtf": "filetype-docx",
    "xls": "filetype-xls", "xlsx": "filetype-xlsx", "ods": "filetype-xlsx",
    "csv": "filetype-csv",
    "png": "filetype-png", "jpg": "filetype-jpg", "jpeg": "filetype-jpg",
    "gif": "filetype-gif", "bmp": "filetype-bmp",
    "tif": "filetype-tiff", "tiff": "filetype-tiff",
    "webp": "file-earmark-image",
    "zip": "file-earmark-zip", "rar": "file-earmark-zip",
    "7z": "file-earmark-zip", "gz": "file-earmark-zip", "tar": "file-earmark-zip",
}


def doc_icon(doc):
    """(icon name, label) for a document — a link, or a file by extension."""
    if getattr(doc, "url", None):
        return "link-45deg", "External link"
    path = getattr(doc, "file_path", None) or ""
    if "." not in path:
        return "file-earmark", "File"
    ext = path.rsplit(".", 1)[-1].lower()
    return FILE_ICONS.get(ext, "file-earmark"), ext.upper()


# Extensions the browser renders in place. Anything else is sent as a
# download instead, so a .docx or .zip doesn't open as a wall of bytes.
INLINE_DOC_EXT = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp", "txt", "md"}

# Served as text/plain so they render rather than download. The stored
# extension is all we have to go on; there's no sniffing.
PLAINTEXT_DOC_EXT = {"txt", "md", "markdown", "csv", "log"}


def home_endpoint():
    """Where a user starts: the dashboard for admins, the parts list for
    everyone else.

    The dashboard is stock totals and category counts — housekeeping, not
    what a non-admin opens the app to do. One helper rather than three
    hardcoded redirects, so the nav brand, the post-login redirect and the
    dashboard's own guard can't disagree about it.
    """
    if current_user.is_authenticated and current_user.is_admin:
        return "main.dashboard"
    return "parts.index"


def format_attr_name(name):
    """Render an attribute name's backslash subscript notation as HTML: a
    backslash marks everything after it as a subscript, e.g. "V\\C\\E\\O"
    (however many backslash-separated pieces) collapses to "V<sub>CEO</sub>"
    — the datasheet convention for V_CEO. Names without a backslash render
    as plain (escaped) text. Markup.format escapes its arguments, so this
    is safe against attribute names containing HTML-special characters."""
    if not name:
        return Markup("")
    if "\\" not in name:
        return Markup.escape(name)
    base, *subs = name.split("\\")
    return Markup("{}<sub>{}</sub>").format(base, "".join(subs))


def format_attr_label(ad):
    """format_attr_name(ad.name), plus a trailing " (unit)" when the
    attribute def has one — the combined label used on filter/form fields."""
    label = format_attr_name(ad.name)
    if ad.unit:
        label = Markup("{} ({})").format(label, ad.unit)
    return label


def format_number(v):
    """Render a float without trailing zeros and — unlike '%g' — without ever
    switching to scientific notation (e.g. a 1e6 ohm resistor should read
    "1000000", not "1e+06"). Goes through Decimal(repr(v)) rather than a
    fixed-precision f-string: a fixed precision either truncates picofarad-scale
    values to "0" (too few decimal places) or prints float64 binary-rounding
    noise like "3.56999999999999984013" for ordinary values (too many) —
    repr(v) is Python's shortest string that round-trips to the same float,
    so Decimal-izing that instead sidesteps both failure modes."""
    if v == int(v):
        return str(int(v))
    s = format(Decimal(repr(v)), "f")
    return s.rstrip("0").rstrip(".")


def format_number_commas(v, unit="", sep=" "):
    """format_number, plus comma thousands separators (750000 -> "750,000").
    If `unit` is given it's appended (750 -> "750 W" with the default sep,
    or "750W" with sep="" for a compact fused style)."""
    s = format_number(v)
    sign = "-" if s.startswith("-") else ""
    s = s[1:] if sign else s
    int_part, _, dec_part = s.partition(".")
    int_part = f"{int(int_part):,}"
    out = sign + int_part + (f".{dec_part}" if dec_part else "")
    return f"{out}{sep}{unit}" if unit else out


_MEDIABOX_RE = re.compile(
    rb"/MediaBox\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s*\]"
)


def _mediabox_ratio(data):
    m = _MEDIABOX_RE.search(data)
    if not m:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    w, h = abs(x1 - x0), abs(y1 - y0)
    if w <= 0 or h <= 0:
        return None
    return round(h / w, 4)


def pdf_page_ratio(path):
    """Height/width of a PDF's first page, or None if it can't be read.

    Used to size the preview pane to the document actually being shown —
    the collection is split roughly evenly between US Letter (1.294) and
    A4 (1.414), and a pane sized for one wastes width or clips the other.

    No PDF library needed: /MediaBox sits in the page object, which is
    plain text in most files. PDF 1.5+ can pack page objects into
    compressed object streams, so if the plain scan misses we inflate the
    FlateDecode streams and look again — across a real 44-document set
    that combination found every one, 41 on the first pass.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None

    ratio = _mediabox_ratio(raw)
    if ratio is not None:
        return ratio

    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        try:
            inflated = zlib.decompress(raw[start:end])
        except zlib.error:
            continue
        ratio = _mediabox_ratio(inflated)
        if ratio is not None:
            return ratio
    return None


def format_qty(v):
    """A stock quantity for display: comma-separated, "-" when unknown.

    Exists so quantities can't drift between screens the way they had —
    the part detail page printed a bare `{{ part.total_quantity }}` while
    the stock table right below it comma-separated the same kind of
    number, so one card read "12500" and the next "12,500"."""
    if v is None:
        return "-"
    return f"{int(v):,}"


def format_price(v, currency=None):
    """A unit price for display: two decimals, optional currency suffix.

    The detail page used to print prices at 4 decimals while the parts
    list used 2, for the same underlying value. Two is what a price is
    read at; the extra precision is still stored, just not shown."""
    if v is None:
        return "-"
    s = f"{v:,.2f}"
    return f"{s} {currency}" if currency else s


_SI_PREFIXES = [
    (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"),
    (1, ""),
    (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"),
]


def format_engineering(v, unit="", sep=" "):
    """Electronics-style SI-prefixed notation: 750000 -> "750k", 0.01 ->
    "10m", 0.0000001 -> "100n". Picks the largest prefix (in steps of 1000)
    whose mantissa is still >= 1, which is how resistor/capacitor/inductor
    values are conventionally written.

    When `unit` is given, the SI prefix fuses onto the *unit*, not the
    number — 820e-3 with unit "Ω" is "820 mΩ", never "820m Ω". `sep`
    controls the gap between the number and the prefixed unit: " " (the
    default, e.g. for search filters) or "" for a compact fused style like
    "750kΩ" (e.g. the parts-list summary column)."""
    if v == 0:
        mantissa, prefix = "0", ""
    else:
        sign = "-" if v < 0 else ""
        av = abs(v)
        factor, prefix = _SI_PREFIXES[-1]  # smallest (pico) — the floor if av is tinier still
        for f, p in _SI_PREFIXES:
            if av >= f:
                factor, prefix = f, p
                break
        # `av / factor` is itself a float division and can land a hair off
        # the intended mantissa (e.g. 0.47/0.001 -> 469.99999999999994)
        # even when `av` was clean — round to 10 significant figures (far
        # more than any real component spec needs) to clear that noise
        # before formatting, rather than printing it verbatim.
        mantissa = sign + format_number(float(f"{av / factor:.10g}"))
    if not unit:
        return mantissa + prefix
    return f"{mantissa}{sep}{prefix}{unit}"


def collect_descendant_ids(node):
    """Collect a category/location node's own id plus all of its descendants' ids (node needs .id and .children)."""
    ids = [node.id]
    for child in node.children:
        ids.extend(collect_descendant_ids(child))
    return ids


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def allowed_file(filename, allowed_ext):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_ext


def save_upload(file_storage, subdir, allowed_ext):
    """Save an uploaded file under instance/uploads/<subdir>/, returning its relative path (None if disallowed)."""
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename, allowed_ext):
        return None

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    target_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], subdir)
    os.makedirs(target_dir, exist_ok=True)
    full_path = os.path.join(target_dir, safe_name)
    file_storage.save(full_path)
    return f"{subdir}/{safe_name}"


def make_thumbnail(relative_path, box=None):
    """Generate a thumbnail for an image, returning the thumbnail's relative
    path. `box` (x0, y0, x1, y1), if given, crops to that region of the
    original first — used to re-generate a tighter thumbnail around a small
    component instead of shrinking the whole (mostly background) photo."""
    src = os.path.join(current_app.config["UPLOAD_FOLDER"], relative_path)
    folder, filename = os.path.split(relative_path)
    thumb_rel = f"{folder}/thumb_{filename}"
    thumb_full = os.path.join(current_app.config["UPLOAD_FOLDER"], thumb_rel)
    try:
        with Image.open(src) as im:
            if box is not None:
                im = im.crop(box)
            im.thumbnail(current_app.config["THUMBNAIL_SIZE"])
            im.convert("RGB").save(thumb_full)
        return thumb_rel
    except Exception:
        return None
