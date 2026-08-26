"""
Batch version of the "Search LCSC" button on the part edit page: look up
every part's MPN on LCSC and attach its product photos automatically.

Only exact MPN matches are downloaded; anything else is left for manual
review (via the edit page's "Search LCSC" button) so we never attach the
wrong part's photo. See app/lcsc.py for how the lookup works.
"""
import os
import time
import uuid

import click

from app.extensions import db
from app.lcsc import LCSC_ERRORS, download_lcsc_image, product_images, search_exact
from app.models import Part, PartPhoto, Photo
from app.utils import make_thumbnail


def _save_photo(part, url, is_primary, upload_folder):
    content, ext = download_lcsc_image(url)

    subdir = f"parts/{part.id}/photos"
    target_dir = os.path.join(upload_folder, subdir)
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    rel_path = f"{subdir}/{filename}"
    with open(os.path.join(target_dir, filename), "wb") as f:
        f.write(content)

    thumb_rel = make_thumbnail(rel_path)
    photo = Photo(file_path=rel_path, thumbnail_path=thumb_rel)
    db.session.add(photo)
    db.session.flush()
    db.session.add(PartPhoto(part_id=part.id, photo_id=photo.id, is_primary=is_primary))


def run_fetch(currency="USD", limit=None, force=False, dry_run=False, delay=0.4):
    """Fetch and attach LCSC photos for parts in the given currency that
    don't already have a photo (unless --force)."""
    from flask import current_app

    upload_folder = current_app.config["UPLOAD_FOLDER"]

    query = Part.query.filter(Part.currency == currency) if currency else Part.query
    if not force:
        query = query.filter(~Part.photo_links.any())
    parts = query.order_by(Part.id).all()
    if limit:
        parts = parts[:limit]

    matched, attached, skipped, errors = [], [], [], []

    for i, part in enumerate(parts):
        try:
            exact_matches = search_exact(part.mpn)
        except LCSC_ERRORS as e:
            errors.append((part.mpn, str(e)))
            continue

        if not exact_matches:
            skipped.append(part.mpn)
            continue
        if len(exact_matches) > 1:
            # Ambiguous — more than one listing exactly matches this MPN
            # (different manufacturers/variants). There's no one here to
            # pick the right one, so leave it for manual review via the
            # edit page's Search LCSC button rather than guessing.
            skipped.append(f"{part.mpn} (ambiguous: {len(exact_matches)} exact matches on LCSC)")
            continue
        product = exact_matches[0]

        image_urls = product_images(product)
        if not image_urls:
            # Exact MPN match, but LCSC has no photo under that exact SKU.
            skipped.append(f"{part.mpn} (matched {product['productCode']}, no photo on LCSC)")
            continue

        matched.append((part.mpn, product["productCode"]))
        if dry_run:
            continue

        existing_count = len(part.photo_links)
        try:
            for j, url in enumerate(image_urls):
                _save_photo(part, url, is_primary=(existing_count == 0 and j == 0), upload_folder=upload_folder)
            db.session.commit()
            attached.append(part.mpn)
        except LCSC_ERRORS + (OSError,) as e:
            db.session.rollback()
            errors.append((part.mpn, str(e)))

        if i < len(parts) - 1:
            time.sleep(delay)

    click.echo(f"Parts considered: {len(parts)}")
    click.echo(f"Exact LCSC match: {len(matched)}")
    if dry_run:
        click.echo("(dry run — nothing downloaded or written)")
    else:
        click.echo(f"Photos attached: {len(attached)}")
    click.echo(f"No exact match (skipped, needs manual review): {len(skipped)}")
    if skipped:
        for mpn in skipped:
            click.echo(f"  - {mpn}")
    if errors:
        click.echo(f"Errors: {len(errors)}")
        for mpn, msg in errors:
            click.echo(f"  - {mpn}: {msg}")
