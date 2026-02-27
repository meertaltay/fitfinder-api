import os
import re
import io
import json
import base64
import asyncio
import time
import httpx
import urllib.parse
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    print("✅ HEIC support enabled")
except ImportError:
    print("⚠️ pillow-heif not installed, HEIC files may fail")

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from serpapi import GoogleSearch

try:
    from rembg import remove as rembg_remove, new_session
    try:
        rembg_session = new_session("u2net_cloth_seg")
        print("✅ rembg loaded (u2net_cloth_seg)")
    except Exception:
        rembg_session = new_session("u2net")
        print("✅ rembg loaded (u2net fallback)")
    HAS_REMBG = True
except ImportError:
    rembg_session = None
    HAS_REMBG = False
    print("⚠️ rembg not installed")

app = FastAPI(title="FitFinder API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_SEM = asyncio.Semaphore(3)
REMBG_LOCK = asyncio.Lock()

_CACHE = {}
CACHE_TTL = 3600

def cache_get(key):
    if key in _CACHE:
        val, ts = _CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _CACHE[key]
    return None

def cache_set(key, val):
    _CACHE[key] = (val, time.time())
    if len(_CACHE) > 500:
        now = time.time()
        expired = [k for k, (_, ts) in _CACHE.items() if now - ts > CACHE_TTL]
        for k in expired:
            del _CACHE[k]
        if len(_CACHE) > 500:
            oldest = sorted(_CACHE.keys(), key=lambda k: _CACHE[k][1])[:50]
            for k in oldest:
                del _CACHE[k]

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID", "")

# ─── Country Configurations ───
COUNTRIES = {
    "tr": {"name": "Türkiye", "gl": "tr", "hl": "tr", "lang": "Turkish", "currency": "₺", "local_stores": ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep."], "gender": {"male": "erkek", "female": "kadın"}},
    "us": {"name": "United States", "gl": "us", "hl": "en", "lang": "English", "currency": "$", "local_stores": ["nordstrom.", "macys.", "bloomingdales.", "target.com", "walmart.com", "urbanoutfitters.", "freepeople.", "anthropologie.", "revolve.com", "shopbop.", "ssense.", "farfetch."], "gender": {"male": "men", "female": "women"}},
    "gb": {"name": "United Kingdom", "gl": "gb", "hl": "en", "lang": "English", "currency": "£", "local_stores": ["asos.com", "selfridges.", "harrods.", "johnlewis.", "next.co.uk", "boohoo.", "prettylittlething.", "missguided."], "gender": {"male": "men", "female": "women"}},
    "de": {"name": "Deutschland", "gl": "de", "hl": "de", "lang": "German", "currency": "€", "local_stores": ["zalando.", "aboutyou.", "otto.", "breuninger.", "peek-cloppenburg.", "bonprix."], "gender": {"male": "herren", "female": "damen"}},
    "fr": {"name": "France", "gl": "fr", "hl": "fr", "lang": "French", "currency": "€", "local_stores": ["galerieslafayette.", "laredoute.", "veepee.", "printemps.", "sarenza."], "gender": {"male": "homme", "female": "femme"}},
    "sa": {"name": "Saudi Arabia", "gl": "sa", "hl": "ar", "lang": "Arabic", "currency": "SAR", "local_stores": ["namshi.", "ounass.", "sivvi.", "nisnass.", "styli.", "vogacloset."], "gender": {"male": "men", "female": "women"}},
    "ae": {"name": "UAE", "gl": "ae", "hl": "en", "lang": "English", "currency": "AED", "local_stores": ["namshi.", "ounass.", "sivvi.", "nisnass.", "6thstreet.", "bloomingdales.ae"], "gender": {"male": "men", "female": "women"}},
    "nl": {"name": "Netherlands", "gl": "nl", "hl": "nl", "lang": "Dutch", "currency": "€", "local_stores": ["zalando.", "debijenkorf.", "wehkamp.", "aboutyou."], "gender": {"male": "heren", "female": "dames"}},
}
DEFAULT_COUNTRY = "us"

def get_country_config(cc):
    return COUNTRIES.get(cc.lower(), COUNTRIES[DEFAULT_COUNTRY])

# ─── Affiliate ───
TRENDYOL_PARTNER_ID = os.environ.get("TRENDYOL_PARTNER_ID", "")
SKIMLINKS_ID = os.environ.get("SKIMLINKS_ID", "")

def make_affiliate(url):
    url_lower = url.lower()
    if TRENDYOL_PARTNER_ID and "trendyol.com" in url_lower:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        query["boutiqueId"] = TRENDYOL_PARTNER_ID
        query["merchantId"] = TRENDYOL_PARTNER_ID
        new_query = urllib.parse.urlencode(query)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    if SKIMLINKS_ID:
        encoded = urllib.parse.quote(url, safe='')
        return f"https://go.skimresources.com/?id={SKIMLINKS_ID}&url={encoded}"
    return url

BRAND_MAP = {
    "trendyol.com": "Trendyol", "hepsiburada.com": "Hepsiburada", "boyner.com.tr": "Boyner", "defacto.com": "DeFacto",
    "lcwaikiki.com": "LC Waikiki", "koton.com": "Koton", "beymen.com": "Beymen", "n11.com": "N11", "flo.com.tr": "FLO",
    "mavi.com": "Mavi", "superstep.com.tr": "Superstep", "zara.com": "Zara", "bershka.com": "Bershka",
    "pullandbear.com": "Pull&Bear", "hm.com": "H&M", "mango.com": "Mango", "asos.com": "ASOS",
    "stradivarius.com": "Stradivarius", "massimodutti.com": "Massimo Dutti", "nike.com": "Nike",
    "adidas.": "Adidas", "puma.com": "Puma", "newbalance.": "New Balance", "converse.": "Converse",
}

BLOCKED = [
    "pinterest.", "instagram.", "facebook.", "twitter.", "x.com", "tiktok.", "youtube.", "reddit.", "tumblr.", "blogspot.",
    "wordpress.", "medium.com", "threads.net", "etsy.com", "ebay.com", "ebay.", "amazon.com", "aliexpress.",
    "wish.com", "dhgate.", "alibaba.", "tripadvisor.", "booking.", "yelp.", "lemon8.", "lookbook.", "chictopia.",
    "polyvore.", "weheartit.", "stylevore.", "fashmates.", "taobao.", "tmall.", "jd.com", "1688.com", "pinduoduo.",
    "forlady", "chicisimo.", "bantoa.", "wear.jp", "shutterstock.", "gettyimages.", "alamy.", "dreamstime.",
    "vecteezy.", "freepik.", "unsplash.", "pexels.", "shein.", "temu.", "cider.", "romwe.", "patpat.",
    "rightmove.", "zillow.", "realtor.", "selency.", "1stdibs.", "pamono.", "chairish.", "wayfair.", "ikea.",
]

FASHION_DOMAINS = [
    "trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.",
    "superstep.", "zara.com", "bershka.com", "pullandbear.com", "hm.com", "mango.com", "asos.com", "stradivarius.com",
    "massimodutti.com", "nike.com", "adidas.", "puma.com", "newbalance.", "converse.", "morhipo.", "lidyana.", "modanisa.",
]

FASHION_KW = ["ceket", "mont", "kaban", "bomber", "jacket", "coat", "blazer", "pantolon", "jean", "denim", "pants",
    "jogger", "chino", "gomlek", "tisort", "sweatshirt", "hoodie", "kazak", "shirt", "elbise", "etek",
    "dress", "skirt", "ayakkabi", "sneaker", "boot", "shoe", "bot", "canta", "bag", "sapka", "bere",
    "hat", "cap", "gozluk", "saat", "watch", "sunglasses", "erkek", "kadin", "giyim", "fashion", "wear", "clothing"]

def get_brand(link, src):
    c = (link + " " + src).lower()
    for d, b in BRAND_MAP.items():
        if d in c: return b
    return src if src else ""

def is_local(link, src, country_config):
    return any(d in (link + " " + src).lower() for d in country_config.get("local_stores", []))

def is_blocked(link):
    return any(d in link.lower() for d in BLOCKED)

def is_fashion(link, title, src):
    c = (link + " " + src).lower()
    if any(d in c for d in FASHION_DOMAINS): return True
    t = (title + " " + src).lower()
    return any(k in t for k in FASHION_KW)

# ─── Brand Filter ───
RIVAL_BRANDS = ["nike", "adidas", "puma", "zara", "hm", "bershka", "mango", "gucci", "prada", "balenciaga",
    "converse", "vans", "new balance", "reebok", "asics", "fila", "skechers", "kinetix", "defacto",
    "koton", "lcw", "mavi", "colins", "levi", "tommy", "polo", "lacoste", "calvin klein",
    "hugo boss", "massimo dutti", "pull&bear", "stradivarius", "uniqlo", "gap", "h&m"]

def filter_rival_brands(results, piece_brand):
    if not piece_brand or piece_brand == "?" or len(piece_brand) < 3: return results
    brand_lower = piece_brand.lower().strip()
    filtered = []
    for r in results:
        combined = (r.get("title", "") + " " + r.get("source", "") + " " + r.get("link", "")).lower()
        is_rival = False
        for rb in RIVAL_BRANDS:
            if re.search(rf'\b{re.escape(rb)}\b', combined) and rb not in brand_lower and brand_lower not in rb:
                is_rival = True; break
        if not is_rival:
            filtered.append(r)
    return filtered if filtered else results

# ─── Image Upload (Imgur → Catbox → Tmpfiles) ───
async def upload_img(img_bytes):
    async with httpx.AsyncClient(timeout=30) as c:
        if IMGUR_CLIENT_ID:
            try:
                r = await c.post("https://api.imgur.com/3/image", headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}, files={"image": ("i.jpg", img_bytes, "image/jpeg")})
                if r.status_code == 200:
                    link = r.json().get("data", {}).get("link", "")
                    if link: return link
            except Exception as e: print(f"Imgur err: {e}")
        try:
            r = await c.post("https://litterbox.catbox.moe/resources/internals/api.php", data={"reqtype": "fileupload", "time": "1h"}, files={"fileToUpload": ("i.jpg", img_bytes, "image/jpeg")})
            if r.status_code == 200 and r.text.startswith("http"):
                return r.text.strip()
        except Exception as e: print(f"Catbox err: {e}")
        try:
            r = await c.post("https://tmpfiles.org/api/v1/upload", files={"file": ("i.jpg", img_bytes, "image/jpeg")})
            if r.status_code == 200:
                u = r.json().get("data", {}).get("url", "")
                if u: return u.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        except Exception as e: print(f"Tmpfiles err: {e}")
    return None

# ─── Background Removal ───
def remove_bg(img_bytes):
    if not HAS_REMBG: return img_bytes
    try:
        result = rembg_remove(img_bytes, session=rembg_session)
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        bbox = img.getbbox()
        if bbox: img = img.crop(bbox)
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        pad = int(max(bg.size) * 0.05)
        bg = ImageOps.expand(bg, border=pad, fill='white')
        buf = io.BytesIO()
        bg.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception as e:
        print(f"  rembg err: {e}")
        return img_bytes

# ─── Auto-Crop (1000x1000 grid, 15% padding) ───
def crop_piece(img_obj, box):
    """Crop piece from PIL Image using box_2d [ymin, xmin, ymax, xmax]. Auto-detects scale."""
    try:
        w, h = img_obj.size
        ymin, xmin, ymax, xmax = [float(v) for v in box]

        # Scale normalization: 0-1, 0-100, or 0-1000
        if max(box) <= 1.0:
            pass
        elif max(box) <= 100.0:
            ymin, xmin, ymax, xmax = ymin/100, xmin/100, ymax/100, xmax/100
        else:
            ymin, xmin, ymax, xmax = ymin/1000, xmin/1000, ymax/1000, xmax/1000

        top = int(ymin * h)
        left = int(xmin * w)
        bottom = int(ymax * h)
        right = int(xmax * w)

        # 15% breathing room
        pad_y = int((bottom - top) * 0.15)
        pad_x = int((right - left) * 0.15)
        px1 = max(0, left - pad_x)
        py1 = max(0, top - pad_y)
        px2 = min(w, right + pad_x)
        py2 = min(h, bottom + pad_y)

        if px2 - px1 < 20 or py2 - py1 < 20:
            return None

        cropped = img_obj.crop((px1, py1, px2, py2))
        cropped.thumbnail((1024, 1024))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception as e:
        print(f"  Crop err: {e}")
        return None

# ─── Claude Reranker (Manual mode only) ───
async def claude_rerank(original_b64, results, cc="tr", expected_text=""):
    if not ANTHROPIC_API_KEY or len(results) < 2: return results
    candidates = results[:12]

    async with httpx.AsyncClient(timeout=10) as client:
        async def fetch_thumb(r):
            url = r.get("thumbnail") or r.get("image") or ""
            if not url: return None
            try:
                if url.startswith("data:image"):
                    raw = url.split(",", 1)[1] if "," in url else ""
                    if len(raw) > 100:
                        raw += "=" * ((4 - len(raw) % 4) % 4)
                        img_data = base64.b64decode(raw)
                        img = Image.open(io.BytesIO(img_data)).convert("RGB")
                        img.thumbnail((512, 512))
                        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=80)
                        return base64.b64encode(buf.getvalue()).decode()
                    return None
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 500:
                    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                    img.thumbnail((512, 512))
                    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=80)
                    return base64.b64encode(buf.getvalue()).decode()
            except Exception: pass
            return None
        thumb_data = await asyncio.gather(*[fetch_thumb(r) for r in candidates])

    target_info = f'\nUSER IS SPECIFICALLY LOOKING FOR: "{expected_text}"\n' if expected_text else ""
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": original_b64}},
        {"type": "text", "text": "This is the ORIGINAL item (Image 0)."},
    ]
    valid_indices = []
    for i, tb64 in enumerate(thumb_data):
        if tb64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tb64}})
            content.append({"type": "text", "text": f"Result #{i+1}: {candidates[i].get('title', '')}"})
            valid_indices.append(i)
    if len(valid_indices) < 2: return results

    content.append({"type": "text", "text": f"""You are an ULTRA-STRICT AI fashion authentication expert.{target_info}
Compare ORIGINAL (Image 0) with numbered results.

10/10: EXACT MATCH. 8-9: Almost identical. 5-7: Similar style. 0-4: Different, REJECT.

CRITICAL: Explain visual reasoning BEFORE scoring. Compare collars, zippers, pockets, logos.

Return ONLY valid JSON array sorted by highest score:
[{{"idx":1,"reason":"Silver asymmetrical zipper, identical lapels.","score":10}}]"""})

    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": "claude-3-7-sonnet-20250219", "max_tokens": 800, "messages": [{"role": "user", "content": content}]})
            text = r.json().get("content", [{}])[0].get("text", "").strip()
            text = re.sub(r'^```\w*\n?', '', text); text = re.sub(r'\n?```$', '', text)
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                rankings = json.loads(m.group())
                reranked, similar, used = [], [], set()
                for rank in rankings:
                    idx = rank.get("idx", 0) - 1
                    score = rank.get("score", 0)
                    if 0 <= idx < len(candidates) and idx not in used:
                        item = candidates[idx].copy()
                        item["match_score"] = score
                        if score >= 8:
                            item["ai_verified"] = True; reranked.append(item); used.add(idx)
                        elif score >= 5:
                            item["ai_verified"] = False; similar.append(item); used.add(idx)
                if reranked or similar: return reranked + similar
                return results[:4]
    except Exception as e: print(f"  Reranker err: {e}")
    return results

# ─── Claude Vision (1000x1000 grid) ───
async def claude_detect(img_b64, cc="tr"):
    if not ANTHROPIC_API_KEY: return None
    cfg = get_country_config(cc)
    lang, g_m, g_f = cfg["lang"], cfg["gender"]["male"], cfg["gender"]["female"]
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": "claude-3-7-sonnet-20250219", "max_tokens": 1500,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": f"""Analyze every clothing item and accessory this person is wearing.

FIRST: Determine gender: "{g_m}" (male) or "{g_f}" (female)

RULES:
1. ONLY list items CLEARLY VISIBLE as SEPARATE garments
2. A collar/lining peeking under a jacket is NOT a separate piece
3. If less than 30% visible, do NOT list it
4. Do NOT guess hidden items
5. Read ALL visible text, logos, brand names

For each item:
- category: hat|sunglasses|scarf|jacket|top|bottom|dress|shoes|bag|watch|accessory
- short_title: 2-4 word {lang} name
- color: in {lang}
- brand: If readable or design strongly resembles famous brand, write it. Else "?"
- visible_text: ALL readable text/logos
- search_query: 4-6 word ULTRA SPECIFIC {lang} shopping query with gender, brand, iconic details.

Return ONLY valid JSON array:
[{{"category":"","short_title":"","color":"","brand":"","visible_text":"","search_query":""}}]"""},
                    ]}],
                })
            data = r.json()
            if "error" in data:
                print(f"Claude error: {data['error']}"); return None
            text = data.get("content", [{}])[0].get("text", "").strip()
            text = re.sub(r'^```\w*\n?', '', text); text = re.sub(r'\n?```$', '', text)
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"Claude err: {e}")
    return None

# ─── Google Lens ───
DUPE_SITES = ["shein.", "temu.", "aliexpress.", "alibaba.", "cider.", "dhgate.", "wish.", "romwe.", "patpat."]

def _lens(url, cc="tr"):
    cfg = get_country_config(cc)
    res, seen = [], set()
    try:
        d = GoogleSearch({"engine": "google_lens", "url": url, "api_key": SERPAPI_KEY, "hl": cfg["hl"], "country": cfg["gl"]}).get_dict()
        for m in d.get("visual_matches", []):
            lnk, ttl, src = m.get("link", ""), m.get("title", ""), m.get("source", "")
            if not lnk or not ttl or lnk in seen: continue
            if is_blocked(lnk) or not is_fashion(lnk, ttl, src): continue
            seen.add(lnk)
            pr = m.get("price", {})
            res.append({
                "title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk), "price": pr.get("value", "") if isinstance(pr, dict) else str(pr),
                "thumbnail": m.get("thumbnail", ""), "image": m.get("image", ""),
                "is_local": is_local(lnk, src, cfg),
            })
            if len(res) >= 20: break
    except Exception as e: print(f"Lens err: {e}")

    def score(r):
        s = 0
        if r["price"]: s += 10
        if r["is_local"]: s += 5
        c = (r["link"] + " " + r["source"]).lower()
        if any(d in c for d in FASHION_DOMAINS): s += 3
        if any(d in c for d in DUPE_SITES): s -= 50
        return -s
    res.sort(key=score)
    return res

# ─── Google Shopping ───
def _shop(q, cc="tr", limit=6):
    cache_key = f"shop:{cc}:{q}"
    cached = cache_get(cache_key)
    if cached: return cached
    cfg = get_country_config(cc)
    res, seen = [], set()
    try:
        d = GoogleSearch({"engine": "google_shopping", "q": q, "gl": cfg["gl"], "hl": cfg["hl"], "api_key": SERPAPI_KEY}).get_dict()
        for item in d.get("shopping_results", []):
            lnk = item.get("product_link") or item.get("link", "")
            ttl, src = item.get("title", ""), item.get("source", "")
            if not lnk or not ttl or lnk in seen or is_blocked(lnk): continue
            seen.add(lnk)
            res.append({
                "title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk),
                "price": item.get("price", str(item.get("extracted_price", ""))),
                "thumbnail": item.get("thumbnail", ""), "image": "",
                "is_local": is_local(lnk, src, cfg),
            })
            if len(res) >= limit: break
    except Exception as e: print(f"Shop err: {e}")
    if res: cache_set(cache_key, res)
    return res

# ─── PIECE KEYWORDS (multilingual, for matching Lens → pieces) ───
PIECE_KEYWORDS = {
    "hat": ["hat", "beanie", "cap", "bere", "şapka", "sapka", "kepçe", "bucket", "fedora", "bonnet", "kappe", "mütze", "chapeau", "bonnet"],
    "sunglasses": ["sunglasses", "glasses", "güneş gözlüğü", "gozluk", "gözlük", "eyewear", "shades", "sonnenbrille", "lunettes"],
    "scarf": ["scarf", "atkı", "atki", "şal", "sal", "fular", "echarpe", "schal"],
    "jacket": ["jacket", "coat", "bomber", "blazer", "ceket", "mont", "kaban", "parka", "denim jacket", "leather", "deri", "mantel", "veste", "blouson"],
    "top": ["shirt", "t-shirt", "tshirt", "hoodie", "sweatshirt", "sweater", "polo", "blouse", "kazak", "tişört", "tisort", "gömlek", "gomlek", "triko", "pullover", "chemise", "haut"],
    "bottom": ["pants", "jeans", "trousers", "shorts", "pantolon", "jean", "denim", "jogger", "chino", "cargo", "hose", "pantalon"],
    "dress": ["dress", "elbise", "gown", "jumpsuit", "tulum", "kleid", "robe"],
    "shoes": ["shoe", "sneaker", "boot", "loafer", "sandal", "ayakkabı", "ayakkabi", "bot", "terlik", "schuh", "chaussure", "trainer"],
    "bag": ["bag", "purse", "backpack", "çanta", "canta", "sırt çantası", "tasche", "sac"],
    "watch": ["watch", "saat", "kol saati", "uhr", "montre", "timepiece", "horloge"],
    "accessory": ["necklace", "bracelet", "ring", "belt", "kemer", "kolye", "bileklik", "yüzük", "accessory", "jewelry"],
}


# ─── Match Lens results → Pieces by keyword + brand ───
def match_lens_to_pieces(lens_results, pieces):
    """Assign each Lens result to the best matching piece using keywords + brand."""
    piece_lens = {i: [] for i in range(len(pieces))}

    for lr in lens_results:
        title_lower = lr["title"].lower()
        link_lower = (lr.get("source", "") + " " + lr.get("link", "")).lower()

        best_piece = -1
        best_score = 0

        for i, p in enumerate(pieces):
            cat = p.get("category", "")
            keywords = PIECE_KEYWORDS.get(cat, [])
            score = 0

            # Keyword match
            for kw in keywords:
                if kw in title_lower:
                    score += 2
                    break

            if score == 0:
                continue

            # Brand bonus
            piece_brand = p.get("brand", "?").lower().strip()
            if piece_brand and piece_brand != "?" and len(piece_brand) > 2:
                combined = title_lower + " " + link_lower
                if piece_brand in combined:
                    score += 10  # Strong brand match

            if score > best_score:
                best_score = score
                best_piece = i

        if best_piece >= 0:
            piece_lens[best_piece].append(lr)

    return piece_lens


# ─── API ENDPOINTS ───

@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...), country: str = Form("tr")):
    """Auto mode: Claude detect + full image Lens + Shopping per piece + keyword matching."""
    if not SERPAPI_KEY:
        raise HTTPException(500, "No API key")
    cc = country.lower()
    contents = await file.read()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        optimized = buf.getvalue()
        b64 = base64.b64encode(optimized).decode()
    except Exception:
        optimized = contents
        b64 = base64.b64encode(contents).decode()

    print(f"\n{'='*50}\n=== AUTO ANALYZE === country={cc}")

    try:
        # Step 1: Claude detect + Upload full image → PARALLEL
        detect_task = claude_detect(b64, cc)
        upload_task = upload_img(optimized)
        pieces, img_url = await asyncio.gather(detect_task, upload_task)

        if not pieces:
            return {"success": True, "pieces": [], "country": cc}
        print(f"Claude: {len(pieces)} pieces detected")

        # Step 2: Full image Lens + all Shopping queries → PARALLEL
        async def do_lens():
            if img_url:
                async with API_SEM:
                    return await asyncio.to_thread(_lens, img_url, cc)
            return []

        async def do_shop(q):
            if q:
                async with API_SEM:
                    return await asyncio.to_thread(_shop, q, cc)
            return []

        shop_tasks = [do_shop(p.get("search_query", "")) for p in pieces[:5]]
        all_results = await asyncio.gather(do_lens(), *shop_tasks)

        lens_all = all_results[0]
        shop_per_piece = list(all_results[1:])
        print(f"Full Lens: {len(lens_all)} | Shop queries: {len(shop_per_piece)}")

        # Step 3: Match Lens → pieces by keyword + brand
        piece_lens = match_lens_to_pieces(lens_all, pieces[:5])

        # Step 4: Build results per piece
        results = []
        for i, p in enumerate(pieces[:5]):
            brand = p.get("brand", "")
            matched_lens = piece_lens.get(i, [])
            shop = shop_per_piece[i] if i < len(shop_per_piece) else []

            # Brand filter
            if brand and brand != "?":
                matched_lens = filter_rival_brands(matched_lens, brand)
                shop = filter_rival_brands(shop, brand)

            # Combine: Shopping first (text-accurate), Lens second (visual)
            seen, combined = set(), []
            for x in shop + matched_lens:
                if x["link"] not in seen:
                    seen.add(x["link"])
                    combined.append(x)

            results.append({
                "category": p.get("category", ""),
                "short_title": p.get("short_title", p.get("category", "").title()),
                "color": p.get("color", ""),
                "brand": brand if brand != "?" else "",
                "visible_text": p.get("visible_text", ""),
                "products": combined[:8],
                "lens_count": len(matched_lens),
            })

        return {"success": True, "pieces": results, "country": cc}
    except Exception as e:
        print(f"AUTO ANALYZE FAILED: {e}")
        return {"success": False, "message": str(e), "pieces": []}


# ─── Claude identify crop (Manual mode) ───
async def claude_identify_crop(img_bytes, cc="tr"):
    if not ANTHROPIC_API_KEY: return ""
    cfg = get_country_config(cc)
    try:
        img_c = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_c.thumbnail((400, 400))
        buf_c = io.BytesIO(); img_c.save(buf_c, format="JPEG", quality=80)
        b64_c = base64.b64encode(buf_c.getvalue()).decode()
    except Exception:
        b64_c = base64.b64encode(img_bytes).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": "claude-3-7-sonnet-20250219", "max_tokens": 100,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_c}},
                        {"type": "text", "text": f"This is a cropped clothing item. Write a 4-6 word {cfg['lang']} shopping search query for this EXACT item. Be ultra specific. Reply with ONLY the query."},
                    ]}]})
            return resp.json().get("content", [{}])[0].get("text", "").strip()
        except Exception: pass
    return ""


@app.post("/api/manual-search")
async def manual_search(file: UploadFile = File(...), query: str = Form(""), country: str = Form("tr")):
    if not SERPAPI_KEY: raise HTTPException(500, "No API key")
    cc = country.lower()
    contents = await file.read()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1024, 1024))
        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=85)
        optimized = buf.getvalue()
    except Exception:
        optimized = contents

    async with REMBG_LOCK:
        clean_bytes = await asyncio.to_thread(remove_bg, optimized)

    url, smart_query = await asyncio.gather(
        upload_img(clean_bytes),
        claude_identify_crop(clean_bytes, cc),
    )
    search_q = query if query else smart_query

    lens_res = []
    if url:
        async with API_SEM:
            lens_res = await asyncio.to_thread(_lens, url, cc)

    shop_res = []
    if len(lens_res) < 3 and search_q:
        async with API_SEM:
            shop_res = await asyncio.to_thread(_shop, search_q, cc, 6)

    seen, combined = set(), []
    for x in lens_res + shop_res:
        if x["link"] not in seen:
            seen.add(x["link"]); combined.append(x)

    if len(combined) >= 3:
        try:
            img_ai = Image.open(io.BytesIO(clean_bytes)).convert("RGB")
            img_ai.thumbnail((512, 512))
            buf_ai = io.BytesIO(); img_ai.save(buf_ai, format="JPEG", quality=80)
            orig_b64 = base64.b64encode(buf_ai.getvalue()).decode()
            combined = await claude_rerank(orig_b64, combined, cc, "clothing item")
        except Exception: pass

    return {"success": True, "products": combined[:10], "lens_count": len(lens_res),
        "query_used": search_q, "country": cc, "bg_removed": HAS_REMBG}


@app.get("/api/health")
async def health():
    return {"status": "ok", "serpapi": bool(SERPAPI_KEY), "anthropic": bool(ANTHROPIC_API_KEY)}

@app.get("/favicon.ico")
async def favicon():
    return Response(content=b"", media_type="image/x-icon")

@app.get("/api/countries")
async def countries():
    return {cc: {"name": cfg["name"], "currency": cfg["currency"]} for cc, cfg in COUNTRIES.items()}

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


# ─── FRONTEND ───
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
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;display:flex;justify-content:center}::-webkit-scrollbar{display:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
.app{width:100%;max-width:440px;min-height:100vh}
.btn-main{border:none;border-radius:14px;padding:15px;width:100%;font:700 15px 'DM Sans',sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-gold{background:var(--accent);color:var(--bg)}.btn-green{background:var(--green);color:var(--bg)}.btn-outline{background:transparent;color:var(--accent);border:2px solid var(--accent) !important}
.crop-container{position:relative;width:100%;max-height:400px;margin:12px 0;border-radius:14px;overflow:hidden;background:#111}.crop-container img{display:block;max-width:100%}
.hero{border-radius:14px;overflow:hidden;background:var(--card);border:1px solid var(--green);margin-bottom:10px;position:relative}.hero img{width:100%;height:200px;object-fit:cover;display:block}.hero .badge{position:absolute;top:10px;left:10px;background:var(--green);color:var(--bg);font-size:10px;font-weight:800;padding:4px 10px;border-radius:6px}.hero .info{padding:12px 14px}.hero .t{font-size:14px;font-weight:700}.hero .s{font-size:11px;color:var(--muted);margin-top:2px}.hero .row{display:flex;align-items:center;justify-content:space-between;margin-top:8px}.hero .price{font-size:20px;font-weight:800;color:var(--accent)}.hero .btn{background:var(--green);color:var(--bg);border:none;border-radius:8px;padding:8px 16px;font:700 12px 'DM Sans',sans-serif;cursor:pointer}
.piece{margin-bottom:28px;animation:fadeUp .4s ease both}.p-hdr{display:flex;align-items:center;gap:12px;margin-bottom:12px}.p-title{font-size:16px;font-weight:700}.p-brand{font-size:9px;font-weight:700;color:var(--bg);background:var(--accent);padding:2px 7px;border-radius:4px;margin-left:6px}
.scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}.card{flex-shrink:0;width:135px;background:var(--card);border-radius:10px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text)}.card.local{border-color:var(--accent)}.card img{width:135px;height:110px;object-fit:cover;display:block}.card .ci{padding:8px}.card .cn{font-size:10px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.card .cs{font-size:9px;color:var(--dim);margin-top:2px}.card .cp{font-size:13px;font-weight:700;color:var(--accent);margin-top:3px}
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
    <input type="file" id="fi" accept="image/jpeg,image/png,image/webp" style="display:none">
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
        <button class="btn-main btn-gold" onclick="startManual()" id="btnManual"></button>
        <button class="btn-main btn-outline" onclick="autoScan()" id="btnAuto"></button>
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
    <div onclick="showFavs()" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer"><div style="font-size:20px;color:var(--dim)">&#x2661;</div><div id="navFav" style="font-size:10px;font-weight:600;color:var(--dim)"></div></div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.js"></script>
<script>
var IC={hat:"\u{1F9E2}",sunglasses:"\u{1F576}",top:"\u{1F455}",jacket:"\u{1F9E5}",bag:"\u{1F45C}",accessory:"\u{1F48D}",watch:"\u{231A}",bottom:"\u{1F456}",dress:"\u{1F457}",shoes:"\u{1F45F}",scarf:"\u{1F9E3}"};
var cF=null,cPrev=null,cropper=null,CC='us';
var L={
  tr:{flag:"\u{1F1F9}\u{1F1F7}",heroTitle:'Gorseldeki outfiti<br><span style="color:var(--accent)">birebir</span> bul.',heroSub:'Fotograf yukle, AI parcalari tespit etsin<br>veya kendin sec, Google Lens bulsun.',upload:'Fotograf Yukle',uploadSub:'Galeri veya screenshot',auto:'\u{1F916} Otomatik Tara',manual:'\u{2702}\u{FE0F} Kendim Seceyim',feat1:'Otomatik Tara',feat1d:'AI tum parcalari tespit edip arar',feat2:'Kendim Seceyim',feat2d:'Parmaginla parcayi sec, birebir bul',feat3:'Yerel Magazalar',feat3d:'Trendyol, Zara TR, Bershka TR, vs.',back:'\u2190 Geri',cropHint:'\u{1F447} Aramak istedigin parcayi cercevele',manualPh:'Opsiyonel: ne aradigini yaz',find:'\u{1F50D} Bu Parcayi Bul',cancel:'\u2190 Vazgec',loading:'Parcalar tespit ediliyor...',loadingManual:'AI analiz ediyor...',noResult:'Sonuc bulunamadi',noProd:'Urun bulunamadi',retry:'\u{2702}\u{FE0F} Kendim Seceyim ile Tekrar Dene',another:'\u{2702}\u{FE0F} Baska Parca Sec',selected:'Sectigin Parca',lensMatch:'Lens eslesmesi',recommended:'\u{2728} Onerilen',lensLabel:'\u{1F3AF} Lens Eslesmesi',goStore:'Magazaya Git \u2197',noPrice:'Fiyat icin tikla',alts:'\u{1F4B8} Alternatifler \u{1F449}',navHome:'Kesfet',navFav:'Favoriler',aiMatch:'AI Onayli Eslesme',step_detect:'AI parcalari tespit ediyor...',step_lens:'Google Lens taramasi...',step_match:'Magazalar esleseniyor...',step_done:'Sonuclar hazirlaniyor...',step_bg:'Arka plan temizleniyor...',step_search:'Global magazalar taraniyor...',step_ai:'AI kumasi analiz ediyor...',step_verify:'Birebir eslesme dogrulaniyor...'},
  en:{flag:"\u{1F1FA}\u{1F1F8}",heroTitle:'Find the outfit<br>in the photo, <span style="color:var(--accent)">exactly</span>.',heroSub:'Upload a photo, AI detects each piece<br>or select manually, Google Lens finds it.',upload:'Upload Photo',uploadSub:'Gallery or screenshot',auto:'\u{1F916} Auto Scan',manual:'\u{2702}\u{FE0F} Select Myself',feat1:'Auto Scan',feat1d:'AI detects all pieces and searches',feat2:'Select Myself',feat2d:'Select the piece with your finger, find exact match',feat3:'Local Stores',feat3d:'',back:'\u2190 Back',cropHint:'\u{1F447} Frame the piece you want to find',manualPh:'Optional: describe what you\'re looking for',find:'\u{1F50D} Find This Piece',cancel:'\u2190 Cancel',loading:'Detecting pieces...',loadingManual:'AI analyzing...',noResult:'No results found',noProd:'No products found',retry:'\u{2702}\u{FE0F} Try Select Myself',another:'\u{2702}\u{FE0F} Select Another Piece',selected:'Your Selection',lensMatch:'Lens match',recommended:'\u{2728} Recommended',lensLabel:'\u{1F3AF} Lens Match',goStore:'Go to Store \u2197',noPrice:'Click for price',alts:'\u{1F4B8} Alternatives \u{1F449}',navHome:'Explore',navFav:'Favorites',aiMatch:'AI Verified Match',step_detect:'AI detecting pieces...',step_lens:'Google Lens scanning...',step_match:'Matching stores...',step_done:'Preparing results...',step_bg:'Removing background...',step_search:'Scanning global stores...',step_ai:'AI analyzing fabric...',step_verify:'Verifying exact match...'}
};
var L_fallback=L.en;
function t(key){var lg=CC_LANG[CC]||'en';return(L[lg]||L_fallback)[key]||(L.en)[key]||key}
var STORE_NAMES={tr:'Trendyol, Zara TR, Bershka TR, H&M TR',us:'Nordstrom, Macy\'s, ASOS, Urban Outfitters',gb:'ASOS, Selfridges, Harrods, John Lewis',de:'Zalando, About You, Otto',fr:'Galeries Lafayette, La Redoute',sa:'Namshi, Ounass, Sivvi',ae:'Namshi, 6th Street, Ounass',nl:'Zalando, De Bijenkorf, Wehkamp'};
var FLAGS={tr:"\u{1F1F9}\u{1F1F7}",us:"\u{1F1FA}\u{1F1F8}",gb:"\u{1F1EC}\u{1F1E7}",de:"\u{1F1E9}\u{1F1EA}",fr:"\u{1F1EB}\u{1F1F7}",sa:"\u{1F1F8}\u{1F1E6}",ae:"\u{1F1E6}\u{1F1EA}",nl:"\u{1F1F3}\u{1F1F1}"};
var CC_LANG={tr:'tr',us:'en',gb:'en',de:'de',fr:'fr',sa:'ar',ae:'en',nl:'nl'};
function detectCountry(){var tz=(Intl.DateTimeFormat().resolvedOptions().timeZone||'').toLowerCase();var lang=(navigator.language||'').toLowerCase();if(tz.indexOf('istanbul')>-1||lang.startsWith('tr'))return'tr';if(tz.indexOf('riyadh')>-1)return'sa';if(tz.indexOf('dubai')>-1)return'ae';if(tz.indexOf('amsterdam')>-1||lang.startsWith('nl'))return'nl';if(tz.indexOf('berlin')>-1||lang.startsWith('de'))return'de';if(tz.indexOf('paris')>-1||lang.startsWith('fr'))return'fr';if(tz.indexOf('london')>-1)return'gb';if(tz.indexOf('america')>-1||lang.startsWith('en-us'))return'us';return'us'}
CC=detectCountry();
function applyLang(){document.getElementById('heroTitle').innerHTML=t('heroTitle');document.getElementById('heroSub').innerHTML=t('heroSub');document.getElementById('uploadTitle').textContent=t('upload');document.getElementById('uploadSub').textContent=t('uploadSub');document.getElementById('feat1').textContent=t('feat1');document.getElementById('feat1d').textContent=t('feat1d');document.getElementById('feat2').textContent=t('feat2');document.getElementById('feat2d').textContent=t('feat2d');document.getElementById('feat3').textContent=t('feat3');document.getElementById('feat3d').textContent=STORE_NAMES[CC]||'';document.getElementById('flagIcon').textContent=FLAGS[CC]||'';document.getElementById('btnAuto').innerHTML=t('auto');document.getElementById('btnManual').innerHTML=t('manual');document.getElementById('backBtn').textContent=t('back');document.getElementById('cropHint').textContent=t('cropHint');document.getElementById('manualQ').placeholder=t('manualPh');document.getElementById('btnFind').innerHTML=t('find');document.getElementById('btnCancel').innerHTML=t('cancel');document.getElementById('navHome').textContent=t('navHome');document.getElementById('navFav').textContent=t('navFav');if(CC_LANG[CC]==='ar')document.documentElement.setAttribute('dir','rtl')}
applyLang();
function getCC(){return CC}
document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});
function loadF(f){if(!f.type.startsWith('image/'))return;cF=f;var r=new FileReader();r.onload=function(e){cPrev=e.target.result;showScreen()};r.readAsDataURL(f)}
function showScreen(){document.getElementById('home').style.display='none';document.getElementById('rScreen').style.display='block';document.getElementById('prev').src=cPrev;document.getElementById('prev').style.maxHeight='260px';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex';document.getElementById('cropMode').style.display='none';document.getElementById('ld').style.display='none';document.getElementById('err').style.display='none';document.getElementById('res').style.display='none';if(cropper){cropper.destroy();cropper=null}}
function goHome(){if(_busy)return;document.getElementById('home').style.display='block';document.getElementById('rScreen').style.display='none';if(cropper){cropper.destroy();cropper=null}cF=null;cPrev=null}
function autoScan(){if(_busy)return;document.getElementById('actionBtns').style.display='none';showLoading(t('loading'),[t('step_detect'),t('step_bg'),t('step_lens'),t('step_ai'),t('step_verify'),t('step_done')]);var fd=new FormData();fd.append('file',cF);fd.append('country',getCC());fetch('/api/full-analyze',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){hideLoading();if(!d.success)return showErr(d.message||'Error');renderAuto(d)}).catch(function(e){hideLoading();showErr(e.message)})}
function startManual(){document.getElementById('actionBtns').style.display='none';document.getElementById('prev').style.display='none';document.getElementById('cropMode').style.display='block';document.getElementById('cropImg').src=cPrev;document.getElementById('manualQ').value='';setTimeout(function(){if(cropper)cropper.destroy();cropper=new Cropper(document.getElementById('cropImg'),{viewMode:1,dragMode:'move',autoCropArea:0.5,responsive:true,background:false,guides:true,highlight:true,cropBoxMovable:true,cropBoxResizable:true})},100)}
function cancelManual(){if(cropper){cropper.destroy();cropper=null}document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex'}
function cropAndSearch(){if(!cropper)return;var canvas=cropper.getCroppedCanvas({maxWidth:800,maxHeight:800});if(!canvas)return;document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';showLoading(t('loadingManual'),[t('step_bg'),t('step_search'),t('step_ai'),t('step_verify')]);canvas.toBlob(function(blob){var q=document.getElementById('manualQ').value.trim();var fd=new FormData();fd.append('file',blob,'crop.jpg');fd.append('query',q);fd.append('country',getCC());fetch('/api/manual-search',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){hideLoading();if(!d.success)return showErr('Error');renderManual(d,canvas.toDataURL('image/jpeg',0.7))}).catch(function(e){hideLoading();showErr(e.message)})},'image/jpeg',0.85);if(cropper){cropper.destroy();cropper=null}}
var _ldTimer=null,_busy=false;
function showLoading(txt,steps){_busy=true;var l=document.getElementById('ld');l.style.display='block';var msgs=steps||[txt];var idx=0;function render(){l.innerHTML='<div style="display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:16px;border:1px solid var(--border);margin:14px 0"><div style="width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div><div style="font-size:13px;font-weight:600">'+msgs[idx]+'</div>'+(msgs.length>1?'<div style="font-size:10px;color:var(--dim);margin-top:3px">'+(idx+1)+'/'+msgs.length+'</div>':'')+'</div></div>'}render();if(msgs.length>1){if(_ldTimer)clearInterval(_ldTimer);_ldTimer=setInterval(function(){idx=(idx+1)%msgs.length;render()},3500)}}
function hideLoading(){_busy=false;if(_ldTimer){clearInterval(_ldTimer);_ldTimer=null}document.getElementById('ld').style.display='none'}
function showErr(m){var e=document.getElementById('err');e.style.display='block';e.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px;margin:12px 0;font-size:13px;color:var(--red)">'+m+'</div>'}
function renderAuto(d){document.getElementById('prev').style.maxHeight='160px';var pieces=d.pieces||[];var ra=document.getElementById('res');ra.style.display='block';var h='';for(var i=0;i<pieces.length;i++){var p=pieces[i],pr=p.products||[],lc=p.lens_count||0;var hero=pr[0],alts=pr.slice(1);h+='<div class="piece" style="animation-delay:'+(i*.1)+'s"><div class="p-hdr"><div style="width:52px;height:52px;border-radius:10px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:22px;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';h+='<div><span class="p-title">'+(p.short_title||p.category)+'</span>';if(p.brand&&p.brand!=='?'&&p.brand)h+='<span class="p-brand">'+p.brand+'</span>';var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';if(lc>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+lc+' '+t('lensMatch')+'</div>';h+='</div></div>';if(!hero){h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div></div>';continue}h+=heroHTML(hero,lc>0);if(alts.length>0)h+=altsHTML(alts);h+='</div>'}if(!pieces.length)h='<div style="text-align:center;padding:40px;color:var(--dim)">'+t('noResult')+'</div>';ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('retry')+'</button>'}
function renderManual(d,cropSrc){document.getElementById('prev').style.maxHeight='160px';var pr=d.products||[];var ra=document.getElementById('res');ra.style.display='block';var h='<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><img src="'+cropSrc+'" style="width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid var(--accent)"><div><span class="p-title">'+t('selected')+'</span>';if(d.query_used)h+='<div style="font-size:10px;color:var(--accent);margin-top:2px">\u{1F50D} "'+d.query_used+'"</div>';if(d.lens_count>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+d.lens_count+' '+t('lensMatch')+'</div>';h+='</div></div>';if(pr.length>0){h+=heroHTML(pr[0],d.lens_count>0);if(pr.length>1)h+=altsHTML(pr.slice(1))}else h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div>';ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('another')+'</button>'}
function _getFavs(){try{return JSON.parse(localStorage.getItem('fitfinder_favs')||'[]')}catch(e){return[]}}
function _setFavs(f){try{localStorage.setItem('fitfinder_favs',JSON.stringify(f))}catch(e){}}
function _hasFav(link){try{return(localStorage.getItem('fitfinder_favs')||'').indexOf(link)>-1}catch(e){return false}}
function toggleFav(e,link,img,title,price,brand){e.preventDefault();e.stopPropagation();var favs=_getFavs();var idx=favs.findIndex(function(f){return f.link===link});if(idx>-1){favs.splice(idx,1);e.target.innerHTML='\u{1F90D}'}else{favs.push({link:link,img:img,title:title,price:price,brand:brand});e.target.innerHTML='\u2764\uFE0F'}_setFavs(favs)}
function heroHTML(p,isLens){var img=p.image||p.thumbnail||'';var verified=p.ai_verified;var score=p.match_score||0;var badgeText=verified?'\u2705 '+t('aiMatch'):(isLens?t('lensLabel'):t('recommended'));var borderColor=verified?'#6fcf7c':(isLens?'var(--green)':'var(--green)');var isFav=_hasFav(p.link);var safeT=(p.title||'').replace(/'/g,"\\'");var safeP=(p.price||'').replace(/'/g,"\\'");var safeB=(p.brand||'').replace(/'/g,"\\'");var h='<div style="position:relative"><a href="'+p.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)"><div class="hero" style="border-color:'+borderColor+'">';if(img)h+='<img src="'+img+'" onerror="if(this.src!==\''+p.thumbnail+'\')this.src=\''+p.thumbnail+'\'">';h+='<div class="badge" style="'+(verified?'background:#22c55e':'')+'">'+badgeText+'</div>';if(score>=7)h+='<div style="position:absolute;top:10px;right:10px;background:rgba(0,0,0,.7);color:#fff;font-size:10px;font-weight:800;padding:3px 8px;border-radius:6px">'+score+'/10</div>';h+='<div class="info"><div class="t">'+p.title+'</div><div class="s">'+(p.brand||p.source||'')+'</div><div class="row"><span class="price">'+(p.price||t('noPrice'))+'</span><button class="btn">'+t('goStore')+'</button></div></div></div></a>';h+='<div onclick="toggleFav(event,\''+p.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:10px;right:'+(score>=7?'50px':'10px')+';background:rgba(0,0,0,.6);color:#fff;padding:6px;border-radius:50%;cursor:pointer;font-size:14px;z-index:10;line-height:1">'+(isFav?'\u2764\uFE0F':'\u{1F90D}')+'</div></div>';return h}
function altsHTML(list){var h='<div style="font-size:11px;color:var(--dim);margin:6px 0">'+t('alts')+'</div><div class="scroll">';for(var i=0;i<list.length;i++){var a=list[i];var img=a.thumbnail||a.image||'';var isFav=_hasFav(a.link);var safeT=(a.title||'').replace(/'/g,"\\'");var safeP=(a.price||'').replace(/'/g,"\\'");var safeB=(a.brand||a.source||'').replace(/'/g,"\\'");h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="card'+(a.is_local?' local':'')+'" style="'+(a.ai_verified?'border-color:#22c55e':'')+';position:relative">';if(img)h+='<img src="'+img+'" onerror="this.hidden=true">';h+='<div class="ci">';if(a.ai_verified)h+='<div style="font-size:8px;color:#22c55e;font-weight:700;margin-bottom:2px">\u2705 '+t('aiMatch')+'</div>';h+='<div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div>';h+='<div onclick="toggleFav(event,\''+a.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:5px;right:5px;background:rgba(0,0,0,.6);color:#fff;padding:4px;border-radius:50%;cursor:pointer;font-size:10px;z-index:10;line-height:1">'+(isFav?'\u2764\uFE0F':'\u{1F90D}')+'</div></a>'}return h+'</div>'}
function showFavs(){if(_busy)return;document.getElementById('home').style.display='none';document.getElementById('rScreen').style.display='block';var ab=document.getElementById('actionBtns');if(ab)ab.style.display='none';var cm=document.getElementById('cropMode');if(cm)cm.style.display='none';var pv=document.getElementById('prev');if(pv)pv.style.display='none';var ra=document.getElementById('res');var favs=_getFavs();ra.style.display='block';if(favs.length===0){var empty=CC_LANG[CC]==='tr'?'Henuz kaydedilmis urun yok \u{1F90D}':'No saved items yet \u{1F90D}';ra.innerHTML='<div style="text-align:center;padding:40px;color:var(--dim)">'+empty+'</div><button class="btn-main btn-outline" onclick="goHome()" style="margin-top:20px">'+t('back')+'</button>';return}var h='<h3 style="margin-bottom:15px;font-size:18px">'+t('navFav')+' \u2764\uFE0F</h3><div style="display:flex;flex-wrap:wrap;gap:10px">';for(var i=0;i<favs.length;i++){var f=favs[i];var safeT=(f.title||'').replace(/'/g,"\\'");var safeP=(f.price||'').replace(/'/g,"\\'");var safeB=(f.brand||'').replace(/'/g,"\\'");h+='<div style="width:calc(50% - 5px);border:1px solid var(--border);border-radius:10px;overflow:hidden;position:relative"><a href="'+f.link+'" target="_blank" style="text-decoration:none;color:var(--text)">';if(f.img)h+='<img src="'+f.img+'" style="width:100%;height:140px;object-fit:cover">';h+='<div style="padding:8px"><div style="font-size:10px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+f.title+'</div><div style="font-size:9px;color:var(--dim)">'+(f.brand||'')+'</div><div style="color:var(--accent);font-weight:700;font-size:12px;margin-top:4px">'+(f.price||'')+'</div></div></a>';h+='<div onclick="toggleFav(event,\''+f.link+'\',\''+(f.img||'')+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\');showFavs()" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);color:#fff;padding:5px;border-radius:50%;cursor:pointer;font-size:12px;line-height:1">\u2764\uFE0F</div></div>'}ra.innerHTML=h+'</div><button class="btn-main btn-outline" onclick="goHome()" style="margin-top:20px">'+t('back')+'</button>'}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
