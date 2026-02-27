import os
import re
import io
import json
import base64
import asyncio
import httpx
from PIL import Image
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from serpapi import GoogleSearch

app = FastAPI(title="FitFinder API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Country Configurations ───
COUNTRIES = {
    "tr": {
        "name": "Türkiye", "gl": "tr", "hl": "tr", "lang": "Turkish",
        "currency": "₺",
        "local_stores": ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.",
                         "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep."],
        "gender": {"male": "erkek", "female": "kadın"},
    },
    "us": {
        "name": "United States", "gl": "us", "hl": "en", "lang": "English",
        "currency": "$",
        "local_stores": ["nordstrom.", "macys.", "bloomingdales.", "target.com",
                         "walmart.com", "urbanoutfitters.", "freepeople.", "anthropologie.",
                         "revolve.com", "shopbop.", "ssense.", "farfetch."],
        "gender": {"male": "men", "female": "women"},
    },
    "gb": {
        "name": "United Kingdom", "gl": "gb", "hl": "en", "lang": "English",
        "currency": "£",
        "local_stores": ["asos.com", "selfridges.", "harrods.", "johnlewis.",
                         "next.co.uk", "boohoo.", "prettylittlething.", "missguided."],
        "gender": {"male": "men", "female": "women"},
    },
    "de": {
        "name": "Deutschland", "gl": "de", "hl": "de", "lang": "German",
        "currency": "€",
        "local_stores": ["zalando.", "aboutyou.", "otto.", "breuninger.",
                         "peek-cloppenburg.", "bonprix."],
        "gender": {"male": "herren", "female": "damen"},
    },
    "fr": {
        "name": "France", "gl": "fr", "hl": "fr", "lang": "French",
        "currency": "€",
        "local_stores": ["galerieslafayette.", "laredoute.", "veepee.",
                         "printemps.", "sarenza."],
        "gender": {"male": "homme", "female": "femme"},
    },
    "sa": {
        "name": "Saudi Arabia", "gl": "sa", "hl": "ar", "lang": "Arabic",
        "currency": "SAR",
        "local_stores": ["namshi.", "ounass.", "sivvi.", "nisnass.",
                         "styli.", "vogacloset."],
        "gender": {"male": "men", "female": "women"},
    },
    "ae": {
        "name": "UAE", "gl": "ae", "hl": "en", "lang": "English",
        "currency": "AED",
        "local_stores": ["namshi.", "ounass.", "sivvi.", "nisnass.",
                         "6thstreet.", "bloomingdales.ae"],
        "gender": {"male": "men", "female": "women"},
    },
    "nl": {
        "name": "Netherlands", "gl": "nl", "hl": "nl", "lang": "Dutch",
        "currency": "€",
        "local_stores": ["zalando.", "debijenkorf.", "wehkamp.", "aboutyou."],
        "gender": {"male": "heren", "female": "dames"},
    },
}

DEFAULT_COUNTRY = "us"

def get_country_config(cc):
    return COUNTRIES.get(cc.lower(), COUNTRIES[DEFAULT_COUNTRY])

BRAND_MAP = {
    "trendyol.com": "Trendyol", "hepsiburada.com": "Hepsiburada",
    "boyner.com.tr": "Boyner", "defacto.com": "DeFacto",
    "lcwaikiki.com": "LC Waikiki", "koton.com": "Koton",
    "beymen.com": "Beymen", "n11.com": "N11", "flo.com.tr": "FLO",
    "mavi.com": "Mavi", "superstep.com.tr": "Superstep",
    "zara.com": "Zara", "bershka.com": "Bershka",
    "pullandbear.com": "Pull&Bear", "hm.com": "H&M",
    "mango.com": "Mango", "asos.com": "ASOS",
    "stradivarius.com": "Stradivarius", "massimodutti.com": "Massimo Dutti",
    "nike.com": "Nike", "adidas.": "Adidas", "puma.com": "Puma",
    "newbalance.": "New Balance", "converse.": "Converse",
}

BLOCKED = [
    "pinterest.", "instagram.", "facebook.", "twitter.", "x.com",
    "tiktok.", "youtube.", "reddit.", "tumblr.", "blogspot.",
    "wordpress.", "medium.com", "threads.net", "etsy.com",
    "ebay.com", "ebay.", "amazon.com", "aliexpress.",
    "wish.com", "dhgate.", "alibaba.", "flickr.",
    "tripadvisor.", "booking.", "yelp.",
    "lemon8.", "lookbook.", "chictopia.", "polyvore.",
    "weheartit.", "wearethis.", "stylevore.", "fashmates.",
    "taobao.", "tmall.", "jd.com", "1688.com", "pinduoduo.",
    "forlady", "chicisimo.", "bantoa.", "wear.jp",
    "shutterstock.", "gettyimages.", "alamy.", "dreamstime.",
    "vecteezy.", "freepik.", "unsplash.", "pexels.",
]

FASHION_DOMAINS = [
    "trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.",
    "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep.",
    "zara.com", "bershka.com", "pullandbear.com", "hm.com",
    "mango.com", "asos.com", "shein.com", "stradivarius.com",
    "massimodutti.com", "nike.com", "adidas.", "puma.com",
    "newbalance.", "converse.", "morhipo.", "lidyana.", "modanisa.",
]

FASHION_KW = [
    "ceket", "mont", "kaban", "bomber", "jacket", "coat", "blazer",
    "pantolon", "jean", "denim", "pants", "jogger", "chino",
    "gomlek", "tisort", "sweatshirt", "hoodie", "kazak", "shirt",
    "elbise", "etek", "dress", "skirt",
    "ayakkabi", "sneaker", "boot", "shoe", "bot",
    "canta", "bag", "sapka", "bere", "hat", "cap",
    "gozluk", "saat", "watch", "sunglasses",
    "erkek", "kadin", "giyim", "fashion", "wear", "clothing",
]

PIECE_KEYWORDS = {
    "hat": ["sapka", "şapka", "cap", "hat", "bere", "beanie", "kasket", "kepi", "baseball", "snapback", "bucket", "fedora"],
    "sunglasses": ["gozluk", "gözlük", "sunglasses", "güneş", "eyewear"],
    "scarf": ["atki", "atkı", "sal", "şal", "fular", "scarf", "bandana"],
    "jacket": ["ceket", "mont", "kaban", "blazer", "bomber", "jacket", "coat", "varsity", "parka", "trench", "palto", "kase", "kaşe", "cardigan", "hırka", "yelek", "vest", "windbreaker", "puffer", "embroidered cloth"],
    "top": ["tisort", "tişört", "gomlek", "gömlek", "sweatshirt", "hoodie", "kazak", "bluz", "top", "shirt", "polo", "triko", "t-shirt", "tee", "tank", "henley", "sweater"],
    "bottom": ["pantolon", "jean", "denim", "jogger", "chino", "pants", "trousers", "sort", "şort", "etek", "skirt", "cargo", "wide leg", "slim fit", "straight", "baggy"],
    "dress": ["elbise", "dress", "tulum", "jumpsuit", "romper"],
    "shoes": ["ayakkabi", "ayakkabı", "sneaker", "bot", "boot", "shoe", "terlik", "loafer", "sandalet", "trainer", "runner", "chelsea", "oxford"],
    "bag": ["canta", "çanta", "bag", "clutch", "sirt", "sırt", "backpack", "tote", "crossbody"],
    "watch": ["saat", "watch", "kol saati", "timepiece"],
    "accessory": ["kolye", "bileklik", "yuzuk", "yüzük", "kupe", "küpe", "aksesuar", "kemer", "belt", "necklace", "bracelet"],
}


# ─── Helpers ───
def get_brand(link, src):
    c = (link + " " + src).lower()
    for d, b in BRAND_MAP.items():
        if d in c:
            return b
    return src if src else ""


def is_local(link, src, country_config):
    return any(d in (link + " " + src).lower() for d in country_config.get("local_stores", []))


def is_blocked(link):
    return any(d in link.lower() for d in BLOCKED)


def is_fashion(link, title, src):
    c = (link + " " + src).lower()
    if any(d in c for d in FASHION_DOMAINS):
        return True
    t = (title + " " + src).lower()
    return any(k in t for k in FASHION_KW)


# ─── Image Upload ───
async def upload_img(img_bytes):
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": ("i.jpg", img_bytes, "image/jpeg")},
            )
            if r.status_code == 200 and r.text.startswith("http"):
                return r.text.strip()
        except Exception as e:
            print(f"Upload err: {e}")
        try:
            r = await c.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": ("i.jpg", img_bytes, "image/jpeg")},
            )
            if r.status_code == 200:
                u = r.json().get("data", {}).get("url", "")
                if u:
                    return u.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        except Exception as e:
            print(f"Upload2 err: {e}")
    return None


# ─── Claude Vision ───
async def claude_detect(img_b64, cc="tr"):
    if not ANTHROPIC_API_KEY:
        return None
    cfg = get_country_config(cc)
    lang = cfg["lang"]
    g_male = cfg["gender"]["male"]
    g_female = cfg["gender"]["female"]
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                            {"type": "text", "text": f"""Analyze every clothing item and accessory this person is wearing.

FIRST: Determine the person's gender: "{g_male}" (male) or "{g_female}" (female)

CRITICAL RULES:
1. ONLY list items that are CLEARLY VISIBLE as SEPARATE garments
2. A collar, lining, or edge peeking under a jacket is NOT a separate piece
3. If you can see less than 30% of an item, do NOT list it
4. Do NOT guess hidden items - only what you can actually see
5. Read ALL visible text, logos, brand names, patches on each item

For each CLEARLY VISIBLE item:
- category: hat|sunglasses|scarf|jacket|top|bottom|dress|shoes|bag|watch|accessory
- short_title: 2-4 word {lang} name. Be EXACT about item type.
- color: color name in {lang}
- brand: ONLY if you can READ it on the item, else "?"
- visible_text: ALL readable text/logos/patches
- search_query: 4-6 word ULTRA SPECIFIC {lang} shopping query
  * MUST include "{g_male}" or "{g_female}" based on gender
  * MUST match exact item type
  * Include brand if readable, include visible text/patches

Return ONLY valid JSON array, no markdown no backticks:
[{{"category":"","short_title":"","color":"","brand":"","visible_text":"","search_query":""}}]"""},
                        ],
                    }],
                },
            )
            data = r.json()
            if "error" in data:
                print(f"Claude error: {data['error']}")
                return None
            text = data.get("content", [{}])[0].get("text", "").strip()
            if text.startswith("```"):
                text = re.sub(r'^```\w*\n?', '', text)
                text = re.sub(r'\n?```$', '', text)
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError as e:
                    print(f"JSON err: {e}")
        except Exception as e:
            print(f"Claude err: {type(e).__name__}: {e}")
    return None


# ─── Google Lens ───
def _lens(url, cc="tr"):
    cfg = get_country_config(cc)
    res = []
    seen = set()
    try:
        d = GoogleSearch({
            "engine": "google_lens", "url": url,
            "api_key": SERPAPI_KEY, "hl": cfg["hl"], "country": cfg["gl"],
        }).get_dict()
        all_matches = d.get("visual_matches", [])
        print(f"  Lens raw: {len(all_matches)} visual_matches")
        for m in all_matches:
            lnk = m.get("link", "")
            ttl = m.get("title", "")
            src = m.get("source", "")
            if not lnk or not ttl or lnk in seen:
                continue
            if is_blocked(lnk):
                continue
            if not is_fashion(lnk, ttl, src):
                continue
            seen.add(lnk)
            pr = m.get("price", {})
            pr = pr.get("value", "") if isinstance(pr, dict) else str(pr)
            res.append({
                "title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": lnk, "price": pr,
                "thumbnail": m.get("thumbnail", ""),
                "image": m.get("image", ""),
                "is_local": is_local(lnk, src, cfg),
            })
            if len(res) >= 20:
                break
    except Exception as e:
        print(f"Lens err: {e}")
    def score(r):
        s = 0
        if r["price"]:
            s += 10
        if r["is_local"]:
            s += 5
        c = (r["link"] + " " + r["source"]).lower()
        if any(d in c for d in FASHION_DOMAINS):
            s += 3
        return -s
    res.sort(key=score)
    print(f"  Lens after filter: {len(res)} results")
    return res


# ─── Google Shopping ───
def _shop(q, cc="tr", limit=6):
    cfg = get_country_config(cc)
    res = []
    seen = set()
    try:
        d = GoogleSearch({
            "engine": "google_shopping", "q": q,
            "gl": cfg["gl"], "hl": cfg["hl"], "api_key": SERPAPI_KEY,
        }).get_dict()
        for item in d.get("shopping_results", []):
            lnk = item.get("product_link") or item.get("link", "")
            ttl = item.get("title", "")
            src = item.get("source", "")
            if not lnk or not ttl or lnk in seen or is_blocked(lnk):
                continue
            seen.add(lnk)
            res.append({
                "title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": lnk,
                "price": item.get("price", str(item.get("extracted_price", ""))),
                "thumbnail": item.get("thumbnail", ""),
                "image": "",
                "is_local": is_local(lnk, src, cfg),
            })
            if len(res) >= limit:
                break
    except Exception as e:
        print(f"Shop err: {e}")
    return res


# ─── Match Lens → Pieces ───
def match_lens_to_pieces(lens_results, pieces):
    piece_lens = {i: [] for i in range(len(pieces))}
    for lr in lens_results:
        title_lower = lr["title"].lower()
        for i, p in enumerate(pieces):
            cat = p.get("category", "")
            keywords = PIECE_KEYWORDS.get(cat, [])
            if any(kw in title_lower for kw in keywords):
                piece_lens[i].append(lr)
                break
    return piece_lens


# ─── Process Piece (Shopping only) ───
async def process_piece(p, cc="tr"):
    cat = p.get("category", "")
    q = p.get("search_query", "") or p.get("search_query_tr", "") or p.get("short_title", "") or p.get("short_title_tr", "")
    print(f"[{cat}] Shopping: '{q}'")
    products = await asyncio.to_thread(_shop, q, cc) if q else []
    print(f"[{cat}] Found: {len(products)}")
    return {
        "category": cat,
        "short_title": p.get("short_title", "") or p.get("short_title_tr", cat.title()),
        "color": p.get("color", "") or p.get("color_tr", ""),
        "brand": p.get("brand", ""),
        "visible_text": p.get("visible_text", ""),
        "products": products,
        "lens_count": 0,
    }


# ─── AUTO ANALYZE ───
@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...), country: str = Form("tr")):
    if not SERPAPI_KEY:
        raise HTTPException(500, "No API key")

    cc = country.lower()
    cfg = get_country_config(cc)
    contents = await file.read()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        b64 = base64.b64encode(contents).decode()

    print(f"\n{'='*50}")
    print(f"=== AUTO ANALYZE === country={cc} ({cfg['name']})")

    detect_task = claude_detect(b64, cc)
    upload_task = upload_img(contents)
    pieces, url = await asyncio.gather(detect_task, upload_task)

    lens_results = []
    if url:
        lens_results = await asyncio.to_thread(_lens, url, cc)
        print(f"Full image Lens: {len(lens_results)}")

    if not pieces:
        return {"success": True, "pieces": [], "country": cc}

    print(f"Claude: {len(pieces)} pieces")
    piece_lens = match_lens_to_pieces(lens_results, pieces)

    tasks = [process_piece(p, cc) for p in pieces]
    results = list(await asyncio.gather(*tasks))

    for i, r in enumerate(results):
        lens_for_piece = piece_lens.get(i, [])
        shop_products = r["products"]
        seen = set()
        combined = []
        for x in lens_for_piece + shop_products:
            if x["link"] not in seen:
                seen.add(x["link"])
                combined.append(x)
        r["products"] = combined[:8]
        r["lens_count"] = len(lens_for_piece)

    return {"success": True, "pieces": results, "country": cc}


# ─── Claude identify crop ───
async def claude_identify_crop(img_bytes, cc="tr"):
    if not ANTHROPIC_API_KEY:
        return ""
    cfg = get_country_config(cc)
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((400, 400))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        b64 = base64.b64encode(img_bytes).decode()

    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 100,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                            {"type": "text", "text": f"This is a cropped clothing item. Write a 4-6 word {cfg['lang']} shopping search query for this EXACT item. Be ultra specific about type, color, pattern, length. Reply with ONLY the query, nothing else."},
                        ],
                    }],
                },
            )
            data = r.json()
            text = data.get("content", [{}])[0].get("text", "").strip()
            print(f"  Claude crop ID: '{text}'")
            return text
        except Exception as e:
            print(f"Claude crop err: {e}")
    return ""


@app.post("/api/manual-search")
async def manual_search(file: UploadFile = File(...), query: str = Form(""), country: str = Form("tr")):
    if not SERPAPI_KEY:
        raise HTTPException(500, "No API key")

    cc = country.lower()
    contents = await file.read()
    print(f"\n=== MANUAL SEARCH === country={cc} user_query='{query}'")

    upload_task = upload_img(contents)
    claude_task = claude_identify_crop(contents, cc)
    url, smart_query = await asyncio.gather(upload_task, claude_task)

    search_q = query if query else smart_query
    print(f"  Search query: '{search_q}'")

    async def do_lens():
        if url:
            return await asyncio.to_thread(_lens, url, cc)
        return []

    async def do_shop():
        if search_q:
            return await asyncio.to_thread(_shop, search_q, cc, 6)
        return []

    lens_res, shop_res = await asyncio.gather(do_lens(), do_shop())
    print(f"  Manual Lens: {len(lens_res)}, Shop: {len(shop_res)}")

    seen = set()
    combined = []
    for x in shop_res + lens_res:
        if x["link"] not in seen:
            seen.add(x["link"])
            combined.append(x)

    return {"success": True, "products": combined[:10], "lens_count": len(lens_res), "query_used": search_q, "country": cc}


@app.get("/api/health")
async def health():
    return {"status": "ok", "serpapi": bool(SERPAPI_KEY), "anthropic": bool(ANTHROPIC_API_KEY)}


@app.get("/api/countries")
async def countries():
    return {cc: {"name": cfg["name"], "currency": cfg["currency"]} for cc, cfg in COUNTRIES.items()}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>FitFinder</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.css" rel="stylesheet">
<style>
:root{--bg:#0a0a0c;--card:#131315;--border:#222;--text:#f0ece4;--muted:#8a8880;--dim:#555;--accent:#d4a853;--green:#6fcf7c;--red:#e85d5d}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;display:flex;justify-content:center}
::-webkit-scrollbar{display:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
.app{width:100%;max-width:440px;min-height:100vh}
.btn-main{border:none;border-radius:14px;padding:15px;width:100%;font:700 15px 'DM Sans',sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-gold{background:var(--accent);color:var(--bg)}
.btn-green{background:var(--green);color:var(--bg)}
.btn-outline{background:transparent;color:var(--accent);border:2px solid var(--accent) !important}
.crop-container{position:relative;width:100%;max-height:400px;margin:12px 0;border-radius:14px;overflow:hidden;background:#111}
.crop-container img{display:block;max-width:100%}
.hero{border-radius:14px;overflow:hidden;background:var(--card);border:1px solid var(--green);margin-bottom:10px;position:relative}
.hero img{width:100%;height:200px;object-fit:cover;display:block}
.hero .badge{position:absolute;top:10px;left:10px;background:var(--green);color:var(--bg);font-size:10px;font-weight:800;padding:4px 10px;border-radius:6px}
.hero .info{padding:12px 14px}
.hero .t{font-size:14px;font-weight:700}
.hero .s{font-size:11px;color:var(--muted);margin-top:2px}
.hero .row{display:flex;align-items:center;justify-content:space-between;margin-top:8px}
.hero .price{font-size:20px;font-weight:800;color:var(--accent)}
.hero .btn{background:var(--green);color:var(--bg);border:none;border-radius:8px;padding:8px 16px;font:700 12px 'DM Sans',sans-serif;cursor:pointer}
.piece{margin-bottom:28px;animation:fadeUp .4s ease both}
.p-hdr{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.p-title{font-size:16px;font-weight:700}
.p-brand{font-size:9px;font-weight:700;color:var(--bg);background:var(--accent);padding:2px 7px;border-radius:4px;margin-left:6px}
.scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}
.card{flex-shrink:0;width:135px;background:var(--card);border-radius:10px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text)}
.card.local{border-color:var(--accent)}
.card img{width:135px;height:110px;object-fit:cover;display:block}
.card .ci{padding:8px}
.card .cn{font-size:10px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .cs{font-size:9px;color:var(--dim);margin-top:2px}
.card .cp{font-size:13px;font-weight:700;color:var(--accent);margin-top:3px}
.bnav{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:440px;background:rgba(10,10,12,.93);backdrop-filter:blur(20px);border-top:1px solid var(--border);display:flex;padding:8px 0 22px;z-index:50}
</style>
</head>
<body>
<div class="app">
  <div id="home" style="padding:0 20px">
    <div style="padding-top:56px;padding-bottom:28px">
      <p style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:10px">FitFinder</p>
      <h1 id="heroTitle" style="font-size:32px;font-weight:700;line-height:1.15"></h1>
      <p id="heroSub" style="font-size:14px;color:var(--muted);margin-top:12px;line-height:1.5"></p>
    </div>
    <div onclick="document.getElementById('fi').click()" style="background:var(--accent);border-radius:14px;padding:18px 24px;display:flex;align-items:center;gap:14px;cursor:pointer;margin-bottom:16px">
      <div style="font-size:24px">&#x1F4F7;</div>
      <div><div id="uploadTitle" style="font-size:16px;font-weight:700;color:var(--bg)"></div><div id="uploadSub" style="font-size:12px;color:rgba(0,0,0,.45)"></div></div>
    </div>
    <input type="file" id="fi" accept="image/*" style="display:none">
    <div style="margin-top:32px;display:flex;flex-direction:column;gap:14px;padding-bottom:100px">
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px">&#x1F916;</span><div><div id="feat1" style="font-size:13px;font-weight:600"></div><div id="feat1d" style="font-size:11px;color:var(--muted)"></div></div></div>
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px">&#x2702;&#xFE0F;</span><div><div id="feat2" style="font-size:13px;font-weight:600"></div><div id="feat2d" style="font-size:11px;color:var(--muted)"></div></div></div>
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px" id="flagIcon"></span><div><div id="feat3" style="font-size:13px;font-weight:600"></div><div id="feat3d" style="font-size:11px;color:var(--muted)"></div></div></div>
    </div>
  </div>

  <div id="rScreen" style="display:none">
    <div style="position:sticky;top:0;z-index:40;background:rgba(10,10,12,.9);backdrop-filter:blur(20px);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
      <div onclick="goHome()" style="cursor:pointer;color:var(--muted);font-size:13px" id="backBtn"></div>
      <div style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2.5px">FITFINDER</div>
      <div style="width:40px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <div style="border-radius:14px;overflow:hidden;margin:14px 0;position:relative;background:#111">
        <img id="prev" src="" style="width:100%;display:block;object-fit:cover;max-height:260px">
        <div style="position:absolute;inset:0;background:linear-gradient(transparent 50%,var(--bg));pointer-events:none"></div>
      </div>
      <div id="actionBtns" style="display:flex;flex-direction:column;gap:10px">
        <button class="btn-main btn-gold" onclick="autoScan()" id="btnAuto"></button>
        <button class="btn-main btn-outline" onclick="startManual()" id="btnManual"></button>
      </div>
      <div id="cropMode" style="display:none">
        <p id="cropHint" style="font-size:13px;color:var(--accent);font-weight:600;margin-bottom:8px;text-align:center"></p>
        <div class="crop-container"><img id="cropImg" src=""></div>
        <input id="manualQ" style="width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--text);font:14px 'DM Sans',sans-serif;margin:10px 0">
        <button class="btn-main btn-green" onclick="cropAndSearch()" id="btnFind"></button>
        <button class="btn-main btn-outline" onclick="cancelManual()" style="margin-top:8px;font-size:13px" id="btnCancel"></button>
      </div>
      <div id="ld" style="display:none"></div>
      <div id="err" style="display:none"></div>
      <div id="res" style="display:none"></div>
    </div>
  </div>

  <div class="bnav">
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer" onclick="goHome()"><div style="font-size:20px;color:var(--accent)">&#x2B21;</div><div id="navHome" style="font-size:10px;font-weight:600;color:var(--accent)"></div></div>
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer"><div style="font-size:20px;color:var(--dim)">&#x2661;</div><div id="navFav" style="font-size:10px;font-weight:600;color:var(--dim)"></div></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.js"></script>
<script>
var IC={hat:"\u{1F9E2}",sunglasses:"\u{1F576}",top:"\u{1F455}",jacket:"\u{1F9E5}",bag:"\u{1F45C}",accessory:"\u{1F48D}",watch:"\u{231A}",bottom:"\u{1F456}",dress:"\u{1F457}",shoes:"\u{1F45F}",scarf:"\u{1F9E3}"};
var cF=null,cPrev=null,cropper=null,CC='us';

// ─── i18n ───
var L={
  tr:{
    flag:"\u{1F1F9}\u{1F1F7}",heroTitle:'Gorseldeki outfiti<br><span style="color:var(--accent)">birebir</span> bul.',
    heroSub:'Fotograf yukle, AI parcalari tespit etsin<br>veya kendin sec, Google Lens bulsun.',
    upload:'Fotograf Yukle',uploadSub:'Galeri veya screenshot',
    auto:'\u{1F916} Otomatik Tara',manual:'\u{2702}\u{FE0F} Kendim Seceyim',
    feat1:'Otomatik Tara',feat1d:'AI tum parcalari tespit edip arar',
    feat2:'Kendim Seceyim',feat2d:'Parmaginla parcayi sec, birebir bul',
    feat3:'Yerel Magazalar',feat3d:'Trendyol, Zara TR, Bershka TR, H&M TR',
    back:'\u2190 Geri',cropHint:'\u{1F447} Aramak istedigin parcayi cercevele',
    manualPh:'Opsiyonel: ne aradigini yaz (ornek: siyah deri ceket)',
    find:'\u{1F50D} Bu Parcayi Bul',cancel:'\u2190 Vazgec',
    loading:'Parcalar tespit ediliyor...',loadingManual:'Sectigin parca araniyor...',
    noResult:'Sonuc bulunamadi',noProd:'Urun bulunamadi',
    retry:'\u{2702}\u{FE0F} Kendim Seceyim ile Tekrar Dene',another:'\u{2702}\u{FE0F} Baska Parca Sec',
    selected:'Sectigin Parca',lensMatch:'Lens eslesmesi',
    recommended:'\u{2728} Onerilen',lensLabel:'\u{1F3AF} Lens Eslesmesi',
    goStore:'Magazaya Git \u2197',noPrice:'Fiyat icin tikla',
    alts:'\u{1F4B8} Alternatifler \u{1F449}',
    navHome:'Kesfet',navFav:'Favoriler'
  },
  en:{
    flag:"\u{1F1FA}\u{1F1F8}",heroTitle:'Find the outfit<br>in the photo, <span style="color:var(--accent)">exactly</span>.',
    heroSub:'Upload a photo, AI detects each piece<br>or select manually, Google Lens finds it.',
    upload:'Upload Photo',uploadSub:'Gallery or screenshot',
    auto:'\u{1F916} Auto Scan',manual:'\u{2702}\u{FE0F} Select Myself',
    feat1:'Auto Scan',feat1d:'AI detects all pieces and searches',
    feat2:'Select Myself',feat2d:'Select the piece with your finger, find exact match',
    feat3:'Local Stores',feat3d:'',
    back:'\u2190 Back',cropHint:'\u{1F447} Frame the piece you want to find',
    manualPh:'Optional: describe what you\'re looking for (e.g. black leather jacket)',
    find:'\u{1F50D} Find This Piece',cancel:'\u2190 Cancel',
    loading:'Detecting pieces...',loadingManual:'Searching for your selection...',
    noResult:'No results found',noProd:'No products found',
    retry:'\u{2702}\u{FE0F} Try Select Myself',another:'\u{2702}\u{FE0F} Select Another Piece',
    selected:'Your Selection',lensMatch:'Lens match',
    recommended:'\u{2728} Recommended',lensLabel:'\u{1F3AF} Lens Match',
    goStore:'Go to Store \u2197',noPrice:'Click for price',
    alts:'\u{1F4B8} Alternatives \u{1F449}',
    navHome:'Explore',navFav:'Favorites'
  },
  de:{
    flag:"\u{1F1E9}\u{1F1EA}",heroTitle:'Finde das Outfit<br>im Foto, <span style="color:var(--accent)">genau so</span>.',
    heroSub:'Lade ein Foto hoch, KI erkennt jedes Teil<br>oder wahle selbst, Google Lens findet es.',
    upload:'Foto hochladen',uploadSub:'Galerie oder Screenshot',
    auto:'\u{1F916} Automatisch scannen',manual:'\u{2702}\u{FE0F} Selbst wahlen',
    feat1:'Automatisch scannen',feat1d:'KI erkennt alle Teile und sucht',
    feat2:'Selbst wahlen',feat2d:'Wahle das Teil mit dem Finger',
    feat3:'Lokale Shops',feat3d:'Zalando, About You, Otto',
    back:'\u2190 Zuruck',cropHint:'\u{1F447} Rahme das Teil ein, das du finden willst',
    manualPh:'Optional: beschreibe was du suchst (z.B. schwarze Lederjacke)',
    find:'\u{1F50D} Dieses Teil finden',cancel:'\u2190 Abbrechen',
    loading:'Teile werden erkannt...',loadingManual:'Deine Auswahl wird gesucht...',
    noResult:'Keine Ergebnisse',noProd:'Keine Produkte gefunden',
    retry:'\u{2702}\u{FE0F} Selbst wahlen',another:'\u{2702}\u{FE0F} Anderes Teil wahlen',
    selected:'Deine Auswahl',lensMatch:'Lens Treffer',
    recommended:'\u{2728} Empfohlen',lensLabel:'\u{1F3AF} Lens Treffer',
    goStore:'Zum Shop \u2197',noPrice:'Preis ansehen',
    alts:'\u{1F4B8} Alternativen \u{1F449}',
    navHome:'Entdecken',navFav:'Favoriten'
  },
  fr:{
    flag:"\u{1F1EB}\u{1F1F7}",heroTitle:'Trouvez la tenue<br>sur la photo, <span style="color:var(--accent)">exactement</span>.',
    heroSub:'Telechargez une photo, l\'IA detecte chaque piece<br>ou selectionnez manuellement.',
    upload:'Telecharger photo',uploadSub:'Galerie ou capture',
    auto:'\u{1F916} Scan auto',manual:'\u{2702}\u{FE0F} Choisir moi-meme',
    feat1:'Scan auto',feat1d:'L\'IA detecte toutes les pieces',
    feat2:'Choisir moi-meme',feat2d:'Selectionnez la piece avec votre doigt',
    feat3:'Boutiques locales',feat3d:'Galeries Lafayette, La Redoute',
    back:'\u2190 Retour',cropHint:'\u{1F447} Cadrez la piece que vous cherchez',
    manualPh:'Optionnel: decrivez ce que vous cherchez',
    find:'\u{1F50D} Trouver cette piece',cancel:'\u2190 Annuler',
    loading:'Detection en cours...',loadingManual:'Recherche en cours...',
    noResult:'Aucun resultat',noProd:'Aucun produit trouve',
    retry:'\u{2702}\u{FE0F} Reessayer manuellement',another:'\u{2702}\u{FE0F} Autre piece',
    selected:'Votre selection',lensMatch:'Correspondance Lens',
    recommended:'\u{2728} Recommande',lensLabel:'\u{1F3AF} Correspondance Lens',
    goStore:'Voir boutique \u2197',noPrice:'Voir le prix',
    alts:'\u{1F4B8} Alternatives \u{1F449}',
    navHome:'Explorer',navFav:'Favoris'
  },
  ar:{
    flag:"\u{1F1F8}\u{1F1E6}",heroTitle:'اعثر على الإطلالة<br>في الصورة <span style="color:var(--accent)">بالضبط</span>.',
    heroSub:'ارفع صورة، الذكاء الاصطناعي يكتشف كل قطعة<br>أو اختر بنفسك.',
    upload:'ارفع صورة',uploadSub:'المعرض أو لقطة شاشة',
    auto:'\u{1F916} مسح تلقائي',manual:'\u{2702}\u{FE0F} أختار بنفسي',
    feat1:'مسح تلقائي',feat1d:'الذكاء الاصطناعي يكتشف كل القطع',
    feat2:'أختار بنفسي',feat2d:'اختر القطعة بإصبعك',
    feat3:'متاجر محلية',feat3d:'نمشي، أوناس، سيفي',
    back:'رجوع \u2190',cropHint:'\u{1F447} حدد القطعة التي تبحث عنها',
    manualPh:'اختياري: صف ما تبحث عنه',
    find:'\u{1F50D} ابحث عن هذه القطعة',cancel:'إلغاء \u2190',
    loading:'جاري اكتشاف القطع...',loadingManual:'جاري البحث...',
    noResult:'لا توجد نتائج',noProd:'لم يتم العثور على منتجات',
    retry:'\u{2702}\u{FE0F} حاول الاختيار يدوياً',another:'\u{2702}\u{FE0F} اختر قطعة أخرى',
    selected:'اختيارك',lensMatch:'تطابق Lens',
    recommended:'\u{2728} مُوصى به',lensLabel:'\u{1F3AF} تطابق Lens',
    goStore:'زيارة المتجر \u2197',noPrice:'انقر للسعر',
    alts:'\u{1F4B8} بدائل \u{1F449}',
    navHome:'استكشف',navFav:'المفضلة'
  },
  nl:{
    flag:"\u{1F1F3}\u{1F1F1}",heroTitle:'Vind de outfit<br>op de foto, <span style="color:var(--accent)">precies zo</span>.',
    heroSub:'Upload een foto, AI herkent elk kledingstuk<br>of selecteer zelf.',
    upload:'Foto uploaden',uploadSub:'Galerij of screenshot',
    auto:'\u{1F916} Automatisch scannen',manual:'\u{2702}\u{FE0F} Zelf selecteren',
    feat1:'Automatisch scannen',feat1d:'AI herkent alle items en zoekt',
    feat2:'Zelf selecteren',feat2d:'Selecteer het item met je vinger',
    feat3:'Lokale winkels',feat3d:'Zalando, De Bijenkorf, Wehkamp',
    back:'\u2190 Terug',cropHint:'\u{1F447} Kader het item dat je wilt vinden',
    manualPh:'Optioneel: beschrijf wat je zoekt',
    find:'\u{1F50D} Dit item vinden',cancel:'\u2190 Annuleren',
    loading:'Items worden herkend...',loadingManual:'Je selectie wordt gezocht...',
    noResult:'Geen resultaten',noProd:'Geen producten gevonden',
    retry:'\u{2702}\u{FE0F} Zelf selecteren',another:'\u{2702}\u{FE0F} Ander item selecteren',
    selected:'Je selectie',lensMatch:'Lens match',
    recommended:'\u{2728} Aanbevolen',lensLabel:'\u{1F3AF} Lens Match',
    goStore:'Naar winkel \u2197',noPrice:'Klik voor prijs',
    alts:'\u{1F4B8} Alternatieven \u{1F449}',
    navHome:'Ontdek',navFav:'Favorieten'
  }
};

var STORE_NAMES={
  tr:'Trendyol, Zara TR, Bershka TR, H&M TR',
  us:'Nordstrom, Macy\'s, ASOS, Urban Outfitters',
  gb:'ASOS, Selfridges, Harrods, John Lewis',
  de:'Zalando, About You, Otto',
  fr:'Galeries Lafayette, La Redoute',
  sa:'Namshi, Ounass, Sivvi',
  ae:'Namshi, 6th Street, Ounass',
  nl:'Zalando, De Bijenkorf, Wehkamp'
};

var FLAGS={tr:"\u{1F1F9}\u{1F1F7}",us:"\u{1F1FA}\u{1F1F8}",gb:"\u{1F1EC}\u{1F1E7}",de:"\u{1F1E9}\u{1F1EA}",fr:"\u{1F1EB}\u{1F1F7}",sa:"\u{1F1F8}\u{1F1E6}",ae:"\u{1F1E6}\u{1F1EA}",nl:"\u{1F1F3}\u{1F1F1}"};
var CC_LANG={tr:'tr',us:'en',gb:'en',de:'de',fr:'fr',sa:'ar',ae:'en',nl:'nl'};

function detectCountry(){
  var tz=(Intl.DateTimeFormat().resolvedOptions().timeZone||'').toLowerCase();
  var lang=(navigator.language||'').toLowerCase();
  if(tz.indexOf('istanbul')>-1||lang.startsWith('tr'))return'tr';
  if(tz.indexOf('riyadh')>-1||tz.indexOf('asia/riyadh')>-1)return'sa';
  if(tz.indexOf('dubai')>-1||tz.indexOf('asia/dubai')>-1)return'ae';
  if(tz.indexOf('amsterdam')>-1||lang.startsWith('nl'))return'nl';
  if(tz.indexOf('berlin')>-1||tz.indexOf('europe/berlin')>-1||tz.indexOf('europe/vienna')>-1||tz.indexOf('europe/zurich')>-1&&lang.startsWith('de'))return'de';
  if(tz.indexOf('paris')>-1||tz.indexOf('europe/paris')>-1||lang.startsWith('fr'))return'fr';
  if(tz.indexOf('london')>-1||tz.indexOf('europe/london')>-1)return'gb';
  if(tz.indexOf('america')>-1||lang.startsWith('en-us'))return'us';
  if(lang.startsWith('en-gb'))return'gb';
  if(lang.startsWith('ar'))return'sa';
  return'us';
}

CC=detectCountry();

function t(key){var lg=CC_LANG[CC]||'en';return(L[lg]||L.en)[key]||(L.en)[key]||key}

function applyLang(){
  document.getElementById('heroTitle').innerHTML=t('heroTitle');
  document.getElementById('heroSub').innerHTML=t('heroSub');
  document.getElementById('uploadTitle').textContent=t('upload');
  document.getElementById('uploadSub').textContent=t('uploadSub');
  document.getElementById('feat1').textContent=t('feat1');
  document.getElementById('feat1d').textContent=t('feat1d');
  document.getElementById('feat2').textContent=t('feat2');
  document.getElementById('feat2d').textContent=t('feat2d');
  document.getElementById('feat3').textContent=t('feat3');
  document.getElementById('feat3d').textContent=STORE_NAMES[CC]||'';
  document.getElementById('flagIcon').textContent=FLAGS[CC]||'';
  document.getElementById('btnAuto').innerHTML=t('auto');
  document.getElementById('btnManual').innerHTML=t('manual');
  document.getElementById('backBtn').textContent=t('back');
  document.getElementById('cropHint').textContent=t('cropHint');
  document.getElementById('manualQ').placeholder=t('manualPh');
  document.getElementById('btnFind').innerHTML=t('find');
  document.getElementById('btnCancel').innerHTML=t('cancel');
  document.getElementById('navHome').textContent=t('navHome');
  document.getElementById('navFav').textContent=t('navFav');
  if(CC_LANG[CC]==='ar')document.documentElement.setAttribute('dir','rtl');
}
applyLang();

function getCC(){return CC}

document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});

function loadF(f){if(!f.type.startsWith('image/'))return;cF=f;var r=new FileReader();r.onload=function(e){cPrev=e.target.result;showScreen()};r.readAsDataURL(f)}

function showScreen(){
  document.getElementById('home').style.display='none';
  document.getElementById('rScreen').style.display='block';
  document.getElementById('prev').src=cPrev;
  document.getElementById('prev').style.maxHeight='260px';
  document.getElementById('prev').style.display='block';
  document.getElementById('actionBtns').style.display='flex';
  document.getElementById('cropMode').style.display='none';
  document.getElementById('ld').style.display='none';
  document.getElementById('err').style.display='none';
  document.getElementById('res').style.display='none';
  if(cropper){cropper.destroy();cropper=null}
}
function goHome(){document.getElementById('home').style.display='block';document.getElementById('rScreen').style.display='none';if(cropper){cropper.destroy();cropper=null}cF=null;cPrev=null}

function autoScan(){
  document.getElementById('actionBtns').style.display='none';
  showLoading(t('loading'));
  var fd=new FormData();fd.append('file',cF);fd.append('country',getCC());
  fetch('/api/full-analyze',{method:'POST',body:fd})
    .then(function(r){return r.json()})
    .then(function(d){hideLoading();if(!d.success)return showErr(d.message||'Error');renderAuto(d)})
    .catch(function(e){hideLoading();showErr(e.message)});
}

function startManual(){
  document.getElementById('actionBtns').style.display='none';
  document.getElementById('prev').style.display='none';
  document.getElementById('cropMode').style.display='block';
  document.getElementById('cropImg').src=cPrev;
  document.getElementById('manualQ').value='';
  setTimeout(function(){
    if(cropper)cropper.destroy();
    cropper=new Cropper(document.getElementById('cropImg'),{viewMode:1,dragMode:'move',autoCropArea:0.5,responsive:true,background:false,guides:true,highlight:true,cropBoxMovable:true,cropBoxResizable:true});
  },100);
}
function cancelManual(){if(cropper){cropper.destroy();cropper=null}document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex'}

function cropAndSearch(){
  if(!cropper)return;
  var canvas=cropper.getCroppedCanvas({maxWidth:800,maxHeight:800});
  if(!canvas)return;
  document.getElementById('cropMode').style.display='none';
  document.getElementById('prev').style.display='block';
  showLoading(t('loadingManual'));
  canvas.toBlob(function(blob){
    var q=document.getElementById('manualQ').value.trim();
    var fd=new FormData();fd.append('file',blob,'crop.jpg');fd.append('query',q);fd.append('country',getCC());
    fetch('/api/manual-search',{method:'POST',body:fd})
      .then(function(r){return r.json()})
      .then(function(d){hideLoading();if(!d.success)return showErr('Error');renderManual(d,canvas.toDataURL('image/jpeg',0.7))})
      .catch(function(e){hideLoading();showErr(e.message)});
  },'image/jpeg',0.85);
  if(cropper){cropper.destroy();cropper=null}
}

function showLoading(txt){var l=document.getElementById('ld');l.style.display='block';l.innerHTML='<div style="display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:16px;border:1px solid var(--border);margin:14px 0"><div style="width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div style="font-size:13px;font-weight:600">'+txt+'</div></div>'}
function hideLoading(){document.getElementById('ld').style.display='none'}
function showErr(m){var e=document.getElementById('err');e.style.display='block';e.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px;margin:12px 0;font-size:13px;color:var(--red)">'+m+'</div>'}

function renderAuto(d){
  document.getElementById('prev').style.maxHeight='160px';
  var pieces=d.pieces||[];
  var ra=document.getElementById('res');ra.style.display='block';
  var h='';
  for(var i=0;i<pieces.length;i++){
    var p=pieces[i],pr=p.products||[],lc=p.lens_count||0;
    var hero=pr[0],alts=pr.slice(1);
    h+='<div class="piece" style="animation-delay:'+(i*.1)+'s">';
    h+='<div class="p-hdr">';
    h+='<div style="width:52px;height:52px;border-radius:10px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:22px;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';
    h+='<div><span class="p-title">'+(p.short_title||p.category)+'</span>';
    if(p.brand&&p.brand!=='?')h+='<span class="p-brand">'+p.brand+'</span>';
    var vt=p.visible_text||'';
    if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';
    if(lc>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+lc+' '+t('lensMatch')+'</div>';
    h+='</div></div>';
    if(!hero){h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div></div>';continue}
    h+=heroHTML(hero,lc>0);
    if(alts.length>0)h+=altsHTML(alts);
    h+='</div>';
  }
  if(!pieces.length)h='<div style="text-align:center;padding:40px;color:var(--dim)">'+t('noResult')+'</div>';
  ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('retry')+'</button>';
}

function renderManual(d,cropSrc){
  document.getElementById('prev').style.maxHeight='160px';
  var pr=d.products||[];
  var ra=document.getElementById('res');ra.style.display='block';
  var h='<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><img src="'+cropSrc+'" style="width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid var(--accent)"><div><span class="p-title">'+t('selected')+'</span>';
  if(d.query_used)h+='<div style="font-size:10px;color:var(--accent);margin-top:2px">\u{1F50D} "'+d.query_used+'"</div>';
  if(d.lens_count>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+d.lens_count+' '+t('lensMatch')+'</div>';
  h+='</div></div>';
  if(pr.length>0){h+=heroHTML(pr[0],d.lens_count>0);if(pr.length>1)h+=altsHTML(pr.slice(1))}
  else h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div>';
  ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('another')+'</button>';
}

function heroHTML(p,isLens){
  var img=p.image||p.thumbnail||'';
  var h='<a href="'+p.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)"><div class="hero">';
  if(img)h+='<img src="'+img+'" onerror="if(this.src!==\''+p.thumbnail+'\')this.src=\''+p.thumbnail+'\'">';
  h+='<div class="badge">'+(isLens?t('lensLabel'):t('recommended'))+'</div>';
  h+='<div class="info"><div class="t">'+p.title+'</div><div class="s">'+(p.brand||p.source||'')+'</div>';
  h+='<div class="row"><span class="price">'+(p.price||t('noPrice'))+'</span>';
  h+='<button class="btn">'+t('goStore')+'</button></div></div></div></a>';
  return h;
}
function altsHTML(list){
  var h='<div style="font-size:11px;color:var(--dim);margin:6px 0">'+t('alts')+'</div><div class="scroll">';
  for(var i=0;i<list.length;i++){var a=list[i];var img=a.thumbnail||a.image||'';
    h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="card'+(a.is_local?' local':'')+'">';
    if(img)h+='<img src="'+img+'" onerror="this.hidden=true">';
    h+='<div class="ci"><div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div></a>'}
  return h+'</div>';
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
