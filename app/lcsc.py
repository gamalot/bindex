"""
Look up parts on LCSC (https://www.lcsc.com) by MPN and fetch their product
photos.

LCSC's product-image pages block right-click saving client-side, but the
images themselves are plain, unauthenticated static files on assets.lcsc.com,
and LCSC's own search box calls a public JSON API to resolve a keyword to a
product — no browser session or login needed, a plain HTTP request is enough.

Used by both the `flask fetch-lcsc-photos` batch command (scripts/fetch_lcsc_photos.py)
and the "Search LCSC" button on the part edit page (app/blueprints/parts/routes.py).
`fetch_product` (the full record by LCSC code, parameters included) backs the
`flask import-lcsc` order importer (scripts/import_lcsc.py), along with
`download_lcsc_datasheet` for the record's `pdfUrl`.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://wmsc.lcsc.com/ftps/wm/search/v3/global"
CATALOG_URL = "https://wmsc.lcsc.com/ftps/wm/search/third/catalog"
PRODUCT_PAGE_URL = "https://www.lcsc.com/product-image/{code}.html"
PRODUCT_DETAIL_URL = "https://www.lcsc.com/product-detail/{code}.html"
ALLOWED_IMAGE_HOST = "assets.lcsc.com"
HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lcsc.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
# Errors that mean "the network call didn't work", worth catching at call sites.
LCSC_ERRORS = (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError)


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _http_post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _global_search(keyword):
    """Raw call to LCSC's search box API. Returns the parsed `result` dict."""
    payload = {
        "keyword": keyword,
        "secondKeyword": "",
        "brandIdList": [],
        "catalogIdList": [],
        "isStock": False,
        "isAsianBrand": False,
        "isDeals": False,
        "isRohsCert": False,
    }
    data = _http_post_json(SEARCH_URL, payload)
    return data.get("result") or {}


def search_exact(keyword):
    """Look up `keyword` via LCSC's search API. Returns *every* result whose
    productModel exactly matches `keyword` (case-insensitive) — there can
    legitimately be more than one (the same MPN listed under different
    manufacturers, or duplicate/variant listings), so this doesn't guess
    which one is "the" match by just taking the first; the caller decides
    what to do when there's more than one. Empty list if there's no exact
    match at all (a different/looser match is never included here — use
    search_suggestions for that)."""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    matches = _global_search(keyword).get("exactMatchResult") or []
    return [m for m in matches if m.get("productModel", "").strip().upper() == keyword.upper()]


def _collect_leaf_catalog_ids(nodes):
    leaves = []
    for n in nodes or []:
        children = n.get("childCatelogs") or []
        if n.get("isLeaf") or not children:
            leaves.append(n["catalogId"])
        else:
            leaves.extend(_collect_leaf_catalog_ids(children))
    return leaves


def _fetch_product_detail(code):
    """Scrape a product-image page for its clean model name + image URLs.
    Same data LCSC's own Nuxt frontend renders from, just parsed out of the
    server-rendered page instead of a dedicated JSON endpoint (there isn't
    one for "look up by LCSC code")."""
    html = _http_get(PRODUCT_PAGE_URL.format(code=code)).decode("utf-8", errors="replace")
    model_m = re.search(r'productModel:"([^"]+)"', html)
    encap_m = re.search(r'encapStandard:"([^"]*)"', html)
    images_m = re.search(r'\["(https:\\u002F\\u002Fassets\.lcsc\.com[^\]]*)"\]', html)
    images = []
    if images_m:
        images = json.loads(('["' + images_m.group(1) + '"]').replace("\\u002F", "/"))
    return {
        "product_code": code,
        "product_model": model_m.group(1) if model_m else code,
        "encap": encap_m.group(1) if encap_m else None,
        "images": images,
    }


def fetch_product(code):
    """Full product record for an LCSC code (e.g. "C317180"): category,
    brand, package, image URLs, datasheet URL and the parametric table
    (`paramVOList`). The product-detail page server-renders the record as
    a plain JSON object inside its Nuxt payload, so this finds the object
    that holds `paramVOList` and decodes it whole. Raises ValueError when
    the page carries no such record (some listings, e.g. spare fuses sold
    as accessories, have no detail page)."""
    html = _http_get(PRODUCT_DETAIL_URL.format(code=code)).decode("utf-8", errors="replace")
    anchor = html.find('"paramVOList"')
    if anchor < 0:
        raise ValueError(f"No product record on the LCSC detail page for {code}")
    decoder = json.JSONDecoder()
    start = anchor
    while True:
        start = html.rfind("{", 0, start)
        if start < 0:
            raise ValueError(f"Couldn't decode the LCSC product record for {code}")
        try:
            obj, end = decoder.raw_decode(html, start)
        except ValueError:
            continue
        if end > anchor and "paramVOList" in obj:
            return obj


def search_suggestions(keyword, limit=5):
    """When there's no exact match, return up to `limit` distinct candidate
    products (with photos) for the user to pick from, mirroring what LCSC's
    own search results page would show for an ambiguous/partial keyword."""
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    result = _global_search(keyword)
    leaf_ids = _collect_leaf_catalog_ids(result.get("catalogVOS"))
    if not leaf_ids:
        return []

    payload = {"currentPage": 1, "pageSize": 20, "keyword": keyword, "wmCatalogIdList": leaf_ids}
    data = _http_post_json(CATALOG_URL, payload)
    items = ((data.get("result") or {}).get("productList")) or []

    codes = []
    seen = set()
    for it in items:
        code = it.get("productCode")
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    suggestions = []
    for code in codes:
        if len(suggestions) >= limit:
            break
        try:
            detail = _fetch_product_detail(code)
        except LCSC_ERRORS:
            continue
        if detail["images"]:
            suggestions.append(detail)
    return suggestions


def product_images(product, limit=3):
    """Extract up to `limit` usable image URLs (front/back/blank, largest size) from a search match."""
    images = (product.get("productImages") or [])[:limit]
    if not images and product.get("productImageUrlBig"):
        images = [product["productImageUrlBig"]]
    return images


def download_lcsc_image(url):
    """Download an LCSC product image. Returns (content_bytes, extension).
    Refuses to fetch anything not actually hosted on LCSC's asset CDN, since
    this can be reached with a client-supplied URL."""
    host = urllib.parse.urlparse(url).netloc
    if host != ALLOWED_IMAGE_HOST:
        raise ValueError(f"Refusing to fetch image from untrusted host: {host}")
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in ("jpg", "jpeg", "png"):
        ext = "jpg"
    content = _http_get(url)
    return content, ext


def download_lcsc_datasheet(url):
    """Download the PDF a product record's `pdfUrl` points at. Returns the
    file's bytes. Usually that's LCSC's own copy on datasheet.lcsc.com, but
    some listings link the manufacturer's site instead, so any https host
    is accepted — the URL comes from LCSC's record, never from a client.
    Refuses anything that isn't actually a PDF (a missing file comes back
    as an HTML error page with a 200)."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Refusing to fetch datasheet over a non-https URL: {url}")
    content = _http_get(url)
    if not content.startswith(b"%PDF"):
        raise ValueError(f"Not a PDF: {url}")
    return content
