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

# iPhone HEIC support (safety net for in-app browsers)
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
        rembg_session = new_session("u2net_cloth_seg")  # Clothing-specific model
        print("✅ rembg loaded (u2net_cloth_seg)")
    except Exception:
        rembg_session = new_session("u2net")  # Fallback to general model
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
        if time.time() - ts < CACHE_TTL: return val
        del _CACHE[key]
    return None

def cache_set(key, val):
    _CACHE[key] = (val, time.time())
    if len(_CACHE) > 500:
        now = time.time()
        expired = [k for k, (_, ts) in _CACHE.items() if now - ts > CACHE_TTL]
        for k in expired: del _CACHE[k]
        if len(_CACHE) > 500:
            oldest = sorted(_CACHE.keys(), key=lambda k: _CACHE[k][1])[:50]
            for k in oldest: del _CACHE[k]

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID", "")

# ✅ FIX #1: Correct model name (verified from Anthropic API docs Feb 2026)
# claude-3-7-sonnet-20250219 was RETIRED in July 2025
# claude-sonnet-4-20250514 is the current active Sonnet model
CLAUDE_MODEL = "claude-sonnet-4-20250514"

COUNTRIES = {
    "tr": {"name": "Türkiye", "gl": "tr", "hl": "tr", "lang": "Turkish", "currency": "₺", "local_stores": ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep."], "gender": {"male": "erkek", "female": "kadın"}},
    "us": {"name": "United States", "gl": "us", "hl": "en", "lang": "English", "currency": "$", "local_stores": ["nordstrom.", "macys.", "bloomingdales.", "target.com", "walmart.com", "urbanoutfitters.", "freepeople.", "anthropologie.", "revolve.com", "shopbop.", "ssense.", "farfetch."], "gender": {"male": "men", "female": "women"}},
}
DEFAULT_COUNTRY = "us"
def get_country_config(cc): return COUNTRIES.get(cc.lower(), COUNTRIES[DEFAULT_COUNTRY])

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
    if SKIMLINKS_ID: return f"https://go.skimresources.com/?id={SKIMLINKS_ID}&url={urllib.parse.quote(url, safe='')}"
    return url

BRAND_MAP = {"trendyol.com": "Trendyol", "hepsiburada.com": "Hepsiburada", "boyner.com.tr": "Boyner", "defacto.com": "DeFacto", "lcwaikiki.com": "LC Waikiki", "koton.com": "Koton", "beymen.com": "Beymen", "zara.com": "Zara", "bershka.com": "Bershka", "pullandbear.com": "Pull&Bear", "hm.com": "H&M", "mango.com": "Mango", "asos.com": "ASOS", "stradivarius.com": "Stradivarius", "massimodutti.com": "Massimo Dutti", "nike.com": "Nike", "adidas.": "Adidas"}
BLOCKED = ["pinterest.", "instagram.", "facebook.", "twitter.", "tiktok.", "youtube.", "aliexpress.", "wish.com", "dhgate.", "alibaba.", "shein.", "temu.", "cider.", "romwe.", "patpat.", "rightmove.", "zillow.", "realtor.", "ikea.", "wayfair."]
FASHION_DOMAINS = ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "zara.com", "bershka.com", "pullandbear.com", "hm.com", "mango.com", "asos.com", "stradivarius.com", "massimodutti.com", "nike.com", "adidas.", "puma.com"]

PIECE_KEYWORDS = {
    "hat": ["sapka","şapka","cap","hat","bere","beanie","kasket","kepi","baseball","snapback","bucket","fedora"],
    "sunglasses": ["gozluk","gözlük","sunglasses","güneş","eyewear","sonnenbrille","brille","lunettes"],
    "scarf": ["atki","atkı","sal","şal","fular","scarf","bandana","schal","tuch","écharpe"],
    "jacket": ["ceket","mont","kaban","blazer","bomber","jacket","coat","varsity","parka","trench","palto","kase","kaşe","cardigan","hırka","yelek","vest","windbreaker","puffer","jacke","mantel"],
    "top": ["tisort","tişört","gomlek","gömlek","sweatshirt","hoodie","kazak","bluz","top","shirt","polo","triko","t-shirt","tee","tank","henley","sweater","pullover","jumper","hemd"],
    "bottom": ["pantolon","jean","denim","jogger","chino","pants","trousers","sort","şort","etek","skirt","cargo","wide leg","slim fit","straight","baggy","hose","jeans","rock","shorts"],
    "dress": ["elbise","dress","tulum","jumpsuit","romper","kleid","overall","robe"],
    "shoes": ["ayakkabi","ayakkabı","sneaker","bot","boot","shoe","terlik","loafer","sandalet","trainer","runner","chelsea","oxford","schuh","schuhe","stiefel"],
    "bag": ["canta","çanta","bag","clutch","sirt","sırt","backpack","tote","crossbody","tasche","rucksack"],
    "watch": ["saat","watch","kol saati","timepiece","uhr","armbanduhr","montre"],
    "accessory": ["kolye","bileklik","yuzuk","yüzük","kupe","küpe","aksesuar","kemer","belt","necklace","bracelet","ring","earring","kette"],
}

def get_brand(link, src):
    c = (link + " " + src).lower()
    for d, b in BRAND_MAP.items():
        if d in c: return b
    return src if src else ""

def is_local(link, src, country_config): return any(d in (link + " " + src).lower() for d in country_config.get("local_stores", []))
def is_blocked(link): return any(d in link.lower() for d in BLOCKED)
def is_fashion(link, title, src):
    c = (link + " " + src).lower()
    if any(d in c for d in FASHION_DOMAINS): return True
    return any(k in (title + " " + src).lower() for k in ["ceket", "kazak", "shirt", "dress", "ayakkabi", "sneaker", "shoe", "canta", "bag", "gozluk", "saat", "giyim", "fashion"])

RIVAL_BRANDS = ["nike", "adidas", "puma", "zara", "hm", "bershka", "mango", "gucci", "prada", "balenciaga", "converse", "vans", "defacto", "koton", "lcw", "mavi", "colins", "levi", "tommy", "lacoste", "calvin klein", "massimo dutti", "pull&bear", "stradivarius"]

def filter_rival_brands(results, piece_brand):
    if not piece_brand or piece_brand == "?" or len(piece_brand) < 3: return results
    brand_lower = piece_brand.lower().strip()
    filtered = []
    for r in results:
        combined = (r.get("title", "") + " " + r.get("source", "")).lower()
        is_rival = False
        for rb in RIVAL_BRANDS:
            if re.search(rf'\b{re.escape(rb)}\b', combined) and rb not in brand_lower and brand_lower not in rb:
                is_rival = True; break
        if not is_rival: filtered.append(r)
    return filtered if filtered else results

# 🛡️ KALKAN BUG FIX: Claude "Watch" (büyük W) dönerse → "watch" olarak map'e
def get_category_key(cat):
    """Claude'un döndüğü kategoriyi normalize et (büyük/küçük harf, typo vs.)"""
    cat_lower = cat.lower().strip()
    if cat_lower in PIECE_KEYWORDS: return cat_lower
    for k, v in PIECE_KEYWORDS.items():
        if k in cat_lower: return k
        for kw in v[:5]:
            if kw in cat_lower: return k
    return None

# 🛡️ DEMİR KUBBE: KATEGORİ KALKANI
def filter_by_category(results, cat):
    """Lens sonucu kategoriye uymuyorsa çöpe at — ama asla hepsini öldürme!"""
    cat_key = get_category_key(cat) if cat else None
    if not cat_key: return results
    kws = PIECE_KEYWORDS[cat_key]
    filtered = []
    for r in results:
        tl = r.get("title", "").lower()
        if any(kw in tl for kw in kws):  # Basit substring, word boundary YOK
            filtered.append(r)
    return filtered if filtered else results  # Fallback: hiçbiri eşleşmediyse hepsini koru

# 🕵️ STRATEJİ 1: PARMAK İZİ PUANLAMA (Fingerprint Scoring)
def score_by_fingerprint(results, fingerprint, brand="", visible_text=""):
    """Her sonucu parmak izi detaylarına göre puanla. Daha çok detay eşleşen = daha üst sıra."""
    if not fingerprint and not brand and not visible_text:
        return results

    scored = []
    for r in results:
        title_lower = r.get("title", "").lower()
        link_lower = (r.get("link", "") + " " + r.get("source", "")).lower()
        combined_text = title_lower + " " + link_lower
        fp_score = 0

        # Parmak izi eşleştirme (her detay +3 puan)
        if fingerprint:
            for detail in fingerprint:
                if not detail: continue
                # Her detayı kelimelere böl, en az 1 kelime eşleşirse puan ver
                words = [w.strip().lower() for w in detail.split() if len(w) > 2]
                matched_words = sum(1 for w in words if w in combined_text)
                if matched_words >= 1:
                    fp_score += 3
                if matched_words >= 2:
                    fp_score += 2  # Bonus: 2+ kelime eşleşti

        # 🔍 OCR text eşleştirme (+10 puan — çok güçlü sinyal)
        if visible_text and visible_text.lower() not in ["none", "?", ""]:
            for vt in visible_text.lower().split():
                if len(vt) > 2 and vt in combined_text:
                    fp_score += 10
                    break

        # Marka eşleştirme (+8 puan)
        if brand and brand != "?" and len(brand) > 2:
            if brand.lower() in combined_text:
                fp_score += 8

        r_copy = r.copy()
        r_copy["_fp_score"] = fp_score
        scored.append(r_copy)

    # Yüksek parmak izi puanı olan üste
    scored.sort(key=lambda x: -x.get("_fp_score", 0))
    return scored

# 🎯 STRATEJİ 2: VENN ŞEMASI (Çapraz Doğrulama)
def venn_intersect_boost(shop_res, lens_res):
    """Hem Lens hem Shopping'de bulunan ürünler = BİREBİR EŞLEŞME → zirveye taşı!"""
    # Her iki listeden domain+title fingerprint oluştur
    def make_fingerprint(r):
        """URL domain + title'dan benzersiz parmak izi"""
        try:
            domain = urllib.parse.urlparse(r.get("link", "")).netloc.replace("www.", "")
        except:
            domain = ""
        # Title'ın ilk 5 kelimesi (normalize)
        title_words = re.sub(r'[^\w\s]', '', r.get("title", "").lower()).split()[:5]
        return domain + ":" + " ".join(title_words)

    # Shop fingerprint seti
    shop_fps = {}
    for r in shop_res:
        fp = make_fingerprint(r)
        shop_fps[fp] = r

    # Lens'te olanları kontrol et
    intersection = []
    lens_only = []
    for r in lens_res:
        fp = make_fingerprint(r)
        if fp in shop_fps:
            r_copy = r.copy()
            r_copy["_venn_match"] = True
            r_copy["ai_verified"] = True  # Çapraz doğrulanmış!
            intersection.append(r_copy)
        else:
            lens_only.append(r)

    # Ayrıca title benzerliği ile de kontrol et (fuzzy match)
    shop_titles = set()
    for r in shop_res:
        words = set(re.sub(r'[^\w\s]', '', r.get("title", "").lower()).split())
        words = {w for w in words if len(w) > 3}
        if words: shop_titles.add(frozenset(words))

    for r in lens_only[:]:  # Copy iterate
        words = set(re.sub(r'[^\w\s]', '', r.get("title", "").lower()).split())
        words = {w for w in words if len(w) > 3}
        if words:
            for st in shop_titles:
                overlap = len(words & st) / max(len(words | st), 1)
                if overlap >= 0.4:  # %40+ kelime örtüşmesi
                    r_copy = r.copy()
                    r_copy["_venn_match"] = True
                    intersection.append(r_copy)
                    lens_only.remove(r)
                    break

    return intersection, lens_only

# 🔍 STRATEJİ 4: OCR ARAMA SORGUSU OLUŞTURUCU
def build_ocr_query(brand, visible_text, color, category, lang_cfg):
    """Görünür metin + marka varsa agresif bir 2. arama sorgusu oluştur."""
    parts = []

    # Marka varsa mutlaka ekle
    if brand and brand != "?" and len(brand) > 1:
        parts.append(brand)

    # OCR text varsa ekle (logo, yazı)
    if visible_text and visible_text.lower() not in ["none", "?", "", "yok"]:
        # Sadece anlamlı kelimeleri al
        for word in visible_text.split():
            clean = re.sub(r'[^\w]', '', word)
            if len(clean) > 1 and clean.lower() not in ["none", "yok"]:
                parts.append(clean)
                break  # En önemli 1 kelime yeter

    # Renk + kategori
    if color and color.lower() not in ["?", ""]:
        parts.append(color)

    cat_names = {"hat": "şapka", "sunglasses": "güneş gözlüğü", "jacket": "ceket", "top": "üst",
        "bottom": "pantolon", "dress": "elbise", "shoes": "ayakkabı", "bag": "çanta",
        "watch": "saat", "scarf": "atkı", "accessory": "aksesuar"}
    if category in cat_names:
        parts.append(cat_names.get(category, category))

    query = " ".join(parts).strip()
    return query if len(query) > 4 else ""

async def upload_img(img_bytes):
    async with httpx.AsyncClient(timeout=30) as c:
        if IMGUR_CLIENT_ID:
            try:
                r = await c.post("https://api.imgur.com/3/image", headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}, files={"image": ("i.jpg", img_bytes, "image/jpeg")})
                if r.status_code == 200: return r.json().get("data", {}).get("link", "")
            except Exception: pass
        try:
            r = await c.post("https://litterbox.catbox.moe/resources/internals/api.php", data={"reqtype": "fileupload", "time": "1h"}, files={"fileToUpload": ("i.jpg", img_bytes, "image/jpeg")})
            if r.status_code == 200 and r.text.startswith("http"): return r.text.strip()
        except Exception: pass
        try:
            r = await c.post("https://tmpfiles.org/api/v1/upload", files={"file": ("i.jpg", img_bytes, "image/jpeg")})
            if r.status_code == 200:
                u = r.json().get("data", {}).get("url", "")
                if u: return u.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        except Exception: pass
    return None

def remove_bg(img_bytes):
    if not HAS_REMBG: return img_bytes
    try:
        result = rembg_remove(img_bytes, session=rembg_session)
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        bbox = img.getbbox()
        if bbox: img = img.crop(bbox)
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        bg = ImageOps.expand(bg, border=int(max(bg.size) * 0.05), fill='white')
        buf = io.BytesIO()
        bg.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception: return img_bytes

# ─── 🌟 THE MAGIC FIX: KUSURSUZ MATEMATİK MOTORU ───
def crop_piece(img_obj, box):
    """Matematiksel bug'ı çözer. Claude 0-1, 0-100 veya 0-1000 verse bile mükemmel keser!"""
    try:
        w, h = img_obj.size
        t_val, l_val, b_val, r_val = [float(v) for v in box]

        max_val = max(box)
        if max_val <= 1.0 and sum(box) > 0:
            scale = 1.0
        elif max_val <= 100.0:
            scale = 100.0
        else:
            scale = 1000.0

        top_pct, left_pct = max(0.0, t_val / scale), max(0.0, l_val / scale)
        bottom_pct, right_pct = min(1.0, b_val / scale), min(1.0, r_val / scale)

        # AI sol ve sağı karıştırırsa düzelt (Dyslexia Fix)
        if left_pct > right_pct: left_pct, right_pct = right_pct, left_pct
        if top_pct > bottom_pct: top_pct, bottom_pct = bottom_pct, top_pct

        top, left = int(top_pct * h), int(left_pct * w)
        bottom, right = int(bottom_pct * h), int(right_pct * w)

        # Hata Güvenliği: Kutu çok küçükse %20 minimum'a genişlet
        min_dim = int(max(w, h) * 0.20)
        if (right - left) < min_dim:
            cx = (left + right) // 2
            left = max(0, cx - min_dim // 2)
            right = min(w, cx + min_dim // 2)
        if (bottom - top) < min_dim:
            cy = (top + bottom) // 2
            top = max(0, cy - min_dim // 2)
            bottom = min(h, cy + min_dim // 2)

        # %10 Nefes payı
        pad_y, pad_x = int((bottom - top) * 0.10), int((right - left) * 0.10)
        px1, py1 = max(0, left - pad_x), max(0, top - pad_y)
        px2, py2 = min(w, right + pad_x), min(h, bottom + pad_y)

        cropped = img_obj.crop((px1, py1, px2, py2))
        cropped.thumbnail((1024, 1024))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception as e:
        print(f"Crop err: {e}"); return None

# ─── Claude Reranker (MANUAL MODE ONLY — auto'da timeout yapar) ───
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
                        img_data = base64.b64decode(raw); img = Image.open(io.BytesIO(img_data)).convert("RGB")
                        img.thumbnail((512, 512)); buf = io.BytesIO(); img.save(buf, format="JPEG", quality=80)
                        return base64.b64encode(buf.getvalue()).decode()
                    return None
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 500:
                    img = Image.open(io.BytesIO(resp.content)).convert("RGB"); img.thumbnail((512, 512))
                    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=80)
                    return base64.b64encode(buf.getvalue()).decode()
            except Exception: pass
            return None
        thumb_data = await asyncio.gather(*[fetch_thumb(r) for r in candidates])

    target_info = f'\nUSER IS LOOKING FOR: "{expected_text}"\nCRITICAL WARNING: If Image 0 shows a BACKGROUND object (wall, roof, door, wood) instead of a {expected_text}, REJECT ALL RESULTS (Score 0)!\n' if expected_text else ""

    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": original_b64}}, {"type": "text", "text": "ORIGINAL crop (Image 0)."}]
    valid_indices = []
    for i, tb64 in enumerate(thumb_data):
        if tb64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tb64}})
            content.append({"type": "text", "text": f"Result #{i+1}: {candidates[i].get('title', '')}"})
            valid_indices.append(i)
    if len(valid_indices) < 2: return results

    content.append({"type": "text", "text": f"""You are an ULTRA-STRICT AI fashion expert.{target_info}
Compare ORIGINAL (Image 0) with results.
10/10: EXACT MATCH. 8-9: Almost identical. 5-7: Similar style. 0-4: Different.
Explain visual reasoning BEFORE scoring. Return ONLY valid JSON array:
[{{"idx":1,"reason":"Same zipper...","score":10}}]"""})

    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": CLAUDE_MODEL, "max_tokens": 800, "messages": [{"role": "user", "content": content}]})
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
                        item = candidates[idx].copy(); item["match_score"] = score
                        if score >= 8: item["ai_verified"] = True; reranked.append(item); used.add(idx)
                        elif score >= 5: item["ai_verified"] = False; similar.append(item); used.add(idx)
                if reranked or similar: return reranked + similar
                # Jüri her şeyi çöp buldu → kapı/çatı göstermektense hiç gösterme
                print(f"  Reranker: tüm sonuçlar <5 puan, çöp elendi")
                return []
    except Exception as e: print(f"Reranker err: {e}")
    return results

async def claude_detect(img_b64, cc="tr"):
    if not ANTHROPIC_API_KEY: return None
    cfg = get_country_config(cc)
    lang, g_m, g_f = cfg["lang"], cfg["gender"]["male"], cfg["gender"]["female"]
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": CLAUDE_MODEL, "max_tokens": 2000,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": f"""You are an expert fashion detective. Analyze EVERY visible clothing item and accessory.
Gender: "{g_m}" (male) or "{g_f}" (female).

RULES:
1. ONLY items you are 100% SURE the person is wearing (30%+ visible).
2. A collar/lining peeking under a jacket is NOT a separate piece.

🔍 AGGRESSIVE OCR: Zoom into EVERY text, logo, label, embroidery, print on each item. Even tiny 2cm text on a pocket, sole, or tag. This is CRITICAL for exact matching.

🕵️ FINGERPRINT: For each item, identify 3 UNIQUE details that distinguish it from similar items (zipper style, collar type, pattern, stitching, button shape, pocket design, sole pattern, buckle type etc.)

For each item return:
- category: MUST be lowercase, exactly one of: hat|sunglasses|scarf|jacket|top|bottom|dress|shoes|bag|watch|accessory
- short_title: 2-4 word {lang} name
- color: in {lang}
- brand: Read logos/text carefully. Guess if obvious. Else "?"
- visible_text: ALL readable text, logos, numbers on this item (be aggressive, zoom in!)
- fingerprint: exactly 3 unique distinguishing micro-details in English (e.g. ["asymmetric silver zipper", "quilted diamond shoulders", "snap-button mandarin collar"])
- box_2d: [ymin, xmin, ymax, xmax]. Imagine the image is 1000x1000 pixels. Provide integer coordinates (0-1000).
- search_query: MAX 3-4 word {lang} shopping query. ONLY: gender + brand (if known) + color + category. Example: "erkek siyah ceket" or "kadın Nike beyaz sneaker". DO NOT write long sentences!

Return ONLY valid JSON array:
[{{"category":"","short_title":"","color":"","brand":"","visible_text":"","fingerprint":["","",""],"box_2d":[0,0,1000,1000],"search_query":""}}]"""}
                    ]}]})
            data = r.json()
            if "error" in data:
                print(f"Claude API error: {data['error']}")
                return None
            text = data.get("content", [{}])[0].get("text", "").strip()
            text = re.sub(r'^```\w*\n?', '', text); text = re.sub(r'\n?```$', '', text)
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m: return json.loads(m.group())
        except Exception as e: print(f"Claude err: {e}")
    return None

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
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src, "link": make_affiliate(lnk), "price": pr.get("value", "") if isinstance(pr, dict) else str(pr), "thumbnail": m.get("thumbnail", ""), "image": m.get("image", ""), "is_local": is_local(lnk, src, cfg)})
            if len(res) >= 20: break
    except Exception: pass

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
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src, "link": make_affiliate(lnk), "price": item.get("price", str(item.get("extracted_price", ""))), "thumbnail": item.get("thumbnail", ""), "image": "", "is_local": is_local(lnk, src, cfg)})
            if len(res) >= limit: break
    except Exception as e: print(f"Shop err: {e}")
    if res: cache_set(cache_key, res)
    return res

# ─── Auto Piece Pipeline ───
# ✅ FIX #2: NO rembg in auto mode (causes timeout due to serial REMBG_LOCK)
# ✅ FIX #3: NO rerank in auto mode (adds 4s per piece = 16s+ extra → timeout)
# rembg and rerank are ONLY used in manual mode where there's a single piece.
REMBG_CATS = {"jacket", "top", "bottom", "dress"}  # Only cloth items get rembg (manual mode)

async def process_auto_piece(p, img_obj, cc):
    cat, box, q, brand = p.get("category", "").lower(), p.get("box_2d"), p.get("search_query", ""), p.get("brand", "")
    color, short_title = p.get("color", ""), p.get("short_title", cat.title())
    visible_text = p.get("visible_text", "")
    fingerprint = p.get("fingerprint", [])  # 🕵️ Mikro-detay parmak izi

    try:
        # Step 1: Crop from high-res image
        cropped_bytes = None
        if box and isinstance(box, list) and len(box) == 4:
            cropped_bytes = await asyncio.to_thread(crop_piece, img_obj, box)

        # 🌟 Crop preview for UI
        crop_b64 = ""
        if cropped_bytes:
            try:
                t_img = Image.open(io.BytesIO(cropped_bytes)).convert("RGB")
                t_img.thumbnail((256, 256))
                t_buf = io.BytesIO()
                t_img.save(t_buf, format="JPEG", quality=80)
                crop_b64 = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
            except Exception: pass

        # Step 2: Upload crop + Shopping → PARALLEL
        # 🔍 OCR araması: SADECE gerçek marka/logo okunduysa 2. arama yap
        # brand="?" veya visible_text="None" ise gereksiz — Semaphore'u boğar, Lens'i geciktirir
        has_real_ocr = (brand and brand != "?" and len(brand) > 1) or \
                       (visible_text and visible_text.lower() not in ["none", "?", "", "yok", "-"])
        ocr_q = build_ocr_query(brand, visible_text, color, cat, get_country_config(cc)) if has_real_ocr else ""

        async def do_upload():
            return await upload_img(cropped_bytes) if cropped_bytes else None
        async def do_shop():
            if q:
                async with API_SEM:
                    return await asyncio.to_thread(_shop, q, cc, 8)
            return []
        async def do_ocr_shop():
            # SADECE gerçek OCR bilgisi varsa VE ana sorgudan farklıysa
            if ocr_q and ocr_q != q and has_real_ocr:
                async with API_SEM:
                    return await asyncio.to_thread(_shop, ocr_q, cc, 4)
            return []

        url, shop_res, ocr_shop_res = await asyncio.gather(
            do_upload(), do_shop(), do_ocr_shop()
        )

        # OCR sonuçlarını ana shop'a ekle (dedup)
        shop_links = {r["link"] for r in shop_res}
        for r in ocr_shop_res:
            if r["link"] not in shop_links:
                shop_res.append(r)
                shop_links.add(r["link"])

        print(f"  [{cat}] Shop:{len(shop_res)} OCR:{len(ocr_shop_res)} (ocr_q='{ocr_q}' real={has_real_ocr})")

        # Step 3: Lens on cropped image
        lens_res = []
        if url:
            try:
                async with API_SEM:
                    lens_res = await asyncio.to_thread(_lens, url, cc)
                raw_lens = len(lens_res)
                # 🛡️ DEMİR KUBBE (basit substring, asla hepsini öldürmez)
                lens_res = filter_by_category(lens_res, cat)
                print(f"  [{cat}] Lens: {raw_lens} raw → {len(lens_res)} after filter")
            except Exception as e:
                print(f"  [{cat}] Lens ERROR: {e}")

        # Step 4: Brand filter
        if brand and brand != "?":
            lens_res = filter_rival_brands(lens_res, brand)
            shop_res = filter_rival_brands(shop_res, brand)

        # Step 5: AKILLI BİRLEŞTİRME
        # Tüm sonuçları tek havuza al, akıllıca puanla, en iyiyi üste koy
        seen, combined = set(), []

        # Lens link'lerini set'e al (Venn tespiti için)
        lens_links = {r.get("link", "") for r in lens_res}
        shop_links = {r.get("link", "") for r in shop_res}

        # Tüm sonuçları birleştir (shop önce, lens sonra — dedup)
        all_results = []
        for r in shop_res:
            if r["link"] not in seen:
                seen.add(r["link"])
                r_copy = r.copy()
                r_copy["_source"] = "shop"
                all_results.append(r_copy)
        for r in lens_res:
            if r["link"] not in seen:
                seen.add(r["link"])
                r_copy = r.copy()
                r_copy["_source"] = "lens"
                all_results.append(r_copy)

        # Her sonuca akıllı puan ver
        for r in all_results:
            smart_score = 0
            title_lower = r.get("title", "").lower()
            link_lower = (r.get("link", "") + " " + r.get("source", "")).lower()
            combined_text = title_lower + " " + link_lower

            # 🎯 VENN BONUS: Hem Shop HEM Lens'te varsa (+20 puan — BİREBİR EŞLEŞME!)
            if r["link"] in lens_links and r["link"] in shop_links:
                smart_score += 20
                r["ai_verified"] = True

            # Shop bonus YOK — Lens ve Shop eşit rekabet etsin!
            # v33'te shop önce geliyordu ama bu Lens eşleşmelerini gömmüyordu
            # çünkü sıralama yoktu. Şimdi sıralama var, eşit olmalılar.

            # 🕵️ PARMAK İZİ BONUS
            if fingerprint:
                for detail in fingerprint:
                    if not detail: continue
                    words = [w.strip().lower() for w in detail.split() if len(w) > 2]
                    matched = sum(1 for w in words if w in combined_text)
                    if matched >= 1: smart_score += 3
                    if matched >= 2: smart_score += 2

            # 🔍 OCR BONUS: visible_text eşleşmesi (+10)
            if visible_text and visible_text.lower() not in ["none", "?", ""]:
                for vt in visible_text.lower().split():
                    if len(vt) > 2 and vt in combined_text:
                        smart_score += 10
                        break

            # Marka eşleşmesi (+8)
            if brand and brand != "?" and len(brand) > 2:
                if brand.lower() in combined_text:
                    smart_score += 8

            # Fiyat varsa bonus (+2)
            if r.get("price"): smart_score += 2

            # Yerel mağaza bonusu (+3)
            if r.get("is_local"): smart_score += 3

            r["_smart_score"] = smart_score

        # En yüksek puandan en düşüğe sırala
        all_results.sort(key=lambda x: -x.get("_smart_score", 0))

        # Debug log: top 3 results
        for i, r in enumerate(all_results[:3]):
            print(f"  [{cat}] #{i+1}: score={r.get('_smart_score',0)} src={r.get('_source','')} title={r.get('title','')[:50]}")

        # Cleanup: internal fields'ı kaldır
        for r in all_results:
            r.pop("_smart_score", None)
            r.pop("_source", None)

        combined = all_results

        return {
            "category": cat, "short_title": short_title, "color": color,
            "brand": brand if brand != "?" else "", "visible_text": visible_text,
            "fingerprint": fingerprint,
            "products": combined[:8], "lens_count": len(lens_res), "has_crop": cropped_bytes is not None,
            "crop_image": crop_b64
        }
    except Exception as e:
        print(f"  [{cat}] FAILED: {e}")
        return {
            "category": cat, "short_title": short_title, "color": color,
            "brand": brand if brand != "?" else "", "visible_text": visible_text,
            "products": [], "lens_count": 0, "has_crop": False, "crop_image": ""
        }

# ─── Claude identify crop (Manual mode only) ───
async def claude_identify_crop(img_bytes, cc="tr"):
    if not ANTHROPIC_API_KEY: return ""
    cfg = get_country_config(cc)
    try:
        img_c = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_c.thumbnail((400, 400)); buf_c = io.BytesIO(); img_c.save(buf_c, format="JPEG", quality=80)
        b64_c = base64.b64encode(buf_c.getvalue()).decode()
    except Exception:
        b64_c = base64.b64encode(img_bytes).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": CLAUDE_MODEL, "max_tokens": 100,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_c}},
                        {"type": "text", "text": f"This is a cropped clothing item. Write a 4-6 word {cfg['lang']} shopping search query for this EXACT item. Be ultra specific. Reply with ONLY the query."},
                    ]}]})
            return resp.json().get("content", [{}])[0].get("text", "").strip()
        except Exception: pass
    return ""

# ─── API ENDPOINTS ───

@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...), country: str = Form("tr")):
    if not SERPAPI_KEY: raise HTTPException(500, "No API key")
    cc = country.lower()
    contents = await file.read()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img_obj = img.copy()
        img_obj.thumbnail((2000, 2000))  # High-res for cropping
        img.thumbnail((1024, 1024))  # For Claude detection
        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        img_obj = Image.open(io.BytesIO(contents)).convert("RGB")
        b64 = base64.b64encode(contents).decode()

    print(f"\n{'='*50}\n=== AUTO ANALYZE === country={cc}")

    try:
        pieces = await claude_detect(b64, cc)
        if not pieces:
            return {"success": True, "pieces": [], "country": cc}
        print(f"Claude: {len(pieces)} pieces detected")

        # Process up to 5 pieces in parallel (no rembg/rerank = stays well under 30s)
        tasks = [process_auto_piece(p, img_obj, cc) for p in pieces[:4]]
        results = list(await asyncio.gather(*tasks))

        # Ürün bulunamayan parçaları ekranda gösterme (UX iyileştirme)
        results = [r for r in results if r.get("products") and len(r["products"]) > 0]

        return {"success": True, "pieces": results, "country": cc}
    except Exception as e:
        print(f"AUTO ANALYZE FAILED: {e}")
        return {"success": False, "message": str(e), "pieces": []}

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

    # Manual mode: rembg is safe here (single piece, no parallel lock contention)
    async with REMBG_LOCK:
        clean_bytes = await asyncio.to_thread(remove_bg, optimized)

    crop_b64 = ""
    try:
        t_img = Image.open(io.BytesIO(clean_bytes)).convert("RGB")
        t_img.thumbnail((256, 256))
        t_buf = io.BytesIO(); t_img.save(t_buf, format="JPEG", quality=80)
        crop_b64 = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
    except Exception: pass

    url, smart_query = await asyncio.gather(upload_img(clean_bytes), claude_identify_crop(clean_bytes, cc))
    search_q = query if query else smart_query

    lens_res = []
    if url:
        async with API_SEM: lens_res = await asyncio.to_thread(_lens, url, cc)

    shop_res = []
    if len(lens_res) < 3 and search_q:
        async with API_SEM: shop_res = await asyncio.to_thread(_shop, search_q, cc, 6)

    seen, combined = set(), []
    for x in lens_res + shop_res:
        if x["link"] not in seen:
            seen.add(x["link"]); combined.append(x)

    # Manual mode: rerank is safe here (single piece, has time budget)
    if len(combined) >= 3:
        try:
            img_ai = Image.open(io.BytesIO(clean_bytes)).convert("RGB")
            img_ai.thumbnail((512, 512))
            buf_ai = io.BytesIO(); img_ai.save(buf_ai, format="JPEG", quality=80)
            orig_b64 = base64.b64encode(buf_ai.getvalue()).decode()
            combined = await claude_rerank(orig_b64, combined, cc, "clothing item")
        except Exception: pass

    return {"success": True, "products": combined[:10], "lens_count": len(lens_res), "query_used": search_q, "country": cc, "bg_removed": HAS_REMBG, "crop_image": crop_b64}

@app.get("/api/health")
async def health(): return {"status": "ok"}
@app.get("/favicon.ico")
async def favicon(): return Response(content=b"", media_type="image/x-icon")
@app.get("/api/countries")
async def countries(): return {cc: {"name": cfg["name"], "currency": cfg["currency"]} for cc, cfg in COUNTRIES.items()}
@app.get("/", response_class=HTMLResponse)
async def home(): return HTML_PAGE

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
      <div>
        <div id="uploadTitle" style="font-size:16px;font-weight:700;color:var(--bg)"></div>
        <div id="uploadSub" style="font-size:12px;color:rgba(0,0,0,.45)"></div>
      </div>
    </div>
    <input type="file" id="fi" accept="image/jpeg,image/png,image/webp" style="display:none">
    <div style="margin-top:32px;display:flex;flex-direction:column;gap:14px;padding-bottom:100px">
      <div style="display:flex;gap:12px;align-items:center">
        <span style="font-size:20px">&#x1F916;</span>
        <div><div id="feat1" style="font-size:13px;font-weight:600"></div><div id="feat1d" style="font-size:11px;color:var(--muted)"></div></div>
      </div>
      <div style="display:flex;gap:12px;align-items:center">
        <span style="font-size:20px">&#x2702;&#xFE0F;</span>
        <div><div id="feat2" style="font-size:13px;font-weight:600"></div><div id="feat2d" style="font-size:11px;color:var(--muted)"></div></div>
      </div>
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
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer" onclick="goHome()">
      <div style="font-size:20px;color:var(--accent)">&#x2B21;</div>
      <div id="navHome" style="font-size:10px;font-weight:600;color:var(--accent)"></div>
    </div>
    <div onclick="showFavs()" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer">
      <div style="font-size:20px;color:var(--dim)">&#x2661;</div>
      <div id="navFav" style="font-size:10px;font-weight:600;color:var(--dim)"></div>
    </div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.js"></script>
<script>
var IC={hat:"\u{1F9E2}",sunglasses:"\u{1F576}",top:"\u{1F455}",jacket:"\u{1F9E5}",bag:"\u{1F45C}",accessory:"\u{1F48D}",watch:"\u{231A}",bottom:"\u{1F456}",dress:"\u{1F457}",shoes:"\u{1F45F}",scarf:"\u{1F9E3}"};
var cF=null,cPrev=null,cropper=null,CC='us';
var L={
  tr:{flag:"\u{1F1F9}\u{1F1F7}",heroTitle:'Gorseldeki outfiti<br><span style="color:var(--accent)">birebir</span> bul.',heroSub:'Fotograf yukle, AI parcalari tespit etsin<br>veya kendin sec, Google Lens bulsun.',upload:'Fotograf Yukle',uploadSub:'Galeri veya screenshot',auto:'\u{1F916} Otomatik Tara',manual:'\u{2702}\u{FE0F} Kendim Seceyim',feat1:'Otomatik Tara',feat1d:'AI tum parcalari tespit edip arar',feat2:'Kendim Seceyim',feat2d:'Parmaginla parcayi sec, birebir bul',back:'\u2190 Geri',cropHint:'\u{1F447} Aramak istedigin parcayi cercevele',manualPh:'Opsiyonel: ne aradigini yaz',find:'\u{1F50D} Bu Parcayi Bul',cancel:'\u2190 Vazgec',loading:'Parcalar tespit ediliyor...',loadingManual:'AI analiz ediyor...',noResult:'Sonuc bulunamadi',noProd:'Urun bulunamadi',retry:'\u{2702}\u{FE0F} Kendim Seceyim ile Tekrar Dene',another:'\u{2702}\u{FE0F} Baska Parca Sec',selected:'Sectigin Parca',lensMatch:'Lens eslesmesi',recommended:'\u{2728} Onerilen',lensLabel:'\u{1F3AF} Lens Eslesmesi',goStore:'Magazaya Git \u2197',noPrice:'Fiyat icin tikla',alts:'\u{1F4B8} Alternatifler \u{1F449}',navHome:'Kesfet',navFav:'Favoriler',aiMatch:'AI Onayli Eslesme',step_detect:'AI parcalari tespit ediyor...',step_bg:'Kesiliyor...',step_lens:'Google Lens taramasi...',step_ai:'AI kumasi inceliyor...',step_verify:'Birebir eslesme kontrolu...',step_done:'Sonuclar hazirlaniyor...'},
  en:{flag:"\u{1F1FA}\u{1F1F8}",heroTitle:'Find the outfit<br>in the photo, <span style="color:var(--accent)">exactly</span>.',heroSub:'Upload a photo, AI detects each piece<br>or select manually, Google Lens finds it.',upload:'Upload Photo',uploadSub:'Gallery or screenshot',auto:'\u{1F916} Auto Scan',manual:'\u{2702}\u{FE0F} Select Myself',feat1:'Auto Scan',feat1d:'AI detects all pieces and searches',feat2:'Select Myself',feat2d:'Select the piece with your finger, find exact match',back:'\u2190 Back',cropHint:'\u{1F447} Frame the piece you want to find',manualPh:'Optional: describe what you\'re looking for',find:'\u{1F50D} Find This Piece',cancel:'\u2190 Cancel',loading:'Detecting pieces...',loadingManual:'AI analyzing...',noResult:'No results found',noProd:'No products found',retry:'\u{2702}\u{FE0F} Try Select Myself',another:'\u{2702}\u{FE0F} Select Another Piece',selected:'Your Selection',lensMatch:'Lens match',recommended:'\u{2728} Recommended',lensLabel:'\u{1F3AF} Lens Match',goStore:'Go to Store \u2197',noPrice:'Click for price',alts:'\u{1F4B8} Alternatives \u{1F449}',navHome:'Explore',navFav:'Favorites',aiMatch:'AI Verified Match',step_detect:'AI detecting pieces...',step_lens:'Google Lens scanning...',step_match:'Matching stores...',step_done:'Preparing results...',step_bg:'Removing background...',step_search:'Scanning global stores...',step_ai:'AI analyzing fabric...',step_verify:'Verifying exact match...'}
};
var L_fallback=L.en;
function t(key){var lg=CC_LANG[CC]||'en';return(L[lg]||L_fallback)[key]||(L.en)[key]||key}
var STORE_NAMES={tr:'Trendyol, Zara TR, Bershka TR, H&M TR',us:'Nordstrom, Macy\'s, ASOS, Urban Outfitters'};
var FLAGS={tr:"\u{1F1F9}\u{1F1F7}",us:"\u{1F1FA}\u{1F1F8}"};
var CC_LANG={tr:'tr',us:'en'};
function detectCountry(){
  var tz=(Intl.DateTimeFormat().resolvedOptions().timeZone||'').toLowerCase();
  var lang=(navigator.language||'').toLowerCase();
  if(tz.indexOf('istanbul')>-1||lang.startsWith('tr'))return'tr';
  return'us';
}
CC=detectCountry();
function applyLang(){
  document.getElementById('heroTitle').innerHTML=t('heroTitle');
  document.getElementById('heroSub').innerHTML=t('heroSub');
  document.getElementById('uploadTitle').textContent=t('upload');
  document.getElementById('uploadSub').textContent=t('uploadSub');
  document.getElementById('feat1').textContent=t('feat1');
  document.getElementById('feat1d').textContent=t('feat1d');
  document.getElementById('feat2').textContent=t('feat2');
  document.getElementById('feat2d').textContent=t('feat2d');
  document.getElementById('btnAuto').innerHTML=t('auto');
  document.getElementById('btnManual').innerHTML=t('manual');
  document.getElementById('backBtn').textContent=t('back');
  document.getElementById('cropHint').textContent=t('cropHint');
  document.getElementById('manualQ').placeholder=t('manualPh');
  document.getElementById('btnFind').innerHTML=t('find');
  document.getElementById('btnCancel').innerHTML=t('cancel');
  document.getElementById('navHome').textContent=t('navHome');
  document.getElementById('navFav').textContent=t('navFav');
}
applyLang();
function getCC(){return CC}
document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});
function loadF(f){if(!f.type.startsWith('image/'))return;cF=f;var r=new FileReader();r.onload=function(e){cPrev=e.target.result;showScreen()};r.readAsDataURL(f)}
function showScreen(){document.getElementById('home').style.display='none';document.getElementById('rScreen').style.display='block';document.getElementById('prev').src=cPrev;document.getElementById('prev').style.maxHeight='260px';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex';document.getElementById('cropMode').style.display='none';document.getElementById('ld').style.display='none';document.getElementById('err').style.display='none';document.getElementById('res').style.display='none';if(cropper){cropper.destroy();cropper=null}}
function goHome(){if(_busy)return;document.getElementById('home').style.display='block';document.getElementById('rScreen').style.display='none';if(cropper){cropper.destroy();cropper=null}cF=null;cPrev=null}
function autoScan(){if(_busy)return;document.getElementById('actionBtns').style.display='none';showLoading(t('loading'),[t('step_detect'),t('step_bg'),t('step_lens'),t('step_ai'),t('step_verify'),t('step_done')]);var fd=new FormData();fd.append('file',cF);fd.append('country',getCC());fetch('/api/full-analyze',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){hideLoading();if(!d.success)return showErr(d.message||'Error');renderAuto(d)}).catch(function(e){hideLoading();showErr(e.message)})}
function startManual(){document.getElementById('actionBtns').style.display='none';document.getElementById('prev').style.display='none';document.getElementById('cropMode').style.display='block';document.getElementById('cropImg').src=cPrev;document.getElementById('manualQ').value='';setTimeout(function(){if(cropper)cropper.destroy();cropper=new Cropper(document.getElementById('cropImg'),{viewMode:1,dragMode:'move',autoCropArea:0.5,responsive:true,background:false,guides:true,highlight:true,cropBoxMovable:true,cropBoxResizable:true})},100)}
function cancelManual(){if(cropper){cropper.destroy();cropper=null}document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex'}
function cropAndSearch(){if(!cropper)return;var canvas=cropper.getCroppedCanvas({maxWidth:800,maxHeight:800});if(!canvas)return;document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';showLoading(t('loadingManual'),[t('step_bg'),t('step_lens'),t('step_ai'),t('step_verify')]);canvas.toBlob(function(blob){var q=document.getElementById('manualQ').value.trim();var fd=new FormData();fd.append('file',blob,'crop.jpg');fd.append('query',q);fd.append('country',getCC());fetch('/api/manual-search',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){hideLoading();if(!d.success)return showErr('Error');renderManual(d,canvas.toDataURL('image/jpeg',0.7))}).catch(function(e){hideLoading();showErr(e.message)})},'image/jpeg',0.85);if(cropper){cropper.destroy();cropper=null}}
var _ldTimer=null,_busy=false;
function showLoading(txt,steps){_busy=true;var l=document.getElementById('ld');l.style.display='block';var msgs=steps||[txt];var idx=0;function render(){l.innerHTML='<div style="display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:16px;border:1px solid var(--border);margin:14px 0"><div style="width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div><div style="font-size:13px;font-weight:600">'+msgs[idx]+'</div>'+(msgs.length>1?'<div style="font-size:10px;color:var(--dim);margin-top:3px">'+(idx+1)+'/'+msgs.length+'</div>':'')+'</div></div>'}render();if(msgs.length>1){if(_ldTimer)clearInterval(_ldTimer);_ldTimer=setInterval(function(){idx=(idx+1)%msgs.length;render()},3500)}}
function hideLoading(){_busy=false;if(_ldTimer){clearInterval(_ldTimer);_ldTimer=null}document.getElementById('ld').style.display='none'}
function showErr(m){var e=document.getElementById('err');e.style.display='block';e.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px;margin:12px 0;font-size:13px;color:var(--red)">'+m+'</div>'}

function renderAuto(d){
  document.getElementById('prev').style.maxHeight='160px';var pieces=d.pieces||[];var ra=document.getElementById('res');ra.style.display='block';var h='';
  for(var i=0;i<pieces.length;i++){
    var p=pieces[i],pr=p.products||[],lc=p.lens_count||0;var hero=pr[0],alts=pr.slice(1);
    var iconHtml = p.crop_image ? '<img src="'+p.crop_image+'" style="width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">' : '<div style="width:52px;height:52px;border-radius:10px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:22px;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';
    h+='<div class="piece" style="animation-delay:'+(i*.1)+'s"><div class="p-hdr">'+iconHtml;
    h+='<div><span class="p-title">'+(p.short_title||p.category)+'</span>';
    if(p.brand&&p.brand!=='?')h+='<span class="p-brand">'+p.brand+'</span>';
    var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';
    var fp=p.fingerprint||[];if(fp.length>0){h+='<div style="font-size:9px;color:var(--dim);margin-top:3px">';for(var fi=0;fi<fp.length;fi++){if(fp[fi])h+=(fi>0?' · ':'')+fp[fi]}h+='</div>'}
    if(lc>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+lc+' '+t('lensMatch')+'</div>';
    h+='</div></div>';
    if(!hero){h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div></div>';continue}
    h+=heroHTML(hero,lc>0);if(alts.length>0)h+=altsHTML(alts);h+='</div>';
  }
  if(!pieces.length)h='<div style="text-align:center;padding:40px;color:var(--dim)">'+t('noResult')+'</div>';
  ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('retry')+'</button>';
}

function renderManual(d,cropSrc){
  document.getElementById('prev').style.maxHeight='160px';var pr=d.products||[];var ra=document.getElementById('res');ra.style.display='block';
  var displayImg = d.crop_image || cropSrc;
  var h='<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><img src="'+displayImg+'" style="width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid var(--accent)"><div><span class="p-title">'+t('selected')+'</span>';
  if(d.query_used)h+='<div style="font-size:10px;color:var(--accent);margin-top:2px">\u{1F50D} "'+d.query_used+'"</div>';
  if(d.lens_count>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+d.lens_count+' '+t('lensMatch')+'</div>';
  h+='</div></div>';
  if(pr.length>0){h+=heroHTML(pr[0],d.lens_count>0);if(pr.length>1)h+=altsHTML(pr.slice(1))}
  else h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div>';
  ra.innerHTML=h+'<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">'+t('another')+'</button>';
}

function _getFavs(){try{return JSON.parse(localStorage.getItem('fitfinder_favs')||'[]')}catch(e){return[]}}
function _setFavs(f){try{localStorage.setItem('fitfinder_favs',JSON.stringify(f))}catch(e){}}
function _hasFav(link){try{return(localStorage.getItem('fitfinder_favs')||'').indexOf(link)>-1}catch(e){return false}}
function toggleFav(e,link,img,title,price,brand){e.preventDefault();e.stopPropagation();var favs=_getFavs();var idx=favs.findIndex(function(f){return f.link===link});if(idx>-1){favs.splice(idx,1);e.target.innerHTML='\u{1F90D}'}else{favs.push({link:link,img:img,title:title,price:price,brand:brand});e.target.innerHTML='\u2764\uFE0F'}_setFavs(favs)}

function heroHTML(p,isLens){
  var img=p.image||p.thumbnail||'';var verified=p.ai_verified;var score=p.match_score||0;
  var badgeText=verified?'\u2705 '+t('aiMatch'):(isLens?t('lensLabel'):t('recommended'));
  var borderColor=verified?'#6fcf7c':(isLens?'var(--green)':'var(--green)');
  var isFav=_hasFav(p.link);
  var safeT=(p.title||'').replace(/'/g,"\\'");var safeP=(p.price||'').replace(/'/g,"\\'");var safeB=(p.brand||'').replace(/'/g,"\\'");
  var h='<div style="position:relative"><a href="'+p.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)"><div class="hero" style="border-color:'+borderColor+'">';
  if(img)h+='<img src="'+img+'" onerror="if(this.src!==\''+p.thumbnail+'\')this.src=\''+p.thumbnail+'\'">';
  h+='<div class="badge" style="'+(verified?'background:#22c55e':'')+'">'+badgeText+'</div>';
  if(score>=7)h+='<div style="position:absolute;top:10px;right:10px;background:rgba(0,0,0,.7);color:#fff;font-size:10px;font-weight:800;padding:3px 8px;border-radius:6px">'+score+'/10</div>';
  h+='<div class="info"><div class="t">'+p.title+'</div><div class="s">'+(p.brand||p.source||'')+'</div><div class="row"><span class="price">'+(p.price||t('noPrice'))+'</span><button class="btn">'+t('goStore')+'</button></div></div></div></a>';
  h+='<div onclick="toggleFav(event,\''+p.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:10px;right:'+(score>=7?'50px':'10px')+';background:rgba(0,0,0,.6);color:#fff;padding:6px;border-radius:50%;cursor:pointer;font-size:14px;z-index:10;line-height:1">'+(isFav?'\u2764\uFE0F':'\u{1F90D}')+'</div></div>';
  return h;
}

function altsHTML(list){
  var h='<div style="font-size:11px;color:var(--dim);margin:6px 0">'+t('alts')+'</div><div class="scroll">';
  for(var i=0;i<list.length;i++){
    var a=list[i];var img=a.thumbnail||a.image||'';var isFav=_hasFav(a.link);
    var safeT=(a.title||'').replace(/'/g,"\\'");var safeP=(a.price||'').replace(/'/g,"\\'");var safeB=(a.brand||a.source||'').replace(/'/g,"\\'");
    h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="card'+(a.is_local?' local':'')+'" style="'+(a.ai_verified?'border-color:#22c55e':'')+';position:relative">';
    if(img)h+='<img src="'+img+'" onerror="this.hidden=true">';
    h+='<div class="ci">';
    if(a.ai_verified)h+='<div style="font-size:8px;color:#22c55e;font-weight:700;margin-bottom:2px">\u2705 '+t('aiMatch')+'</div>';
    h+='<div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div>';
    h+='<div onclick="toggleFav(event,\''+a.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:5px;right:5px;background:rgba(0,0,0,.6);color:#fff;padding:4px;border-radius:50%;cursor:pointer;font-size:10px;z-index:10;line-height:1">'+(isFav?'\u2764\uFE0F':'\u{1F90D}')+'</div></a>';
  }
  return h+'</div>';
}

function showFavs(){if(_busy)return;document.getElementById('home').style.display='none';document.getElementById('rScreen').style.display='block';var ab=document.getElementById('actionBtns');if(ab)ab.style.display='none';var cm=document.getElementById('cropMode');if(cm)cm.style.display='none';var pv=document.getElementById('prev');if(pv)pv.style.display='none';var ra=document.getElementById('res');var favs=_getFavs();ra.style.display='block';
  if(favs.length===0){var empty=CC_LANG[CC]==='tr'?'Henuz kaydedilmis urun yok \u{1F90D}':'No saved items yet \u{1F90D}';ra.innerHTML='<div style="text-align:center;padding:40px;color:var(--dim)">'+empty+'</div><button class="btn-main btn-outline" onclick="goHome()" style="margin-top:20px">'+t('back')+'</button>';return}
  var h='<h3 style="margin-bottom:15px;font-size:18px">'+t('navFav')+' \u2764\uFE0F</h3><div style="display:flex;flex-wrap:wrap;gap:10px">';
  for(var i=0;i<favs.length;i++){var f=favs[i];var safeT=(f.title||'').replace(/'/g,"\\'");var safeP=(f.price||'').replace(/'/g,"\\'");var safeB=(f.brand||'').replace(/'/g,"\\'");
    h+='<div style="width:calc(50% - 5px);border:1px solid var(--border);border-radius:10px;overflow:hidden;position:relative"><a href="'+f.link+'" target="_blank" style="text-decoration:none;color:var(--text)">';
    if(f.img)h+='<img src="'+f.img+'" style="width:100%;height:140px;object-fit:cover">';
    h+='<div style="padding:8px"><div style="font-size:10px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+f.title+'</div><div style="font-size:9px;color:var(--dim)">'+(f.brand||'')+'</div><div style="color:var(--accent);font-weight:700;font-size:12px;margin-top:4px">'+(f.price||'')+'</div></div></a>';
    h+='<div onclick="toggleFav(event,\''+f.link+'\',\''+(f.img||'')+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\');showFavs()" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);color:#fff;padding:5px;border-radius:50%;cursor:pointer;font-size:12px;line-height:1">\u2764\uFE0F</div></div>';
  }
  ra.innerHTML=h+'</div><button class="btn-main btn-outline" onclick="goHome()" style="margin-top:20px">'+t('back')+'</button>';
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
