import os

import click
from flask import Flask

from config import Config
from app.extensions import db, login_manager, csrf


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.main import bp as main_bp
    from app.blueprints.categories import bp as categories_bp
    from app.blueprints.locations import bp as locations_bp
    from app.blueprints.parts import bp as parts_bp
    from app.blueprints.stock import bp as stock_bp
    from app.blueprints.cart import bp as cart_bp
    from app.blueprints.documents import bp as documents_bp
    from app.blueprints.images import bp as images_bp
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.settings import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(categories_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(parts_bp)
    app.register_blueprint(stock_bp)
    app.register_blueprint(cart_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(images_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(settings_bp)

    register_context_processors(app)
    register_cli(app)
    register_template_filters(app)

    return app


def register_template_filters(app):
    from app.utils import doc_icon, format_attr_label, format_attr_name, format_price, format_qty

    app.jinja_env.filters["attr_name"] = format_attr_name
    app.jinja_env.filters["attr_label"] = format_attr_label
    app.jinja_env.filters["qty"] = format_qty
    app.jinja_env.filters["price"] = format_price
    app.jinja_env.filters["doc_icon"] = doc_icon

    # Transaction-type labels were spelled out as an inline dict in both the
    # part detail page and the stock log — same seven keys, written twice.
    TX_TYPE_LABELS = {
        "in": "In",
        "out": "Out",
        "return": "Return",
        "move_in": "Move in",
        "move_out": "Move out",
        "adjust": "Adjust",
        "initial": "Initial",
    }
    app.jinja_env.filters["tx_type"] = lambda t: TX_TYPE_LABELS.get(t, t)

    @app.template_global()
    def home_url():
        from flask import url_for

        from app.utils import home_endpoint

        return url_for(home_endpoint())

    @app.template_global()
    def page_window(page, total_pages, left_edge=2, left_current=2, right_current=4, right_edge=2):
        """Page numbers to show in a pagination bar, with None marking an
        elided gap — the same windowing Flask-SQLAlchemy's own
        `Pagination.iter_pages` does, so the hand-paginated lists (parts,
        documents, images) render an identical bar to the Stock Log, which
        gets it from the real Pagination object.

        Without this a list just printed every page number: at 668 parts
        that was 27 links side by side.
        """
        pages_end = total_pages + 1
        if pages_end == 1:
            return

        left_end = min(1 + left_edge, pages_end)
        yield from range(1, left_end)
        if left_end == pages_end:
            return

        mid_start = max(left_end, page - left_current)
        mid_end = min(page + right_current + 1, pages_end)
        if mid_start - left_end > 0:
            yield None
        yield from range(mid_start, mid_end)
        if mid_end == pages_end:
            return

        right_start = max(mid_end, pages_end - right_edge)
        if right_start - mid_end > 0:
            yield None
        yield from range(right_start, pages_end)

    @app.template_global()
    def asset_version(rel_path):
        """mtime of a static file, appended as a `?v=` query param on its
        <link>/<script> tag so browsers fetch the new version immediately
        after a deploy instead of serving a stale cached copy — static
        asset URLs are otherwise identical across deploys, so nothing else
        tells the browser the file changed."""
        full = os.path.join(app.static_folder, rel_path)
        try:
            return int(os.path.getmtime(full))
        except OSError:
            return 0


def register_context_processors(app):
    from flask_login import current_user

    @app.context_processor
    def inject_theme():
        theme = "system"
        page_size = app.config["DEFAULT_PAGE_SIZE"]
        if current_user.is_authenticated and current_user.setting:
            theme = current_user.setting.theme
            page_size = current_user.setting.page_size
        return {"current_theme": theme, "current_page_size": page_size}

    @app.context_processor
    def inject_cart_count():
        from app.models import CartItem

        count = 0
        if current_user.is_authenticated:
            count = CartItem.query.filter_by(user_id=current_user.id).count()
        return {"cart_count": count}

    @app.context_processor
    def inject_doc_types():
        """Available everywhere so no template has to spell the labels out
        again — the documents blueprint used to be the only page that got
        them, and the three that didn't each kept their own copy."""
        from app.utils import DOC_TYPES

        return {"doc_types": DOC_TYPES}


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create all tables plus the default category tree."""
        from app.models import Category

        with app.app_context():
            db.create_all()
            if Category.query.count() == 0:
                _seed_categories(db)
            db.session.commit()
        click.echo("Database initialized.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--email", default="")
    def create_admin(username, password, email):
        """Create an admin account."""
        from app.models import User, UserSetting

        with app.app_context():
            if User.query.filter_by(username=username).first():
                click.echo("That username already exists.")
                return
            user = User(username=username, role="admin", email=email or None)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            db.session.add(UserSetting(user_id=user.id))
            db.session.commit()
        click.echo(f"Admin account '{username}' created.")

    @app.cli.command("import-excel")
    @click.argument("path", default="Stocks.xlsx")
    @click.option("--dry-run", is_flag=True, help="Only print stats, don't write to the database")
    def import_excel_cmd(path, dry_run):
        """Import stock data from the legacy Excel ledger (see scripts/import_excel.py)."""
        from scripts.import_excel import run_import

        with app.app_context():
            run_import(path, dry_run=dry_run)

    @app.cli.command("reclassify-parts")
    @click.option("--dry-run", is_flag=True, help="Only print stats, don't write to the database")
    @click.option("--force", is_flag=True, help="Also re-guess parts that were already categorized (default: only Uncategorized ones)")
    def reclassify_parts_cmd(dry_run, force):
        """Guess a category for 'Uncategorized' parts based on Description keywords (see scripts/classify.py)."""
        from scripts.classify import run_reclassify

        with app.app_context():
            run_reclassify(dry_run=dry_run, force=force)

    @app.cli.command("fetch-lcsc-photos")
    @click.option("--currency", default="USD", help="Only fetch photos for parts priced in this currency (empty string for all parts)")
    @click.option("--limit", type=int, default=None, help="Only process the first N matching parts (for a quick test run)")
    @click.option("--force", is_flag=True, help="Also process parts that already have at least one photo")
    @click.option("--dry-run", is_flag=True, help="Only report matches, don't download or write anything")
    def fetch_lcsc_photos_cmd(currency, limit, force, dry_run):
        """Look up parts on LCSC by MPN and attach their product photos (see scripts/fetch_lcsc_photos.py)."""
        from scripts.fetch_lcsc_photos import run_fetch

        with app.app_context():
            run_fetch(currency=currency or None, limit=limit, force=force, dry_run=dry_run)

    @app.cli.command("import-lcsc")
    @click.argument("paths", nargs=-1, required=True)
    @click.option("--skip-order", multiple=True, help="Order number (WM...) to leave out entirely, e.g. one already in stock")
    @click.option("--exclude", multiple=True, help="LCSC part number (C...) to leave out, e.g. something that isn't inventory")
    @click.option("--location", default="Unshelved", show_default=True, help="Top-level staging location the new batches go into (created if missing)")
    @click.option("--operator", default=None, help="Username to record as the operator on the stock-in transactions")
    @click.option("--no-photos", is_flag=True, help="Don't attach LCSC product photos")
    @click.option("--no-datasheets", is_flag=True, help="Don't download and attach LCSC datasheet PDFs")
    @click.option("--report", type=click.Path(dir_okay=False), default=None, help="Write a per-row CSV of what was (or would be) done")
    @click.option("--dry-run", is_flag=True, help="Fetch from LCSC and report, but don't write to the database or download photos")
    def import_lcsc_cmd(paths, skip_order, exclude, location, operator, no_photos, no_datasheets, report, dry_run):
        """Import parts and stock from LCSC order CSV exports (see scripts/import_lcsc.py)."""
        from scripts.import_lcsc import run_import

        with app.app_context():
            run_import(
                paths, skip_orders=set(skip_order), exclude=set(exclude), location_name=location,
                operator=operator, photos=not no_photos, datasheets=not no_datasheets, report_path=report,
                dry_run=dry_run,
            )

    @app.cli.command("update-exchange-rates")
    @click.option("--force", is_flag=True, help="Refresh even if the cached rates aren't stale yet")
    def update_exchange_rates_cmd(force):
        """Refresh currency-to-AUD rates from the ECB daily feed (see app/exchange_rates.py).

        Safe to run from a daily cron job; a failed fetch just leaves the
        previously cached rates in place.
        """
        from app.exchange_rates import ensure_fresh_rates, get_rate_map

        with app.app_context():
            updated = ensure_fresh_rates(force=force, logger=app.logger)
            if updated:
                click.echo(f"Exchange rates updated: {get_rate_map()}")
            else:
                click.echo("Exchange rates not updated (either still fresh, or the fetch failed — see warnings above).")


def _seed_categories(db):
    from app.models import Category

    tree = [
        ("Passive Components", [
            ("Resistors", []),
            ("Capacitors", []),
            ("Inductors & Ferrite", []),
            ("Crystals & Oscillators", []),
            ("Transformers", []),
        ]),
        ("Discrete Semiconductors", [
            ("Diodes", []),
            ("Transistors", []),
            ("Thyristors", []),
        ]),
        ("Integrated Circuits", [
            ("Op-Amps & Comparators", []),
            ("Voltage Reference & Detector", []),
            ("Linear Regulators", []),
            ("DC-DC / SMPS", []),
            ("Microcontrollers", []),
            ("Logic", []),
            ("Memory", []),
            ("Interface", []),
        ]),
        ("Optoelectronics", [
            ("LED", []),
            ("Optocouplers", []),
            ("Photodiodes/Phototransistors", []),
        ]),
        ("Electromechanical", [
            ("Switches", []),
            ("Relays", []),
            ("Connectors", []),
        ]),
        ("Sensors", []),
        ("Tools & Supplies", []),
        ("Uncategorized", []),
    ]

    def add_nodes(nodes, parent, order_start=0):
        for i, (name, children) in enumerate(nodes):
            node = Category(name=name, sort_order=order_start + i, parent=parent)
            db.session.add(node)
            db.session.flush()
            add_nodes(children, node)

    add_nodes(tree, None)
