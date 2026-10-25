"""
Import parts and stock from LCSC order exports — the per-order CSV that
My Orders → Export produces (LCSC__WM2608160130_<timestamp>.csv).

Each CSV row becomes one new stock batch, plus whatever the part itself is
missing:
  - Part: matched on MPN alone (case-insensitive), never MPN+manufacturer —
    LCSC spells brands its own way ("KOA", "DIODES") where this inventory
    says "KOA Speer" / "Diodes", so a pair match would quietly duplicate
    parts that already exist. Unmatched rows create a new part, with the
    manufacturer run through MANUFACTURER_ALIASES and the package through
    normalize_package so they line up with the existing spellings.
  - Category: LCSC's own catalog for the part (from its product page),
    mapped onto this category tree by CATEGORY_MAP. Existing parts keep
    theirs.
  - Parameters: LCSC's parametric table mapped onto the category's
    attribute defs by ATTRIBUTE_SOURCES. Only attributes the part has no
    value for yet are filled, so hand-entered values are never touched.
  - Photos: LCSC's product photos, only for a part that has none yet. No
    thumbnail crop is applied (app/smart_crop.py isn't meant to run
    unattended); re-crop from the photo's edit dialog if wanted.
  - Datasheet: the PDF LCSC links from the product page, downloaded and
    attached as a "datasheet" document, only for a part that has none yet.
    Parts whose listings point at the same PDF share one document.
  - Stock: a new lot in a staging location (created if missing) to move to
    a real bin when shelved, received on the order date, with the order
    number as the transaction's ref no. and the LCSC code as the lot note.
    That pair is also how a re-run recognizes rows it already imported.

Usage:
  flask import-lcsc orders/ --location 未上架 --dry-run --report review.csv
  flask import-lcsc orders/ --location 未上架 --skip-order WM2508270026 --exclude C50198518
"""
import csv
import glob
import math
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import click
from flask import current_app

from app.extensions import db
from app.lcsc import (
    LCSC_ERRORS, download_lcsc_datasheet, download_lcsc_image, fetch_product, product_images,
)
from app.models import (
    Category, Document, Location, Lot, Part, PartAttributeValue, PartPhoto, Photo, StockTransaction, User,
)
from app.utils import format_number, make_thumbnail, pdf_page_ratio

ORDER_RE = re.compile(r"WM(\d{2})(\d{2})(\d{2})\d+")

# LCSC brand name -> the spelling this inventory already uses.
MANUFACTURER_ALIASES = {
    "ALPSALPINE": "Alps",
    "BOURNS": "Bourns",
    "DIODES": "Diodes",
    "EPSON": "Epson",
    "HANRUN(Zhongshan HanRun Elec)": "HanRun",
    "Infineon/CYPRESS": "Infineon Cypress",
    "Johanson Dielectrics": "Johanson",
    "KOA": "KOA Speer",
    "PANASONIC": "Panasonic",
    "ROHM": "Rohm",
    "Samsung Electro-Mechanics": "Samsung",
    "TOREX": "Torex",
    "TOSHIBA": "Toshiba",
    "VISHAY": "Vishay",
    "Worldsemi": "WorldSemi",
}

# LCSC catalog name -> category path in this tree. Checked first; anything
# not listed falls back to PARENT_CATALOG_MAP on LCSC's parent catalog.
CATEGORY_MAP = {
    "Chip Resistor - Surface Mount": ("Passive Components", "Resistors", "Chip Resistors"),
    "Resistor Networks, Arrays": ("Passive Components", "Resistors", "Resistor Networks, Arrays"),
    "Through Hole Resistors": ("Passive Components", "Resistors", "Through Hole Resistors"),
    "Ceramic Capacitors": ("Passive Components", "Capacitors", "Chip Capacitors (MLCC)"),
    "Multilayer Ceramic Capacitors MLCC - SMD/SMT": ("Passive Components", "Capacitors", "Chip Capacitors (MLCC)"),
    "Film Capacitors": ("Passive Components", "Capacitors", "Film Capacitors"),
    "Tantalum Capacitors": ("Passive Components", "Capacitors", "Tantalum Capacitors"),
    "Aluminum - Polymer Capacitors": ("Passive Components", "Capacitors", "Polymer Solid Capacitors"),
    "Fixed Inductors": ("Passive Components", "Inductors & Ferrite", "Inductors"),
    "Ferrite Beads": ("Passive Components", "Inductors & Ferrite", "Ferrite Beads"),
    "Common Mode Chokes": ("Passive Components", "Inductors & Ferrite", "Common Mode Choke"),
    "PTC Resettable Fuses": ("Passive Components", "Fuse & PTC Protection"),
    "Crystals": ("Crystals, Oscillators, Resonators",),
    "Bridge Rectifiers": ("Discrete Semiconductors", "Diodes", "Rectifiers"),
    "Schottky Diodes": ("Discrete Semiconductors", "Diodes", "Schottky Diodes"),
    "Zener Diodes": ("Discrete Semiconductors", "Diodes", "Zener Diodes"),
    "Single Bipolar Transistors": ("Discrete Semiconductors", "Transistors", "BJT"),
    "Single FETs, MOSFETs": ("Discrete Semiconductors", "Transistors", "MOSFET"),
    "JFETs": ("Discrete Semiconductors", "Transistors", "JFET"),
    "Microcontrollers": ("Integrated Circuits", "Microcontrollers"),
    "Memory": ("Integrated Circuits", "Memory"),
    "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators": ("Integrated Circuits", "Linear Regulators"),
    "Supervisors": ("Integrated Circuits", "Voltage Reference & Detector"),
    "Battery Chargers": ("Integrated Circuits", "Power Management (Misc)"),
    "Power Over Ethernet (PoE) Controllers": ("Integrated Circuits", "Power Management (Misc)"),
    "Interface Controllers": ("Integrated Circuits", "Interface"),
    "Analog Switches - Special Purpose": ("Integrated Circuits", "Analog Switch & Mux"),
    "Digital Isolators": ("Integrated Circuits", "Isolators"),
    "ADCs/DACs - Special Purpose": ("Integrated Circuits", "Isolators"),
    "LED Addressable, Specialty": ("Optoelectronics", "LED"),
    "Photodiodes": ("Optoelectronics", "Photodiodes/Phototransistors"),
    "Tactile Switches": ("Electromechanical", "Switches"),
}
PARENT_CATALOG_MAP = {
    "Capacitors": ("Passive Components", "Capacitors"),
    "Resistors": ("Passive Components", "Resistors"),
    "Inductors, Coils, Chokes": ("Passive Components", "Inductors & Ferrite"),
    "Diodes": ("Discrete Semiconductors", "Diodes"),
    "Connectors": ("Electromechanical", "Connectors"),
    "Switches": ("Electromechanical", "Switches"),
    "Sensors": ("Sensors",),
    "Crystals, Oscillators, Resonators": ("Crystals, Oscillators, Resonators",),
}
# For a row LCSC has no product record for: (regex over MPN + description, path).
NO_RECORD_RULES = [
    (re.compile(r"\bFUSE", re.IGNORECASE), ("Passive Components", "Fuse & PTC Protection")),
]

# Attribute def name -> LCSC parameters to fill it from, tried in order;
# the first one present on the product with a parseable value wins. Each is
# (paramNameEn, pick): pick "value" takes the part before any "@" ("25mΩ@4.5V"),
# "after@" the part after it ("90Ω@100MHz" -> 100MHz), "max"/"min" one end of
# a "a~b" range ("max" also accepts a single value, "min" needs a range).
# Names repeat across categories ("Tolerance" is % on resistors but ppm on
# crystals); that resolves itself because a product only carries the LCSC
# parameters of its own kind and the unit is checked when parsing.
ATTRIBUTE_SOURCES = {
    "Resistance": [("Resistance", "value"), ("Resistance @ 25℃", "value")],
    "Tolerance": [("Tolerance", "value"), ("Normal temperature Frequency Tolerance", "value")],
    "Power": [("Power(Watts)", "value"), ("Pd - Power Dissipation", "value")],
    "T.C.": [("Temperature Coefficient", "value")],
    "Number of Resistors": [("Number of Resistors", "value")],
    "Capacitance": [("Capacitance", "value")],
    "Voltage - Rated": [("Voltage Rating", "value"), ("Voltage(AC)", "value")],
    "Impedance": [("Impedance @ Frequency", "value")],
    "Frequency": [("Frequency", "value"), ("Impedance @ Frequency", "after@")],
    "Current Rating (Max)": [("Current Rating", "value")],
    "Current Rating": [("Current Rating", "value"), ("Hold Current", "value")],
    "DC Resistance": [("DC Resistance(DCR)", "value")],
    "Inductance": [("Inductance", "value")],
    "Voltage Rating - DC": [("Voltage - Max", "value")],
    "DC Cold Resistance": [("Resistance @ 25℃", "value")],
    "Frequency Stability": [("Frequency Stability", "value")],
    "V\\R": [("Voltage - DC Reverse(Vr)", "value"), ("Voltage - DC Reverse (Vr) (Max)", "value")],
    "I\\F": [("Current - Rectified", "value")],
    "V\\F": [("Voltage - Forward(Vf@If)", "value")],
    "I\\R": [("Reverse Leakage Current (Ir)", "value")],
    "Pin Config": [("Diode Configuration", "value")],
    "Type": [("type", "value"), ("FET Type", "value"), ("Number", "value")],
    "V\\C\\E\\O": [("Collector - Emitter Voltage VCEO", "value")],
    "I\\C": [("Current - Collector(Ic)", "value")],
    "P\\D": [("Pd - Power Dissipation", "value")],
    "f\\T": [("Transition frequency(fT)", "value")],
    "V\\DSS": [("Drain to Source Voltage", "value")],
    "I\\D": [("Current - Continuous Drain(Id)", "value")],
    "R\\DS": [("RDS(on)", "value")],
    "V\\GS": [("Gate-Source Breakdown Voltage (Vgss)", "value")],
    "V\\GS (off)": [("Gate-Source Cutoff Voltage (VGS(off))", "value")],
    "Voltage - Input(Max)": [("Operating Voltage", "max"), ("Input Voltage", "max")],
    "Voltage - Input(Min)": [("Operating Voltage", "min"), ("Input Voltage", "min")],
    "Voltage - Output(Max/Fixed)": [("Output Voltage", "max")],
    "Voltage - Output(Min)": [("Output Voltage", "min")],
    "Current - Out": [("Output Current", "value")],
    "Voltage - Dropout": [("Dropout Voltage", "value")],
    "Size": [("Memory Size", "value")],
    "Clock Frequency": [("Clock Frequency", "max")],
    "Interface": [("Interface", "value")],
    "Voltage": [("Voltage - Supply", "max")],
}

SI_PREFIXES = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
_NUMBER_RE = re.compile(r"^[±+]?\s*(\d+(?:\.\d+)?)\s*(.*)$")
_LEADING_COUNT_RE = re.compile(r"^\d+\s+")


def _norm_unit(u):
    return (u or "").replace("℃", "°C").replace(" ", "")


def parse_quantity(text, unit):
    """"120mΩ" with unit "Ω" -> 0.12; "±1%" with "%" -> 1.0. None when the
    text isn't a single plain value in that unit — a different unit, an
    unknown suffix, a sign that isn't ± (ranges are split by the caller)."""
    m = _NUMBER_RE.match(text.strip())
    if not m:
        return None
    value, suffix = float(m.group(1)), _norm_unit(m.group(2))
    unit = _norm_unit(unit)
    if suffix == "" or suffix == unit:
        return value
    if suffix[0] in SI_PREFIXES and suffix[1:] == unit:
        # Round off the float noise of the scaling (0.7 * 1e-3 is 0.0007000000000000001).
        return float(f"{value * SI_PREFIXES[suffix[0]]:.12g}")
    return None


def _pick(text, pick):
    """The piece of an LCSC parameter value that `pick` asks for (see ATTRIBUTE_SOURCES)."""
    text = (text or "").strip()
    if not text or text == "-" or ";" in text:
        return None
    if pick == "after@":
        return text.split("@", 1)[1] if "@" in text else None
    text = text.split("@", 1)[0]
    if "~" in text:
        low, high = text.split("~", 1)
        return {"min": low, "max": high}.get(pick)
    return None if pick == "min" else text


def _match_enum(text, options):
    """The enum option LCSC's value names: an exact match (ignoring case and
    punctuation, so "P-Channel" is "P Channel", and either half of "C0G/NP0"
    counts), else the longest option contained in it ("1 Pair Common
    Cathode" -> "Common Cathode")."""
    squash = lambda s: re.sub(r"[^a-z0-9+]", "", s.lower())
    value = squash(_LEADING_COUNT_RE.sub("", text))
    best = None
    for option in options:
        alts = [squash(a) for a in [option] + option.split("/")]
        if value in alts:
            return option
        if any(len(a) >= 3 and a in value for a in alts) and (best is None or len(option) > len(best)):
            best = option
    return best


def _value_text(v, data_type):
    """Text form for value_text, in the style hand-entered values use:
    engineering exponents ("12e3", "100e6", "10e-6") for anything outside
    0.001..999, plain decimals otherwise."""
    if data_type != "engineering" or v == 0 or 1e-3 <= abs(v) < 1e3:
        return format_number(v)
    exp = math.floor(math.log10(abs(v)) / 3) * 3
    return f"{format_number(float(f'{v / 10 ** exp:.10g}'))}e{exp}"


def attribute_values(category, product):
    """[(attribute def, value_text, value_number)] that LCSC's parameter table
    supplies for `category`'s attributes."""
    params = {
        p["paramNameEn"].lower(): p.get("paramValueEn")
        for p in (product or {}).get("paramVOList") or []
        if p.get("paramNameEn")
    }
    found = []
    for ad in category.effective_attribute_defs:
        for name, pick in ATTRIBUTE_SOURCES.get(ad.name, []):
            piece = _pick(params.get(name.lower()), pick)
            if piece is None:
                continue
            if ad.data_type == "enum":
                option = _match_enum(piece, [o.strip() for o in (ad.enum_options or "").split(",")])
                if option:
                    found.append((ad, option, None))
                    break
            elif ad.data_type in ("number", "engineering"):
                number = parse_quantity(piece, ad.unit)
                if number is not None:
                    found.append((ad, _value_text(number, ad.data_type), number))
                    break
    return found


def normalize_package(raw):
    """LCSC package names in this inventory's style: "SOT-23-6" -> "SOT23-6",
    "LQFP-48(7x7)" -> "LQFP48", "SMD,4.4x4.1mm" -> "4.4x4.1mm",
    "SMD-4P,1.2x1mm" -> "SMD-4P 1.2x1mm". After a bare "SMD,"/"Through Hole,"
    only the next piece (size or pitch) is kept — connectors trail mounting
    notes like "SMD,P=1mm,Surface Mount,Right Angle"."""
    s = (raw or "").strip()
    if not s or s == "-":
        return None
    s = re.sub(r"\(.*?\)(mm)?", "", s)
    s = re.sub(r"^(SMD|Through Hole),([^,]*).*$", r"\2", s)
    s = re.sub(r"-\d+mil$", "", s)
    s = re.sub(r"^([A-Z]+)-(\d+)(?![\dP])", r"\1\2", s)
    return s.replace(",", " ").strip() or None


def _clean_description(text):
    text = re.sub(r"\s+RoHS\s*$", "", (text or "").strip())
    return None if text in ("", "-") else text[:512]


def _order_date(order_no):
    m = ORDER_RE.match(order_no)
    return datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)


def read_order_rows(paths):
    """Every row of every LCSC order CSV under `paths` (files or directories), tagged with its order number."""
    files = []
    for path in paths:
        files.extend(sorted(glob.glob(os.path.join(path, "*.csv"))) if os.path.isdir(path) else [path])
    rows = []
    for path in files:
        m = ORDER_RE.search(os.path.basename(path))
        if not m:
            raise click.ClickException(f"No LCSC order number (WM...) in file name: {path}")
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "order": m.group(0),
                    "lcsc": r["LCSC Part Number"].strip(),
                    "mpn": r["Manufacture Part Number"].strip(),
                    "manufacturer": r["Manufacturer"].strip() or None,
                    "package": r["Package"].strip(),
                    "description": r["Description"],
                    "quantity": int(r["Quantity"]),
                    "unit_price": float(r["Unit Price($)"]),
                })
    return rows


def _category_by_path(path):
    node = None
    for name in path:
        node = Category.query.filter_by(name=name, parent_id=node.id if node else None).first()
        if node is None:
            return None
    return node


def _guess_category(row, product):
    if product:
        path = CATEGORY_MAP.get(product.get("catalogName")) or PARENT_CATALOG_MAP.get(
            product.get("parentCatalogName")
        )
    else:
        text = f"{row['mpn']} {row['description']}"
        path = next((p for rx, p in NO_RECORD_RULES if rx.search(text)), None)
    return (_category_by_path(path) if path else None) or _category_by_path(("Uncategorized",))


def _find_part(mpn, manufacturer):
    """The existing part for this MPN, or None. Raises ValueError when several
    share it and the manufacturer doesn't single one out."""
    parts = Part.query.filter(db.func.upper(Part.mpn) == mpn.upper()).all()
    if len(parts) > 1:
        parts = [p for p in parts if p.manufacturer == manufacturer]
        if len(parts) != 1:
            raise ValueError(f"several parts with MPN {mpn}")
    return parts[0] if parts else None


def _already_imported(part, row):
    return (
        db.session.query(StockTransaction.id)
        .join(Lot, StockTransaction.lot_id == Lot.id)
        .filter(
            StockTransaction.part_id == part.id,
            StockTransaction.ref_no == row["order"],
            Lot.note == f"LCSC {row['lcsc']}",
        )
        .first()
        is not None
    )


def _staging_location(name):
    node = Location.query.filter_by(name=name, parent_id=None).first()
    if node is None:
        siblings = Location.query.filter_by(parent_id=None).count()
        node = Location(name=name, parent_id=None, sort_order=siblings)
        db.session.add(node)
        db.session.flush()
    return node


def _save_photo(part, url, is_primary, operator_id, written):
    content, ext = download_lcsc_image(url)
    subdir = f"parts/{part.id}/photos"
    target_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], subdir)
    os.makedirs(target_dir, exist_ok=True)
    rel_path = f"{subdir}/{uuid.uuid4().hex}.{ext}"
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], rel_path)
    with open(full_path, "wb") as f:
        f.write(content)
    written.append(full_path)
    thumb_rel = make_thumbnail(rel_path)
    if thumb_rel:
        written.append(os.path.join(current_app.config["UPLOAD_FOLDER"], thumb_rel))
    photo = Photo(file_path=rel_path, thumbnail_path=thumb_rel, uploaded_by=operator_id)
    db.session.add(photo)
    db.session.flush()
    db.session.add(PartPhoto(part_id=part.id, photo_id=photo.id, is_primary=is_primary))


def _save_datasheet(part, url, operator_id, written):
    content = download_lcsc_datasheet(url)
    rel_path = f"documents/{uuid.uuid4().hex}.pdf"
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(content)
    written.append(full_path)
    doc = Document(
        title=f"{part.mpn} Datasheet", doc_type="datasheet", file_path=rel_path,
        uploaded_by=operator_id, page_ratio=pdf_page_ratio(full_path),
    )
    db.session.add(doc)
    return doc


def _add_lot(part, location, row, operator_id):
    lot = Lot(
        part_id=part.id,
        location_id=location.id,
        quantity_received=row["quantity"],
        quantity_remaining=row["quantity"],
        unit_price=row["unit_price"],
        currency="USD",
        received_at=_order_date(row["order"]),
        note=f"LCSC {row['lcsc']}",
    )
    db.session.add(lot)
    db.session.flush()
    balance = (
        db.session.query(db.func.coalesce(db.func.sum(Lot.quantity_remaining), 0))
        .filter_by(part_id=part.id, location_id=location.id)
        .scalar()
    )
    db.session.add(
        StockTransaction(
            part_id=part.id,
            location_id=location.id,
            lot_id=lot.id,
            type="in",
            quantity_delta=row["quantity"],
            balance_after=balance,
            unit_price=row["unit_price"],
            currency="USD",
            reason="Imported from LCSC order",
            ref_no=row["order"],
            operator_id=operator_id,
        )
    )


def _import_row(row, product, location, operator_id, photos, datasheets, dry_run, written, photographed,
                datasheeted, docs, stats):
    """Apply one CSV row; returns its report line."""
    report = {
        "order": row["order"], "lcsc": row["lcsc"], "mpn": row["mpn"], "qty": row["quantity"],
        "unit_price_usd": row["unit_price"],
    }
    try:
        part = _find_part(row["mpn"], MANUFACTURER_ALIASES.get(row["manufacturer"], row["manufacturer"]))
    except ValueError as e:
        stats["skipped_ambiguous"] += 1
        return {**report, "action": "skipped", "notes": str(e)}

    if part is not None and _already_imported(part, row):
        stats["skipped_already_imported"] += 1
        return {**report, "action": "skipped", "part_id": part.id, "notes": "already imported"}

    if part is None:
        part = Part(
            mpn=row["mpn"],
            manufacturer=MANUFACTURER_ALIASES.get(row["manufacturer"], row["manufacturer"]),
            package=normalize_package(row["package"]),
            description=_clean_description(row["description"]),
            category_id=_guess_category(row, product).id,
            default_unit_price=row["unit_price"],
            currency="USD",
        )
        db.session.add(part)
        db.session.flush()
        stats["parts_created"] += 1
        report["action"] = "new part"
    else:
        stats["parts_reused"] += 1
        report["action"] = "existing part"
    report.update(
        part_id=part.id, manufacturer=part.manufacturer, package=part.package,
        category=part.category.path_name(" / ") if part.category else "",
        lcsc_category=f"{product.get('parentCatalogName')} > {product.get('catalogName')}" if product else "(no LCSC record)",
    )

    filled = []
    if part.category:
        have = {v.attribute_def_id for v in part.attribute_values}
        for ad, text, number in attribute_values(part.category, product):
            if ad.id in have:
                continue
            db.session.add(PartAttributeValue(
                part_id=part.id, attribute_def_id=ad.id, value_text=text, value_number=number,
            ))
            filled.append(f"{ad.name}={text}{ad.unit or ''}")
        db.session.flush()
    stats["attribute_values"] += len(filled)
    report["attributes_filled"] = "; ".join(filled)

    # `photographed` also covers dry runs, where nothing lands in photo_links
    # for a part that turns up again in a later order.
    wants_photos = photos and product and not part.photo_links and part.id not in photographed
    urls = product_images(product) if wants_photos else []
    saved = 0
    for url in urls:
        if dry_run:
            saved += 1
            continue
        try:
            _save_photo(part, url, saved == 0, operator_id, written)
            saved += 1
        except LCSC_ERRORS as e:
            click.echo(f"  photo download failed for {row['mpn']}: {e}", err=True)
    if saved:
        photographed.add(part.id)
        db.session.expire(part, ["photo_links"])
        stats["parts_with_new_photos"] += 1
    report["photos_added"] = saved

    # `docs` maps a PDF (its URL minus the ?productCode= query, which is all
    # that differs between listings sharing one) to the document already
    # made for it this run (None in a dry run). `datasheeted`, like
    # `photographed`, stops a part from a later order getting a second copy.
    pdf_url = (product or {}).get("pdfUrl") if datasheets else None
    has_datasheet = part.id in datasheeted or any(d.doc_type == "datasheet" for d in part.documents)
    if pdf_url and not has_datasheet:
        key = pdf_url.split("?", 1)[0]
        doc = docs.get(key)
        try:
            if doc is None and not dry_run:
                doc = _save_datasheet(part, pdf_url, operator_id, written)
            if not dry_run:
                if key in docs:
                    doc.title = f"{doc.title.removesuffix(' Datasheet')} / {part.mpn} Datasheet"
                part.documents.append(doc)
            report["datasheet"] = "shared" if key in docs else "added"
            docs[key] = doc
            datasheeted.add(part.id)
            stats["datasheets_attached"] += 1
        except LCSC_ERRORS as e:
            click.echo(f"  datasheet download failed for {row['mpn']}: {e}", err=True)
            report["datasheet"] = "failed"

    _add_lot(part, location, row, operator_id)
    stats["lots_added"] += 1
    return report


REPORT_FIELDS = [
    "order", "lcsc", "mpn", "action", "part_id", "manufacturer", "package", "category",
    "lcsc_category", "qty", "unit_price_usd", "attributes_filled", "photos_added", "datasheet", "notes",
]


def run_import(paths, skip_orders=(), exclude=(), location_name="Unshelved", operator=None,
               photos=True, datasheets=True, report_path=None, dry_run=False, delay=0.4):
    rows = read_order_rows(paths)
    stats = Counter()
    kept = []
    for row in rows:
        if row["order"] in skip_orders:
            stats["skipped_order"] += 1
        elif row["lcsc"] in exclude:
            stats["skipped_excluded"] += 1
        else:
            kept.append(row)

    operator_id = None
    if operator:
        user = User.query.filter_by(username=operator).first()
        if user is None:
            raise click.ClickException(f"No such user: {operator}")
        operator_id = user.id

    products = {}
    for code in dict.fromkeys(r["lcsc"] for r in kept):
        try:
            products[code] = fetch_product(code)
        except LCSC_ERRORS as e:
            click.echo(f"  no LCSC record for {code} ({e}); importing it from the CSV alone", err=True)
            products[code] = None
        time.sleep(delay)

    location = _staging_location(location_name)
    written = []
    photographed = set()
    datasheeted = set()
    docs = {}
    report = []
    try:
        for row in kept:
            report.append(_import_row(
                row, products[row["lcsc"]], location, operator_id, photos, datasheets, dry_run, written,
                photographed, datasheeted, docs, stats,
            ))
        if dry_run:
            db.session.rollback()
        else:
            db.session.commit()
    except Exception:
        db.session.rollback()
        for path in written:
            if os.path.exists(path):
                os.remove(path)
        raise

    if report_path:
        with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(report)

    click.echo(
        "LCSC import {}: {} rows read ({} in skipped orders, {} excluded); "
        "{} parts created / {} reused, {} lots added to '{}', {} attribute values filled, "
        "{} parts got photos, {} got datasheets; {} rows already imported, {} ambiguous".format(
            "dry run (not written)" if dry_run else "complete",
            len(rows), stats["skipped_order"], stats["skipped_excluded"],
            stats["parts_created"], stats["parts_reused"], stats["lots_added"], location_name,
            stats["attribute_values"], stats["parts_with_new_photos"], stats["datasheets_attached"],
            stats["skipped_already_imported"], stats["skipped_ambiguous"],
        )
    )
