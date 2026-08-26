"""Loading a category or location tree in a fixed number of queries.

Walking `Location.children` and reading a per-node total off the ORM costs
a query per node per value, because every relationship is lazy. At ~750
locations that turned one page load into ~2200 queries and most of a
second: 764 for the tree walk, 763 for each node's lots, 666 for the part
behind each lot. It's down to 5.

Everything here loads whole tables once and walks them in Python. The
category and location dropdowns on the parts pages use it too, for the
same reason — each was re-walking its tree lazily to build the options.
"""

from collections import defaultdict

from app.extensions import db
from app.models import Category, Location, Lot, Part


def load_tree(model):
    """Every row of a self-referential tree in one query, as
    (roots, children-by-parent-id).

    Rows come back ordered by sort_order, which is numbered per parent, so
    grouping them preserves each parent's own ordering.
    """
    children = defaultdict(list)
    for node in model.query.order_by(model.sort_order).all():
        children[node.parent_id].append(node)
    return children[None], children


def load_location_tree():
    return load_tree(Location)


def load_category_tree():
    return load_tree(Category)


def flatten_tree_from(roots, children, depth=0):
    """Pre-order (node, depth) list for indented dropdown options — the
    Pre-order (node, depth) pairs, read from the prebuilt child map
    instead of touching the lazy relationship."""
    out = []
    for node in roots:
        out.append((node, depth))
        out.extend(flatten_tree_from(children[node.id], children, depth + 1))
    return out


def location_stats(children):
    """Per-location batch count, in-stock parts, and empty-branch flag.

    One query for the lots, then a single post-order pass. `batches` counts
    the whole subtree, so a parent reflects everything beneath it; `parts`
    is only what sits directly on that node (the tree lists it on leaves);
    `empty` marks a leaf holding nothing, or any node with such a leaf
    below it, which is what the "show only empty" filter keeps visible.
    """
    rows = (
        db.session.query(Lot.location_id, Part.id, Part.mpn)
        .join(Part, Part.id == Lot.part_id)
        .filter(Lot.quantity_remaining > 0)
        .all()
    )

    own_batches = defaultdict(int)
    own_parts = defaultdict(dict)  # loc_id -> {part_id: mpn}, dedupes lots of the same part
    for loc_id, part_id, mpn in rows:
        own_batches[loc_id] += 1
        own_parts[loc_id][part_id] = mpn

    stats = {}

    def visit(node):
        kids = children[node.id]
        total = own_batches[node.id]
        for kid in kids:
            visit(kid)
            total += stats[kid.id]["batches"]
        stats[node.id] = {
            "batches": total,
            "parts": sorted(own_parts[node.id].items(), key=lambda kv: kv[1]),
            "empty": (total == 0) if not kids else any(stats[k.id]["empty"] for k in kids),
        }

    for root in children[None]:
        visit(root)
    return stats
