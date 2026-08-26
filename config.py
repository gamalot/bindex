import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'stock.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(INSTANCE_DIR, "uploads")
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32MB per request
    ALLOWED_PHOTO_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
    # Deliberately excludes anything the browser would execute in our own
    # origin — .html and .svg can carry script, and these files are served
    # back from the same host.
    ALLOWED_DOC_EXT = {
        # documents
        "pdf", "doc", "docx", "odt", "rtf",
        # spreadsheets
        "xls", "xlsx", "ods", "csv",
        # plain text / markup
        "txt", "md", "markdown", "log",
        # images (drawings, scans, screenshots)
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
        # archives (reference designs, 3D models, project files)
        "zip", "rar", "7z", "gz", "tar",
    }
    THUMBNAIL_SIZE = (320, 320)
    DEFAULT_PAGE_SIZE = 25
