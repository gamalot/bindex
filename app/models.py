import json
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# User / permissions / personal settings
# ---------------------------------------------------------------------------
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="user")  # admin / user
    email = db.Column(db.String(255))
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    setting = db.relationship(
        "UserSetting", uselist=False, back_populates="user", cascade="all, delete-orphan"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_admin(self):
        return self.role == "admin"

    # Flask-Login uses `is_active` as a property; we store the column under a
    # different attribute name to avoid clashing with UserMixin.
    @property
    def is_active(self):
        return self.is_active_flag

    def __repr__(self):
        return f"<User {self.username}>"


class UserSetting(db.Model):
    __tablename__ = "user_settings"

    # Every parts-list column except MPN and Stock (Qty) can be hidden —
    # those two are the only ones guaranteed useful regardless of what
    # you're looking at, everything else is a per-user preference.
    LIST_COLUMNS = [
        "thumbnail", "location", "manufacturer", "package", "marking",
        "category", "characteristics", "description", "unit_price",
    ]

    # Options for the Documents page's own "per page" choice (kept separate
    # from the Parts list's `page_size` column so the two lists don't fight
    # over one preference), and the two ways it can preview a document.
    DOC_PAGE_SIZE_OPTIONS = (25, 50, 100, 200)
    DOC_PREVIEW_MODES = ("panel", "modal")

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    theme = db.Column(db.String(16), nullable=False, default="system")  # light/dark/system
    page_size = db.Column(db.Integer, nullable=False, default=25)
    # Target currency the parts-list unit-price range filter converts every
    # part's price into for comparison (see Part.display_price and
    # app.exchange_rates) — not a "only show parts priced in X" filter.
    default_price_currency = db.Column(db.String(8), nullable=False, default="AUD")
    # Which engine the "search this MPN online" shortcut on the part detail/
    # edit pages opens — a key into app.utils.SEARCH_ENGINES.
    default_search_engine = db.Column(db.String(16), nullable=False, default="google")
    prefs_json = db.Column(db.Text, nullable=False, default="{}")

    user = db.relationship("User", back_populates="setting")

    @property
    def visible_columns(self):
        """Which optional parts-list columns to show — a set of keys from
        LIST_COLUMNS. Stored inside prefs_json rather than its own column
        since it's just one of potentially several free-form UI prefs.
        Unset (the common case — nobody's touched this yet) means "show
        everything", not "show nothing"."""
        try:
            stored = json.loads(self.prefs_json or "{}").get("visible_columns")
        except (ValueError, TypeError):
            stored = None
        if stored is None:
            return set(self.LIST_COLUMNS)
        return set(stored) & set(self.LIST_COLUMNS)

    @visible_columns.setter
    def visible_columns(self, columns):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["visible_columns"] = [c for c in self.LIST_COLUMNS if c in columns]
        self.prefs_json = json.dumps(prefs)

    @property
    def doc_page_size(self):
        try:
            stored = json.loads(self.prefs_json or "{}").get("doc_page_size")
        except (ValueError, TypeError):
            stored = None
        return stored if stored in self.DOC_PAGE_SIZE_OPTIONS else self.DOC_PAGE_SIZE_OPTIONS[1]

    @doc_page_size.setter
    def doc_page_size(self, value):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["doc_page_size"] = value if value in self.DOC_PAGE_SIZE_OPTIONS else self.DOC_PAGE_SIZE_OPTIONS[1]
        self.prefs_json = json.dumps(prefs)

    @property
    def doc_preview_mode(self):
        try:
            stored = json.loads(self.prefs_json or "{}").get("doc_preview_mode")
        except (ValueError, TypeError):
            stored = None
        return stored if stored in self.DOC_PREVIEW_MODES else self.DOC_PREVIEW_MODES[0]

    @doc_preview_mode.setter
    def doc_preview_mode(self, value):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["doc_preview_mode"] = value if value in self.DOC_PREVIEW_MODES else self.DOC_PREVIEW_MODES[0]
        self.prefs_json = json.dumps(prefs)

    # Same per-page / preview-mode preferences as the Documents page, kept
    # under separate prefs_json keys so the two lists don't share one choice.
    @property
    def img_page_size(self):
        try:
            stored = json.loads(self.prefs_json or "{}").get("img_page_size")
        except (ValueError, TypeError):
            stored = None
        return stored if stored in self.DOC_PAGE_SIZE_OPTIONS else self.DOC_PAGE_SIZE_OPTIONS[1]

    @img_page_size.setter
    def img_page_size(self, value):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["img_page_size"] = value if value in self.DOC_PAGE_SIZE_OPTIONS else self.DOC_PAGE_SIZE_OPTIONS[1]
        self.prefs_json = json.dumps(prefs)

    @property
    def img_preview_mode(self):
        try:
            stored = json.loads(self.prefs_json or "{}").get("img_preview_mode")
        except (ValueError, TypeError):
            stored = None
        return stored if stored in self.DOC_PREVIEW_MODES else self.DOC_PREVIEW_MODES[0]

    @img_preview_mode.setter
    def img_preview_mode(self, value):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["img_preview_mode"] = value if value in self.DOC_PREVIEW_MODES else self.DOC_PREVIEW_MODES[0]
        self.prefs_json = json.dumps(prefs)

    # Currency last picked on the New Part form, so the next new part
    # starts on it. Parts get added in runs from one supplier's order, so
    # the previous choice predicts the next one far better than a fixed
    # default does. Distinct from default_price_currency above, which is
    # the target the parts-list price filter converts *into*.
    @property
    def last_part_currency(self):
        try:
            return json.loads(self.prefs_json or "{}").get("last_part_currency")
        except (ValueError, TypeError):
            return None

    @last_part_currency.setter
    def last_part_currency(self, value):
        try:
            prefs = json.loads(self.prefs_json or "{}")
        except (ValueError, TypeError):
            prefs = {}
        prefs["last_part_currency"] = value
        self.prefs_json = json.dumps(prefs)


# ---------------------------------------------------------------------------
# Category tree (logical classification, modeled after DigiKey / Mouser / Element14)
# ---------------------------------------------------------------------------
class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True, index=True)
    name = db.Column(db.String(128), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    children = db.relationship(
        "Category",
        backref=db.backref("parent", remote_side=[id]),
        order_by="Category.sort_order",
        cascade="all, delete-orphan",
        single_parent=True,
    )
    attribute_defs = db.relationship(
        "AttributeDef", back_populates="category", cascade="all, delete-orphan",
        order_by="AttributeDef.sort_order",
    )
    parts = db.relationship("Part", back_populates="category")
    attribute_order_overrides = db.relationship(
        "CategoryAttributeOrder", back_populates="category", cascade="all, delete-orphan",
        foreign_keys="CategoryAttributeOrder.category_id",
    )

    def ancestors(self):
        node, chain = self.parent, []
        while node is not None:
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def path_name(self, sep=" / "):
        return sep.join([a.name for a in self.ancestors()] + [self.name])

    def _ordered_attribute_defs(self, exclude_hidden):
        """Shared implementation behind effective_attribute_defs and
        all_attribute_defs — same own+inherited collection and per-category
        position-override logic, differing only in whether attributes this
        category has hidden are dropped."""
        defs = []
        for cat in self.ancestors() + [self]:
            defs.extend(cat.attribute_defs)
        if exclude_hidden:
            hidden_ids = {o.attribute_def_id for o in self.attribute_order_overrides if o.hidden}
            defs = [ad for ad in defs if ad.id not in hidden_ids]
        overrides = {
            o.attribute_def_id: o.position
            for o in self.attribute_order_overrides
            if o.position is not None
        }
        if not overrides:
            return defs
        return [
            ad for _, ad in sorted(
                enumerate(defs),
                key=lambda pair: overrides.get(pair[1].id, len(defs) + pair[0]),
            )
        ]

    @property
    def effective_attribute_defs(self):
        """This category's own parametric attributes plus everything
        inherited from ancestor categories (root-first) — so a subcategory
        (e.g. "Chip Resistors") automatically picks up attributes defined on
        a parent (e.g. "Resistance"/"Power" on "Resistors") without having
        to redefine them.

        The default order is ancestors-first then own (each internally in
        that owning category's sort_order). A category can override this
        with its own full custom order — including repositioning inherited
        attributes — via CategoryAttributeOrder; attributes with no explicit
        override keep their relative default order, pushed after any that
        do (so newly-added attributes just land at the end until dragged).

        An inherited attribute this category has hidden (see is_attr_hidden)
        is left out entirely — this is what every part-facing view (detail/
        edit page, search facets, list Characteristics column) uses, so a
        hidden attribute simply doesn't exist as far as this category's
        parts are concerned."""
        return self._ordered_attribute_defs(exclude_hidden=True)

    @property
    def all_attribute_defs(self):
        """Same as effective_attribute_defs but keeps attributes this
        category has hidden — used only by the attribute-management page,
        so a hidden one is still listed there (with its hidden state shown)
        instead of becoming unreachable to un-hide."""
        return self._ordered_attribute_defs(exclude_hidden=False)

    def is_attr_hidden(self, ad):
        """Whether this category has hidden an inherited attribute `ad` —
        per-category, like is_attr_searchable: doesn't touch the attribute
        itself or any other category that also inherits it."""
        override = next(
            (o for o in self.attribute_order_overrides if o.attribute_def_id == ad.id),
            None,
        )
        return bool(override and override.hidden)

    def is_attr_searchable(self, ad):
        """Whether attribute `ad` should show as a search filter for this
        specific category. Checks this category's own override first — lets
        a subcategory opt an inherited attribute in/out of search without
        touching the attribute's global default (AttributeDef.searchable)
        or affecting any other category, ancestor or sibling, that also
        inherits the same attribute — falling back to that default when
        this category has no override of its own."""
        override = next(
            (o for o in self.attribute_order_overrides if o.attribute_def_id == ad.id),
            None,
        )
        if override is not None and override.searchable_override is not None:
            return override.searchable_override
        return ad.searchable

    @property
    def total_part_count(self):
        """Parts filed on this category plus everywhere under it, so a
        parent node's count reflects its whole subtree rather than only
        what's filed directly against it.

        Walks the relationship lazily, which costs a query per node — fine
        for ~60 categories. The location tree needed the same numbers over
        ~750 nodes and had to be rewritten as bulk loads; see app/trees.py
        if this ever grows enough to hurt."""
        count = len(self.parts)
        for child in self.children:
            count += child.total_part_count
        return count

    @property
    def has_empty_subtree(self):
        """True for a leaf category with no parts, or for any ancestor with
        such a leaf somewhere beneath it. This is what the "show only empty"
        tree filter keeps visible, so a branch stays on screen whenever it
        leads to an unused category — the locations tree computes the same
        flag in app/trees.py."""
        if not self.children:
            return self.total_part_count == 0
        return any(child.has_empty_subtree for child in self.children)

    def __repr__(self):
        return f"<Category {self.name}>"


class AttributeDef(db.Model):
    """A category can define parametric attributes (modeled after DigiKey's parametric filters)."""

    __tablename__ = "attribute_defs"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    unit = db.Column(db.String(32))
    # text/number/enum/engineering. "number" and "engineering" both store a
    # comparable float (value_number) for range/facet filtering; they only
    # differ in how the value is *displayed* — "number" gets comma thousands
    # separators (750000 -> "750,000"), "engineering" gets SI-prefix notation
    # (750000 -> "750k"), which suits resistance/capacitance-style values.
    data_type = db.Column(db.String(16), nullable=False, default="text")
    enum_options = db.Column(db.Text)  # comma-separated, used when data_type=enum
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    # Whether this attribute shows up as a filter on the parts search page.
    # Always still shown/editable on the part detail/edit page regardless —
    # this only controls the search filter row.
    searchable = db.Column(db.Boolean, nullable=False, default=True)

    category = db.relationship("Category", back_populates="attribute_defs")
    values = db.relationship(
        "PartAttributeValue", back_populates="attribute_def", cascade="all, delete-orphan"
    )
    order_overrides = db.relationship(
        "CategoryAttributeOrder", back_populates="attribute_def", cascade="all, delete-orphan",
        foreign_keys="CategoryAttributeOrder.attribute_def_id",
    )


class CategoryAttributeOrder(db.Model):
    """A category's own override(s) for one attribute it either owns or
    merely inherits — its position within effective_attribute_defs
    (see Category.effective_attribute_defs), whether it shows up as a
    search filter for this specific category (see Category.is_attr_searchable),
    and/or whether it's hidden from this category's parts entirely (see
    Category.is_attr_hidden — typically used to drop an inherited attribute
    that doesn't make sense for this particular subcategory). Each field can
    be set independently of the others (position is None until the row has
    actually been dragged; searchable_override is None until explicitly
    toggled; hidden defaults False) — a category can override just one
    without the others. Only exists for a (category, attribute) pair an
    admin has actually customized."""

    __tablename__ = "category_attribute_order"
    __table_args__ = (
        db.UniqueConstraint("category_id", "attribute_def_id", name="uq_cat_attr_order"),
    )

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    attribute_def_id = db.Column(db.Integer, db.ForeignKey("attribute_defs.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=True)
    searchable_override = db.Column(db.Boolean, nullable=True)
    hidden = db.Column(db.Boolean, nullable=False, default=False)

    category = db.relationship("Category", back_populates="attribute_order_overrides")
    attribute_def = db.relationship("AttributeDef", back_populates="order_overrides")


# ---------------------------------------------------------------------------
# Storage location tree (physical location, independent of the category tree)
# ---------------------------------------------------------------------------
class Location(db.Model):
    __tablename__ = "locations"

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=True, index=True)
    code = db.Column(db.String(64))  # e.g. BK01-01-01
    name = db.Column(db.String(128), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    children = db.relationship(
        "Location",
        backref=db.backref("parent", remote_side=[id]),
        order_by="Location.sort_order",
        cascade="all, delete-orphan",
        single_parent=True,
    )
    lots = db.relationship("Lot", back_populates="location")

    def ancestors(self):
        node, chain = self.parent, []
        while node is not None:
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def path_name(self):
        """Just this location's own code/name, not a joined ancestor
        breadcrumb — the naming convention here always bakes the full
        hierarchy into the leaf's own code already (e.g. "BK03-06-09" is
        self-explanatory on its own), so re-joining ancestor names on top of
        that would just repeat it (e.g. "BK03 / BK03-06 / BK03-06-09")."""
        return self.code or self.name

    def __repr__(self):
        return f"<Location {self.code or self.name}>"


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------
class Part(db.Model):
    __tablename__ = "parts"
    __table_args__ = (
        db.UniqueConstraint("manufacturer", "mpn", name="uq_part_manufacturer_mpn"),
    )

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True, index=True)
    manufacturer = db.Column(db.String(128))
    mpn = db.Column(db.String(128), nullable=False, index=True)  # part number / manufacturer part number
    package = db.Column(db.String(64))
    marking = db.Column(db.String(64))
    description = db.Column(db.String(512))
    is_suspect_fake = db.Column(db.Boolean, nullable=False, default=False)
    default_unit_price = db.Column(db.Float)
    currency = db.Column(db.String(8), default="CNY")
    min_stock_alert = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    category = db.relationship("Category", back_populates="parts")
    attribute_values = db.relationship(
        "PartAttributeValue", back_populates="part", cascade="all, delete-orphan"
    )
    lots = db.relationship(
        "Lot", back_populates="part", cascade="all, delete-orphan",
        order_by="Lot.received_at",
    )
    transactions = db.relationship(
        "StockTransaction", back_populates="part", cascade="all, delete-orphan",
        order_by="StockTransaction.created_at.desc()",
    )
    # A Photo can be shared across parts (e.g. the same reel/tape photo for
    # several resistance variants) — like documents, the link itself (and
    # which shared photo is this part's own cover) lives on PartPhoto, not
    # on Photo. `photo_links` is the writable relationship; `photos` below
    # is a convenience read-only view in cover-first order.
    photo_links = db.relationship(
        "PartPhoto", back_populates="part", cascade="all, delete-orphan",
    )
    documents = db.relationship(
        "Document", secondary="part_documents", back_populates="parts"
    )

    @property
    def photos(self):
        ordered = sorted(
            self.photo_links,
            key=lambda link: (not link.is_primary, link.photo.uploaded_at or utcnow()),
        )
        return [link.photo for link in ordered]

    @property
    def active_lots(self):
        return [lot for lot in self.lots if lot.quantity_remaining > 0]

    @property
    def returnable_lots(self):
        """Lots with something outstanding (taken out but not yet returned),
        including fully-depleted ones — a return targets the original batch,
        not just whichever ones still show stock."""
        return [lot for lot in self.lots if lot.quantity_remaining < lot.quantity_received]

    @property
    def total_quantity(self):
        return sum(lot.quantity_remaining for lot in self.lots)

    @property
    def location_codes(self):
        """Distinct locations currently holding stock, sorted — the parts-list
        Location column shows just the first (plus a "+N more" badge) rather
        than the full comma-joined list, which gets unwieldy once a part's
        stock is split across several places."""
        return sorted({lot.location.path_name() for lot in self.active_lots})

    @property
    def batch_count(self):
        return len(self.active_lots)

    @property
    def display_price(self):
        """(amount, currency) to show as the part's reference price: the most
        recently received active batch if there is one, else the manually-set
        default (used before any purchase has been recorded)."""
        priced_lots = [lot for lot in self.active_lots if lot.unit_price is not None]
        if priced_lots:
            latest = max(priced_lots, key=lambda lot: lot.received_at)
            return latest.unit_price, latest.currency
        if self.default_unit_price is not None:
            return self.default_unit_price, self.currency
        return None, None

    @property
    def attribute_values_by_def(self):
        return {v.attribute_def_id: v for v in self.attribute_values}

    @staticmethod
    def format_attr_piece(ad, v):
        """The formatted text for one (attribute def, value) pair as it
        appears in attr_summary — factored out so a caller comparing this
        piece across many parts (e.g. to detect a column-wide constant
        value worth collapsing out of the list view) formats it identically."""
        from app.utils import format_engineering, format_number_commas

        if ad.data_type in ("number", "engineering") and v.value_number is not None:
            fmt = format_engineering if ad.data_type == "engineering" else format_number_commas
            return fmt(v.value_number, ad.unit or "", sep="")
        text = v.value_text
        if text and ad.unit:
            text += ad.unit
        return text

    def attr_summary(self, exclude_ids=None):
        """Compact comma-joined summary of this part's spec attributes (e.g.
        "10k, 1%, 0.25W") in the category's defined attribute order — the
        parts-list column equivalent of a datasheet headline spec string.
        `exclude_ids` (attribute_def_id set) skips attributes the caller has
        determined carry no distinguishing information for the current list
        (e.g. every part in the current search result has the same value)."""
        if not self.category:
            return None
        exclude_ids = exclude_ids or ()
        values_by_def = self.attribute_values_by_def
        pieces = []
        for ad in self.category.effective_attribute_defs:
            if ad.id in exclude_ids:
                continue
            v = values_by_def.get(ad.id)
            if v is None:
                continue
            text = self.format_attr_piece(ad, v)
            if not text:
                continue
            pieces.append(text)
        return ", ".join(pieces) if pieces else None

    def __repr__(self):
        return f"<Part {self.mpn}>"


class PartAttributeValue(db.Model):
    __tablename__ = "part_attribute_values"
    __table_args__ = (
        db.UniqueConstraint("part_id", "attribute_def_id", name="uq_part_attr"),
    )

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False, index=True)
    attribute_def_id = db.Column(
        db.Integer, db.ForeignKey("attribute_defs.id"), nullable=False, index=True
    )
    value_text = db.Column(db.String(255))
    value_number = db.Column(db.Float)  # kept in sync when data_type=number, for range queries

    part = db.relationship("Part", back_populates="attribute_values")
    attribute_def = db.relationship("AttributeDef", back_populates="values")


# ---------------------------------------------------------------------------
# Stock: tracked as purchase batches ("lots"), not a single aggregate number.
# Two batches of the same part at different prices/currencies stay distinct
# instead of collapsing into one quantity + one price.
# ---------------------------------------------------------------------------
class Lot(db.Model):
    """One purchase batch of a part sitting at one location.

    `quantity_received` is the immutable original batch size; `quantity_remaining`
    is decremented as stock is taken out / moved / adjusted away, using FIFO
    (oldest `received_at` first) across a part's lots at a location. A lot with
    `quantity_remaining == 0` is kept (not deleted) so batch/price history stays
    visible.
    """

    __tablename__ = "lots"

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    quantity_received = db.Column(db.Integer, nullable=False)
    quantity_remaining = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float)
    currency = db.Column(db.String(8))
    received_at = db.Column(db.DateTime, default=utcnow, index=True)
    note = db.Column(db.String(255))

    part = db.relationship("Part", back_populates="lots")
    location = db.relationship("Location", back_populates="lots")
    cart_items = db.relationship("CartItem", back_populates="lot", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Lot part={self.part_id} loc={self.location_id} remaining={self.quantity_remaining}>"


class StockTransaction(db.Model):
    """Stock in/out transaction log. Every stock change must go through here for traceability."""

    __tablename__ = "stock_transactions"

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id"), nullable=True, index=True)
    type = db.Column(db.String(16), nullable=False)  # in / out / adjust / move_in / move_out / initial
    quantity_delta = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float)
    currency = db.Column(db.String(8))
    reason = db.Column(db.String(255))
    ref_no = db.Column(db.String(64))
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    part = db.relationship("Part", back_populates="transactions")
    location = db.relationship("Location")
    lot = db.relationship("Lot")
    operator = db.relationship("User")


class CartItem(db.Model):
    """One line in a user's stock-out shopping cart — a specific batch
    (never auto-picked FIFO, same "you choose the exact batch" rule as a
    direct stock-out, since different batches of the same part can differ in
    quality/provenance and the batch matters for what it's being used for)
    plus how many of it they intend to take out. Persists per-user (not
    session-based) so it survives across logins/devices, and one cart per
    user — no separate "cart" header row needed."""

    __tablename__ = "cart_items"
    __table_args__ = (
        db.UniqueConstraint("user_id", "lot_id", name="uq_cart_user_lot"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("lots.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False)
    added_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship("User")
    lot = db.relationship("Lot", back_populates="cart_items")


# ---------------------------------------------------------------------------
# Attachments: photos (private to a part) / documents (can be shared across parts)
# ---------------------------------------------------------------------------
class Photo(db.Model):
    """An uploaded/fetched photo — can be linked to more than one Part (see
    PartPhoto), sharing its file and crop_box across all of them, the same
    way a Document can be shared."""

    __tablename__ = "photos"

    id = db.Column(db.Integer, primary_key=True)
    file_path = db.Column(db.String(512), nullable=False)  # relative to UPLOAD_FOLDER
    thumbnail_path = db.Column(db.String(512))
    # "x0,y0,x1,y1" in the original photo's pixel coordinates, set once a
    # user confirms a thumbnail crop — re-opening the crop tool then shows
    # this instead of re-running the auto-suggestion (see app/smart_crop.py).
    # Shared by every part linked to this photo, same as the file itself.
    crop_box = db.Column(db.String(64))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=utcnow)

    part_links = db.relationship("PartPhoto", back_populates="photo", cascade="all, delete-orphan")

    @property
    def parts(self):
        return [link.part for link in self.part_links]

    @property
    def crop_box_tuple(self):
        if not self.crop_box:
            return None
        try:
            parts = [int(p) for p in self.crop_box.split(",")]
        except ValueError:
            return None
        return tuple(parts) if len(parts) == 4 else None


class PartPhoto(db.Model):
    """Link between a Part and a shared Photo, mirroring part_documents —
    but a Photo also has a per-part "is this my cover photo" flag that a
    plain association table can't hold, so this is a full model rather than
    a bare db.Table."""

    __tablename__ = "part_photos"
    __table_args__ = (
        db.UniqueConstraint("part_id", "photo_id", name="uq_part_photo"),
    )

    id = db.Column(db.Integer, primary_key=True)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=False, index=True)
    photo_id = db.Column(db.Integer, db.ForeignKey("photos.id"), nullable=False, index=True)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    part = db.relationship("Part", back_populates="photo_links")
    photo = db.relationship("Photo", back_populates="part_links")


part_documents = db.Table(
    "part_documents",
    db.Column("part_id", db.Integer, db.ForeignKey("parts.id"), primary_key=True),
    db.Column("document_id", db.Integer, db.ForeignKey("documents.id"), primary_key=True),
)


class Document(db.Model):
    """Document/resource (datasheet, purchase link, etc.) that can be shared by multiple parts."""

    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    doc_type = db.Column(db.String(32), nullable=False, default="other")
    # datasheet / appnote / cert / purchase_link / other
    url = db.Column(db.String(1024))
    file_path = db.Column(db.String(512))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=utcnow)
    # First page's height/width, so the preview pane can be sized to the
    # document rather than to a guess. Read once from the PDF and cached
    # here — extracting it means scanning (and sometimes inflating) the
    # whole file, which is too slow to redo per page view.
    page_ratio = db.Column(db.Float)

    parts = db.relationship("Part", secondary=part_documents, back_populates="documents")


# ---------------------------------------------------------------------------
# Exchange rates (used only to normalize prices for sorting across currencies)
# ---------------------------------------------------------------------------
class ExchangeRate(db.Model):
    """Cached conversion rate for one currency into AUD, refreshed from the ECB daily feed."""

    __tablename__ = "exchange_rates"

    id = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.String(8), unique=True, nullable=False)
    rate_to_aud = db.Column(db.Float, nullable=False)  # 1 unit of `currency` = rate_to_aud AUD
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<ExchangeRate {self.currency}->AUD {self.rate_to_aud}>"
