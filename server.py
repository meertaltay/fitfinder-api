import os
import re
import io
import json
import base64
import asyncio
import time
import sys
import uuid
import httpx
import html
from hashlib import md5
import urllib.parse
from PIL import Image, ImageOps

# 🔥 Railway'de logların görünmesi için stdout buffer'ı kapat
os.environ["PYTHONUNBUFFERED"] = "1"
sys.stdout.reconfigure(line_buffering=True)

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

app = FastAPI(title="Fitchy API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_SEM = asyncio.Semaphore(6)  # v40: 3 Lens calls + Shopping + Google Organic
REMBG_SEM = asyncio.Semaphore(2)  # 8GB RAM → 2 paralel rembg güvenli

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

# ─── URL Localization: yabancı marka linklerini hedef ülkeye çevir ───
# Inditex (Bershka, Zara, Pull&Bear, Stradivarius, Massimo Dutti, Oysho):
#   bershka.com/eg/product → bershka.com/tr/product
# H&M: hm.com/en_us/product → hm.com/tr_tr/product
# Mango: shop.mango.com/us/product → shop.mango.com/tr/product

INDITEX_DOMAINS = ["bershka.com", "zara.com", "pullandbear.com", "stradivarius.com", "massimodutti.com", "oysho.com"]
# 2-letter country codes used in Inditex URL paths
INDITEX_CC = ["ae","al","am","at","au","az","ba","be","bg","bh","ca","ch","cl","cn","co","cr","cy","cz","de","dk","dz","ec","ee","eg","es","fi","fr","gb","ge","gr","gt","hk","hr","hu","id","ie","il","in","is","it","jo","jp","kr","kw","kz","lb","lt","lu","lv","ma","me","mk","mt","mx","my","nl","no","nz","om","pa","pe","ph","pk","pl","pt","qa","ro","rs","sa","se","sg","si","sk","sv","th","tn","tr","tw","ua","us","uy","vn","za"]

def localize_url(url, cc="tr"):
    """Yabancı marka linklerini hedef ülkeye çevir."""
    if not url: return url
    url_lower = url.lower()
    cc_lower = cc.lower()

    # Inditex brands: /XX/ → /tr/
    for domain in INDITEX_DOMAINS:
        if domain in url_lower:
            # Pattern: bershka.com/XX/... where XX is 2-letter country code
            pattern = re.compile(r'(https?://(?:www\.)?[^/]*' + re.escape(domain) + r'/)([a-z]{2})(/.*)', re.IGNORECASE)
            m = pattern.match(url)
            if m and m.group(2).lower() in INDITEX_CC:
                return m.group(1) + cc_lower + m.group(3)
            return url

    # H&M: /XX_XX/ → /tr_tr/
    if "hm.com" in url_lower:
        hm_map = {"tr": "tr_tr", "us": "en_us", "de": "de_de", "fr": "fr_fr", "gb": "en_gb"}
        target = hm_map.get(cc_lower, cc_lower + "_" + cc_lower)
        return re.sub(r'(/)[a-z]{2}_[a-z]{2}(/)', r'\g<1>' + target + r'\2', url)

    # Mango: shop.mango.com/XX/ → shop.mango.com/tr/
    if "mango.com" in url_lower:
        return re.sub(r'(mango\.com/)([a-z]{2})(/)', r'\g<1>' + cc_lower + r'\3', url)

    return url

BRAND_MAP = {"trendyol.com": "Trendyol", "hepsiburada.com": "Hepsiburada", "boyner.com.tr": "Boyner", "defacto.com": "DeFacto", "lcwaikiki.com": "LC Waikiki", "koton.com": "Koton", "beymen.com": "Beymen", "zara.com": "Zara", "bershka.com": "Bershka", "pullandbear.com": "Pull&Bear", "hm.com": "H&M", "mango.com": "Mango", "asos.com": "ASOS", "stradivarius.com": "Stradivarius", "massimodutti.com": "Massimo Dutti", "nike.com": "Nike", "adidas.": "Adidas"}
BLOCKED = ["pinterest.", "instagram.", "facebook.", "twitter.", "x.com/", "tiktok.", "youtube.", "aliexpress.", "wish.com", "dhgate.", "alibaba.", "shein.", "temu.", "cider.", "romwe.", "patpat.", "rightmove.", "zillow.", "realtor.", "ikea.", "wayfair.", "reddit.", "quora.", "medium.com", "wordpress.com", "blogspot.", "tumblr.", "buzzfeed.", "cosmopolitan.", "vogue.", "glamour.", "harpersbazaar.", "elle.", "gq.", "esquire.", "whowhatwear.", "refinery29.", "popsugar.", "insider.", "bustle.", "allure.", "fashionista.", "hypebeast.", "highsnobiety.", "complex.", "wikipedia.", "wikihow.", "wikiHow.", "lookastic.", "outfittrends.", "fashionbeans.", "themodestman.", "realmenrealstyle.", "stylesofman.", "brantano.", "lyst.com", "polyvore.", "chictopia.", "lookbook.nu", "wear.jp", "chicisimo.", "/blog/", "/blogs/", "/article/", "/magazin/", "/dergi/", "wattpad.", "booking.com", "tripadvisor.", "hotels.", "airbnb.", "shutterstock.", "gettyimages.", "alamy.", "istockphoto.", "123rf.", "dreamstime.", "stock.", "wallpaper.", "freepik.", "unsplash.", "pexels.", "pixabay.", "sahibinden.", "hepsiemlak.", "letgo.", "ebay.", "etsy.", "mercari.", "vinted.", "depop.", "grailed.", "stockx.", "goat.",
    # v42: Foreign magazine/blog sites that bypass keyword filters
    "blic.rs", "stil.kurir.rs", "zadovoljna.rs", "žena.rs", "gloria.hr", "femina.hr",
    "woman.ru", "lady.mail.ru", "cosmo.ru", "glamour.ru", "kp.ru",
    "digikala.", "divar.ir", "torob.ir", "basalam.ir", "emalls.ir",
    "bol.com", "cdiscount.", "otto.de", "aboutyou.de",
    "ozon.ru", "wildberries.", "lamoda.",
    "/stil/", "/moda/", "/trendy/", "/outfit/", "/look/", "/style-guide/",
    "/fashion-tips/", "/what-to-wear/", "lookbook", "streetstyle",
]
FASHION_DOMAINS = ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "zara.com", "bershka.com", "pullandbear.com", "hm.com", "mango.com", "asos.com", "stradivarius.com", "massimodutti.com", "nike.com", "adidas.", "puma.com", "dolap.com", "gardrops.com", "morhipo.", "lidyana.", "n11.com", "amazon.", "network.", "derimod.", "ipekyol.", "vakko.", "tommy.", "lacoste.", "uniqlo.", "gap.com", "nordstrom.", "farfetch.", "ssense.", "zalando.", "aboutyou."]
FASHION_KW = ["ceket", "kazak", "shirt", "dress", "ayakkabi", "sneaker", "shoe", "canta", "bag", "gozluk", "saat", "giyim", "fashion", "jacket", "hoodie", "sweatshirt", "jeans", "pantolon", "elbise", "bot", "mont", "kaban", "sapka", "hat", "watch", "clothing", "wear", "kolye", "kemer", "fiyat", "satin al", "urun", "modelleri"]

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
# Non-fashion domains that should NEVER appear (even in exact matches)
NON_FASHION_DOMAINS = ["butor", "furniture", "hotel", "booking", "travel", "realty", "estate", "kitchen", "dental", "clinic", "hospital", "lawyer", "plumber", "electric", "repair", "auto.", "car.", "motor", "food", "recipe", "cook", "restaurant", "cafe", "gym.", "fitness.", "sport.", "game.", "play.", "casino", "bet.", "crypto.", "bitcoin", "forex", "trade.", "invest", "bank.", "loan", "mortgage", "insurance", "news.", "press", "journal"]

def is_non_fashion_domain(link, title, source):
    """Quick check if URL/title contains obviously non-fashion keywords."""
    c = (link + " " + title + " " + source).lower()
    return any(nf in c for nf in NON_FASHION_DOMAINS)

def is_blocked(link): return any(d in link.lower() for d in BLOCKED)

# 🛡️ v42: YABANCI YAZI FİLTRESİ — Kiril, Arapça, Farsça başlıkları çöpe at
import unicodedata
def has_foreign_script(text, threshold=0.3):
    """TR modunda Latin-dışı karakter oranı threshold'u geçerse True."""
    if not text: return False
    non_latin = 0
    total = 0
    for ch in text:
        if ch.isalpha():
            total += 1
            cat = unicodedata.category(ch)
            # Latin harfler: Lu/Ll with name containing LATIN
            try:
                name = unicodedata.name(ch, "")
                if "LATIN" not in name and "TURKISH" not in name:
                    non_latin += 1
            except ValueError:
                non_latin += 1
    if total < 3: return False
    return (non_latin / total) > threshold

# 🛡️ v42: KATEGORİ TERS EŞLEŞME — "bag" aramasında "cup" gelirse çöpe at
def is_category_mismatch(title, category):
    """Sonucun başlığından hangi kategoriye ait olduğunu tespit et.
    Eğer aranan kategoriden FARKLI bir kategori tespit edilirse → engelle.
    
    Mantık: "bej çanta" arıyorsun → sonuçta "ceket" kelimesi var → FARKLI KATEGORİ → ⛔
    """
    if not category or not title: return False
    tl = title.lower()
    
    # Her kategoriden kaç keyword eşleşiyor?
    cat_scores = {}
    for cat, keywords in PIECE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if len(kw) >= 3 and kw in tl:
                score += 1
        if score > 0:
            cat_scores[cat] = score
    
    # Hiçbir kategori tespit edilmediyse → geçir (engellemeyiz)
    if not cat_scores:
        return False
    
    # En güçlü tespit edilen kategori
    detected_cat = max(cat_scores, key=cat_scores.get)
    detected_score = cat_scores[detected_cat]
    
    # Aranan kategori de tespit edilenler arasındaysa → geçir
    if category in cat_scores:
        return False
    
    # Aranan kategori tespit edilmedi AMA başka bir kategori tespit edildi → engelle
    # Minimum 1 keyword eşleşmesi yeterli (ceket, bomber, pants vs. açık kelimeler)
    if detected_score >= 1:
        return True
    
    return False
NON_CLOTHING_PRODUCTS = [
    "cup", "mug", "bardak", "tumbler", "thermos", "bottle", "şişe", "matara",
    "starbucks", "coffee", "kahve", "tea", "çay", "fincan",
    "phone", "telefon", "tablet", "laptop", "charger", "şarj", "kablo",
    "kitap", "book", "dergi", "magazine", "takvim", "calendar",
    "parfüm", "perfume", "koku", "deodorant", "kozmetik", "cosmetic",
    "oyuncak", "toy", "puzzle", "lego", "figür",
    "mutfak", "kitchen", "tabak", "plate", "çatal", "fork", "bıçak", "knife",
    "mum", "candle", "dekor", "decor", "vazo", "vase", "çerçeve", "frame",
    "halı", "carpet", "perde", "curtain", "yastık", "pillow", "battaniye", "blanket",
    # v42: Beauty / nail / hair — moda değil
    "nail", "tırnak", "manicure", "manikür", "pedicure", "pedikür", "gel polish",
    "oje", "nail art", "nail design", "cuticle", "acrylic nail",
    "saç", "hair", "peruk", "wig", "shampoo", "şampuan", "conditioner",
    "makeup", "makyaj", "foundation", "mascara", "lipstick", "ruj", "eyeliner",
    "skincare", "cilt bakım", "serum", "moisturizer", "cream", "krem ",
    # v42: Home / garden
    "mobilya", "furniture", "sandalye", "chair", "masa ", "table", "dolap",
    "bahçe", "garden", "pot ", "saksı",
    # v42: Lifestyle / blog content — site içeriği, ürün değil
    "lifestyle", "blog", "trend alert", "style tip", "outfit idea",
    "what to wear", "how to style", "fashion tip",
]

# v42: Dropshipping / scam / spam site patterns
SPAM_DOMAINS = [
    "niobe", "rica™", "rica-", "onlineshopzone", "shopee", "banggood",
    "lightinthebox", "sammydress", "rosegal", "zaful", "newchic",
    "tidebuy", "ericdress", "tbdress", "dressily", "stylewe",
    "floryday", "noracora", "roselinlin", "justfashionnow",
    "modlily", "rotita", "liligal", "bellelily", "soinyou",
    "chicme", "chiquedoll", "ivrose", "enyopro", "tinstree",
    # Genel spam patterns
    "discount-", "cheap-", "replica-", "wholesale-", "buy-online-",
]

# v42: Foreign clothing words (non-TR, non-EN) → dropshipping/yabancı site göstergesi
FOREIGN_CLOTHING_WORDS = [
    "broek", "jurk", "jas", "trui", "rok",       # Hollandaca
    "pantalon", "chemise", "veste", "robe",        # Fransızca (pantalon hariç TR'de de var)
    "hose", "kleid", "jacke", "hemd", "anzug",     # Almanca
    "vestito", "gonna", "camicia", "giacca",        # İtalyanca
    "falda", "camisa", "vestido", "abrigo",         # İspanyolca
    "брюки", "куртка", "платье", "рубашка",         # Rusça
]

def is_spam_domain(link, source):
    """Dropshipping / scam site mi?"""
    c = (link + " " + source).lower()
    return any(sd in c for sd in SPAM_DOMAINS)

def has_foreign_clothing_word(title):
    """Başlıkta yabancı dilde giyim kelimesi var mı? (broek, jurk, Kleid vs.)"""
    tl = title.lower()
    return any(fw in tl for fw in FOREIGN_CLOTHING_WORDS)

def is_non_clothing_product(title):
    """Ürün başlığı moda-dışı bir ürün mü? (bardak, telefon, mutfak vs.)"""
    tl = title.lower()
    return any(ncp in tl for ncp in NON_CLOTHING_PRODUCTS)

def is_fashion(link, title, src):
    c = (link + " " + src).lower()
    if any(d in c for d in FASHION_DOMAINS): return True
    return any(k in (title + " " + src).lower() for k in FASHION_KW)

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

    # OCR text varsa ekle (logo, yazı) — en önemli 3 kelimeye kadar
    if visible_text and visible_text.lower() not in ["none", "?", "", "yok"]:
        ocr_count = 0
        for word in visible_text.split():
            clean = re.sub(r'[^\w]', '', word)
            if len(clean) > 1 and clean.lower() not in ["none", "yok", "the", "and", "for"]:
                parts.append(clean)
                ocr_count += 1
                if ocr_count >= 3: break  # En önemli 3 kelime

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

REMOVEBG_KEY = os.environ.get("REMOVEBG_KEY", "")

def remove_bg(img_bytes):
    """Local rembg — manual mode fallback."""
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

async def remove_bg_api(img_bytes):
    """remove.bg API — hızlı, 0 CPU yükü, auto mode için."""
    if not REMOVEBG_KEY:
        print("  remove.bg: NO API KEY, using raw crop")
        return img_bytes
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.remove.bg/v1.0/removebg",
                headers={"X-Api-Key": REMOVEBG_KEY},
                files={"image_file": ("img.jpg", img_bytes, "image/jpeg")},
                data={"size": "auto", "bg_color": "FFFFFF", "format": "jpg", "type": "product"})
            if r.status_code == 200 and len(r.content) > 1000:
                print(f"  remove.bg OK ({len(r.content)//1024}KB)")
                return r.content
            else:
                print(f"  remove.bg FAIL: status={r.status_code}")
    except Exception as e:
        print(f"  remove.bg ERR: {e}")
    return img_bytes

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

        # %20 Nefes payı (Lens'e geniş bağlam ver)
        pad_y, pad_x = int((bottom - top) * 0.20), int((right - left) * 0.20)
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
                json={"model": CLAUDE_MODEL, "max_tokens": 1500,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": f"""You are a fashion product identification expert with EXCEPTIONAL text-reading ability. Your #1 job is reading EVERY piece of text on clothing to help find the exact product in stores.

Gender: "{g_m}" (male) or "{g_f}" (female).

RULES:
1. ONLY detect MAIN clothing pieces: jacket/top/bottom/dress/shoes. These are the PRIORITY.
2. ALSO detect bag or watch ONLY if clearly visible and prominent (taking up significant space in the photo).
3. Do NOT detect: hats, caps, sunglasses, scarves, belts, jewelry, socks, small accessories — they are too small to search reliably.
4. A collar/lining peeking under a jacket is NOT a separate piece.
5. Maximum 4 pieces total. Focus on quality over quantity.

⚠️ TEXT READING IS YOUR #1 PRIORITY:
- Zoom into EVERY patch, logo, label, tag, embroidery, print, badge on each item
- Read character by character — do NOT guess or approximate
- Varsity jackets have 3-6 patches with text. READ EVERY SINGLE ONE.
- Common patch text: brand names, city names, numbers, slogans, team names
- Even if text is curved, upside down, partially hidden — try to read it
- Report ALL text you see, separated by commas

🏷️ BRAND DETECTION — CHECK THESE COMMON BRANDS:
Fast fashion: Bershka, Zara, Pull&Bear, Stradivarius, H&M, Mango, DeFacto, LC Waikiki, Koton
Sport: Nike, Adidas, Puma, New Balance, Converse, Vans, Reebok
Premium: Tommy Hilfiger, Lacoste, Ralph Lauren, Calvin Klein, Levi's, Guess
If you see ANY text matching these brands (even partially), mark it as the brand.
Also check: zipper pulls, collar tags, sole stamps, button engravings.

For each item return:
- category: exactly one of: jacket|top|bottom|dress|shoes|bag|watch
- short_title: 2-4 word {lang} name
- color: in {lang}
- brand: The brand name if detected. Check ALL text/patches/logos/tags. "?" ONLY if truly unreadable.
- visible_text: EVERY readable text on this item, separated by commas. Be exhaustive. Example: "Timeless, Rebel Spirit, 66, Athletics"
- style_type: specific style in English (e.g. "varsity bomber", "slim fit chino", "chelsea boot")
- search_query_specific: 5-8 word {lang} shopping query. MUST include: gender + brand (if found) + ALL key visible text + color + style. Example: "erkek Bershka Timeless yeşil varsity bomber ceket". The visible text words are CRITICAL for finding the exact product!
- search_query_generic: 3-5 word {lang} fallback: gender + color + style. Example: "erkek yeşil bomber ceket"
- box_2d: [ymin, xmin, ymax, xmax] on a 1000x1000 grid.
  ⚠️ CRITICAL BOX RULES:
  - jacket/top: box should END at the waist/belt line. Do NOT include legs/pants.
  - bottom/pants: box should START at the waist/belt line. Do NOT include the torso/jacket above.
  - shoes: box should only cover feet area, starting below the ankle.
  - dress: can cover full body from shoulders to hem.
  - Boxes must NOT significantly overlap each other. Each piece gets its OWN region.
  - Be TIGHT — include only the garment, minimize background.

Return ONLY valid JSON array:
[{{"category":"","short_title":"","color":"","brand":"","visible_text":"","style_type":"","search_query_specific":"","search_query_generic":"","box_2d":[0,0,1000,1000]}}]"""}
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

def _lens(url, cc="tr", lens_type="all"):
    """Google Lens API. lens_type: 'all', 'exact_matches', 'visual_matches', 'products'"""
    cfg = get_country_config(cc)
    res, seen = [], set()
    try:
        params = {"engine": "google_lens", "url": url, "api_key": SERPAPI_KEY,
                  "hl": cfg["hl"], "country": cfg["gl"]}
        if lens_type != "all":
            params["type"] = lens_type

        d = GoogleSearch(params).get_dict()

        # 1) EXACT MATCHES — "Tam eşleşmeler" = aynı fotoğraf web'de bulundu
        for m in d.get("exact_matches", []):
            lnk = m.get("link", "")
            ttl = m.get("title", m.get("source", ""))
            src = m.get("source", "")
            if not lnk or lnk in seen: continue
            if is_blocked(lnk): continue
            if is_non_fashion_domain(lnk, ttl, src): continue
            # v42: Foreign script filter — TR modunda Kiril/Arapça başlıkları çöpe at
            if cc == "tr" and has_foreign_script(ttl):
                print(f"    ⛔ FOREIGN SCRIPT: {ttl[:60]}")
                continue
            # v42: Non-clothing product filter
            if is_non_clothing_product(ttl):
                print(f"    ⛔ NON-CLOTHING: {ttl[:60]}")
                continue
            # v42: Dropshipping / spam domain filter
            if is_spam_domain(lnk, src):
                print(f"    ⛔ SPAM DOMAIN: {src} | {ttl[:50]}")
                continue
            # v42: Foreign clothing vocabulary (broek, jurk, Kleid etc.)
            if cc == "tr" and has_foreign_clothing_word(ttl):
                print(f"    ⛔ FOREIGN CLOTHING WORD: {ttl[:60]}")
                continue
            seen.add(lnk)
            if not ttl: ttl = src or lnk
            original_lnk = lnk
            lnk = localize_url(lnk, cc)  # bershka.com/eg → bershka.com/tr
            url_changed = (lnk != original_lnk)
            pr = m.get("price", {})
            # Yabancı ülkeden gelen fiyatı temizle (E£, $, € → yanlış para birimi)
            price_val = "" if url_changed else (pr.get("value", "") if isinstance(pr, dict) else str(pr) if pr else "")
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk), "price": price_val,
                "thumbnail": m.get("thumbnail", ""), "image": m.get("image", ""),
                "is_local": is_local(lnk, src, cfg), "ai_verified": True, "_exact": True})
        exact_count = len(res)
        if exact_count > 0:
            print(f"  Lens EXACT matches ({lens_type}): {exact_count}")
            for r in res[:5]:
                print(f"    ✅ {r['title'][:60]} | {r['source']}")

        # 2) VISUAL MATCHES — benzer görünen ürünler
        for m in d.get("visual_matches", []):
            lnk, ttl, src = m.get("link", ""), m.get("title", ""), m.get("source", "")
            if not lnk or not ttl or lnk in seen: continue
            if is_blocked(lnk) or not is_fashion(lnk, ttl, src): continue
            # v42: Foreign script filter
            if cc == "tr" and has_foreign_script(ttl):
                continue
            # v42: Non-clothing product filter
            if is_non_clothing_product(ttl):
                continue
            # v42: Spam domain + foreign clothing word
            if is_spam_domain(lnk, src): continue
            if cc == "tr" and has_foreign_clothing_word(ttl): continue
            seen.add(lnk)
            original_lnk = lnk
            lnk = localize_url(lnk, cc)  # yabancı linkleri yerelleştir
            url_changed = (lnk != original_lnk)
            pr = m.get("price", {})
            price_val = "" if url_changed else (pr.get("value", "") if isinstance(pr, dict) else str(pr) if pr else "")
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk), "price": price_val,
                "thumbnail": m.get("thumbnail", ""), "image": m.get("image", ""),
                "is_local": is_local(lnk, src, cfg)})
            if len(res) >= 25: break

    except Exception as e:
        print(f"Lens err ({lens_type}): {e}")

    def score(r):
        s = 0
        if r.get("_exact"): s += 100
        if r.get("price"): s += 10
        if r.get("is_local"): s += 5
        c = (r.get("link", "") + " " + r.get("source", "")).lower()
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
            # Prefer direct store link over Google Shopping comparison page
            direct = item.get("link", "")
            google_page = item.get("product_link", "")
            # Use direct link if it's a real store URL (not google.com)
            if direct and "google.com" not in direct:
                lnk = direct
            else:
                lnk = google_page or direct
            ttl, src = item.get("title", ""), item.get("source", "")
            if not lnk or not ttl or lnk in seen or is_blocked(lnk): continue
            # v42: Non-clothing product filter (Starbucks bardak vs.)
            if is_non_clothing_product(ttl): continue
            if cc == "tr" and has_foreign_script(ttl): continue
            # v42: Spam domain + foreign clothing word
            if is_spam_domain(lnk, src): continue
            if cc == "tr" and has_foreign_clothing_word(ttl): continue
            seen.add(lnk)
            lnk = localize_url(lnk, cc)
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src, "link": make_affiliate(lnk), "price": item.get("price", str(item.get("extracted_price", ""))), "thumbnail": item.get("thumbnail", ""), "image": "", "is_local": is_local(lnk, src, cfg)})
            if len(res) >= limit: break
    except Exception as e: print(f"Shop err: {e}")
    if res: cache_set(cache_key, res)
    return res


# ─── Google Regular Search (organic results from fashion sites) ───
def _google_organic(q, cc="tr", limit=8):
    """Normal Google araması — Shopping'de olmayan ürünleri yakalar (Trendyol, Bershka.com, Dolap vs.)"""
    cache_key = f"gorg:{cc}:{q}"
    cached = cache_get(cache_key)
    if cached: return cached
    cfg = get_country_config(cc)
    res, seen = [], set()
    try:
        d = GoogleSearch({"engine": "google", "q": q, "gl": cfg["gl"], "hl": cfg["hl"], "api_key": SERPAPI_KEY, "num": 15}).get_dict()

        # 1) Inline shopping results (varsa)
        for item in d.get("inline_shopping_results", d.get("shopping_results", [])):
            lnk = item.get("link", "")
            ttl = item.get("title", "")
            src = item.get("source", "")
            if not lnk or not ttl or lnk in seen or is_blocked(lnk): continue
            if not is_fashion(lnk, ttl, src): continue
            seen.add(lnk)
            lnk = localize_url(lnk, cc)
            pr = item.get("price", item.get("extracted_price", ""))
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk), "price": str(pr) if pr else "",
                "thumbnail": item.get("thumbnail", ""), "image": "",
                "is_local": is_local(lnk, src, cfg), "_src": "google_inline"})
            if len(res) >= limit: break

        # 2) Organic results (fashion domain'lerden)
        for item in d.get("organic_results", []):
            lnk = item.get("link", "")
            ttl = item.get("title", "")
            src = item.get("displayed_link", item.get("source", ""))
            if not lnk or not ttl or lnk in seen or is_blocked(lnk): continue
            if not is_fashion(lnk, ttl, src): continue
            seen.add(lnk)
            lnk = localize_url(lnk, cc)
            # Organic'te fiyat snippet'den çekilebilir
            snippet = item.get("snippet", "")
            price = ""
            price_match = re.search(r'(\d[\d.,]+)\s*(?:TL|₺|\$|€|£|AED|SAR)', snippet)
            if price_match:
                price = price_match.group(0)
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src,
                "link": make_affiliate(lnk), "price": price,
                "thumbnail": item.get("thumbnail", ""), "image": "",
                "is_local": is_local(lnk, src, cfg), "_src": "google_organic"})
            if len(res) >= limit: break

    except Exception as e: print(f"Google organic err: {e}")
    if res: cache_set(cache_key, res)
    return res


# ─── HYBRID AUTO PIPELINE (v39) ───
# Temel fikir: Crop ÇALIŞMIYOR. HİBRİT arama kazanır:
# 1. Claude → brand, visible_text, style, 2 arama sorgusu
# 2. Multi-query Shopping per piece (spesifik → generic)
# 3. Google Organic per piece (normal arama — Shopping'de olmayan ürünleri yakalar)
# 4. Full image Lens → keyword match to pieces (bonus görsel)
# 5. Cross-channel scoring + match confidence

# ─── HYBRID AUTO PIPELINE (v39) uses PIECE_KEYWORDS from above ───


def build_ocr_shopping_query(brand, visible_text, color, category, style_type, lang_cfg):
    """OCR text + brand'den 3. bir Shopping sorgusu oluştur."""
    parts = []
    if brand and brand != "?" and len(brand) > 1:
        parts.append(brand)
    if visible_text and visible_text.lower() not in ["none", "?", "", "yok"]:
        for word in visible_text.replace(",", " ").split():
            clean = re.sub(r'[^\w]', '', word)
            if len(clean) > 1 and clean.lower() not in ["none", "yok", "the", "and", "for"]:
                parts.append(clean)
                if len(parts) >= 4: break
    if color and color.lower() not in ["?", ""]:
        parts.append(color)
    cat_tr = {"hat": "şapka", "sunglasses": "güneş gözlüğü", "jacket": "ceket", "top": "üst",
        "bottom": "pantolon", "dress": "elbise", "shoes": "ayakkabı", "bag": "çanta",
        "watch": "saat", "scarf": "atkı", "accessory": "aksesuar"}
    if category in cat_tr:
        parts.append(cat_tr[category])
    query = " ".join(parts).strip()
    return query if len(query) > 4 else ""


def match_lens_to_pieces(lens_results, pieces):
    """Full image Lens sonuçlarını keyword + brand ile parçalara eşle."""
    piece_lens = {i: [] for i in range(len(pieces))}
    for lr in lens_results:
        title_lower = lr["title"].lower()
        link_lower = (lr.get("source", "") + " " + lr.get("link", "")).lower()
        best_i, best_score = -1, 0
        for i, p in enumerate(pieces):
            cat = p.get("category", "")
            kws = PIECE_KEYWORDS.get(cat, [])
            score = 0
            for kw in kws:
                if kw in title_lower:
                    score += 2; break
            if score == 0: continue
            # Brand bonus
            pb = p.get("brand", "?").lower().strip()
            if pb and pb != "?" and len(pb) > 2 and pb in (title_lower + " " + link_lower):
                score += 10
            # Visible text bonus
            vt = p.get("visible_text", "").lower()
            if vt and vt not in ["none", "?", ""]:
                for word in vt.replace(",", " ").split():
                    if len(word) > 2 and word in title_lower:
                        score += 5; break
            if score > best_score:
                best_score = score; best_i = i
        if best_i >= 0:
            piece_lens[best_i].append(lr)
    return piece_lens


# ─── API ENDPOINTS ───

@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...), country: str = Form("tr")):
    """v40: HYBRID — Per-piece Lens (crop) + Shopping + Google Organic, all parallel."""
    if not SERPAPI_KEY: raise HTTPException(500, "No API key")
    cc = country.lower()
    contents = await file.read()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img_obj = img.copy()  # Keep original for cropping
        img_obj.thumbnail((1400, 1400))
        img.thumbnail((1400, 1400))  # High-res for Claude OCR
        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95)
        optimized = buf.getvalue()
        b64 = base64.b64encode(optimized).decode()
        print(f"  Image: {img.size[0]}x{img.size[1]}, {len(optimized)//1024}KB sent to Claude")
    except Exception:
        img_obj = Image.open(io.BytesIO(contents)).convert("RGB")
        optimized = contents
        b64 = base64.b64encode(contents).decode()

    print(f"\n{'='*50}\n=== AUTO v40 HYBRID+CROP === country={cc}")

    try:
        # ── Step 1: Claude detect + Upload full image → PARALLEL ──
        detect_task = claude_detect(b64, cc)
        upload_task = upload_img(optimized)
        pieces, img_url = await asyncio.gather(detect_task, upload_task)

        if not pieces:
            return {"success": True, "pieces": [], "country": cc}
        ALLOWED_CATS = {"jacket", "top", "bottom", "dress", "shoes", "bag", "watch"}
        pieces = [p for p in pieces if p.get("category", "") in ALLOWED_CATS][:4]
        print(f"Claude: {len(pieces)} pieces (filtered)")
        for p in pieces:
            print(f"  → {p.get('category')} | brand={p.get('brand')} | text='{p.get('visible_text','')}' | style={p.get('style_type','')}")
            print(f"    q_spec: {p.get('search_query_specific','')}")
            print(f"    q_gen:  {p.get('search_query_generic','')}")

        # ── Step 2: Crop each piece + Build search queries ──
        # v42 OPTIMIZED: 1 merged query per piece (specific + OCR keywords)
        # OLD: specific + ocr + generic = 3 SerpAPI calls per piece
        # NEW: 1 smart merged query = 1 SerpAPI call per piece
        search_queries = []  # [(piece_idx, query_str, priority)]
        crop_tasks = []  # [(piece_idx, crop_bytes)]

        for i, p in enumerate(pieces):
            q_specific = p.get("search_query_specific", "").strip()
            q_generic = p.get("search_query_generic", "").strip()

            # Merge OCR keywords into specific query (instead of separate call)
            brand = p.get("brand", "")
            visible_text = p.get("visible_text", "")
            if q_specific:
                # Check if OCR text is already in specific query
                q_lower = q_specific.lower()
                extra_ocr = []
                if visible_text and visible_text.lower() not in ["none", "?", "", "yok"]:
                    for word in visible_text.replace(",", " ").split():
                        clean = re.sub(r'[^\w]', '', word)
                        if len(clean) > 2 and clean.lower() not in q_lower and clean.lower() not in ["none", "yok", "the", "and", "for"]:
                            extra_ocr.append(clean)
                            if len(extra_ocr) >= 2: break  # Max 2 extra OCR words
                if extra_ocr:
                    q_merged = q_specific + " " + " ".join(extra_ocr)
                    search_queries.append((i, q_merged, "specific"))
                    print(f"  [{p.get('category')}] Merged OCR into specific: +{extra_ocr}")
                else:
                    search_queries.append((i, q_specific, "specific"))
            elif q_generic:
                search_queries.append((i, q_generic, "generic"))

            # Crop for Lens (simple, no rembg)
            box = p.get("box_2d")
            if box and isinstance(box, list) and len(box) == 4:
                try:
                    cropped_bytes = await asyncio.to_thread(crop_piece, img_obj, box)
                    if cropped_bytes:
                        crop_tasks.append((i, cropped_bytes))
                        try:
                            t_img = Image.open(io.BytesIO(cropped_bytes)).convert("RGB")
                            t_img.thumbnail((128, 128))
                            t_buf = io.BytesIO(); t_img.save(t_buf, format="JPEG", quality=75)
                            pieces[i]["_crop_b64"] = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
                        except: pass
                        print(f"  [{p.get('category')}] Cropped OK ({len(cropped_bytes)//1024}KB) box={box}")
                    else:
                        print(f"  [{p.get('category')}] Crop FAILED")
                except Exception as e:
                    print(f"  [{p.get('category')}] Crop error: {e}")

        serpapi_calls = len(search_queries) + len(crop_tasks) + 1  # +1 for full exact
        print(f"Search queries: {len(search_queries)} | Crops: {len(crop_tasks)} | SerpAPI calls: {serpapi_calls}")
        for idx, q, pri in search_queries:
            print(f"  [{pieces[idx].get('category')}] {pri}: '{q}'")

        # ── Step 3: Upload crops + Per-piece Lens + Shopping → ALL PARALLEL ──
        # v42 OPTIMIZED: Removed full_lens_visual (piece_lens covers it)
        # v42 OPTIMIZED: Removed Google organic (moved to "load more" button)
        # v42 OPTIMIZED: 1 shopping query per piece (merged specific+OCR)
        # Result: 2-piece outfit = 5 SerpAPI calls (was 12)

        async def do_shop(q, limit=8):
            async with API_SEM:
                return await asyncio.to_thread(_shop, q, cc, limit)

        async def do_piece_lens(crop_bytes):
            """Upload crop → Lens ALL (exact+visual matches for this piece)."""
            url = await upload_img(crop_bytes)
            if url:
                async with API_SEM:
                    return await asyncio.to_thread(_lens, url, cc, "all")
            return []

        async def do_full_lens_exact():
            """Full image → Lens EXACT matches (same photo found on Bershka, Instagram etc.)."""
            if img_url:
                async with API_SEM:
                    return await asyncio.to_thread(_lens, img_url, cc, "exact_matches")
            return []

        # Build ALL tasks
        tasks = []
        task_map = []  # (type, piece_idx, extra_info)

        # 🏆 FULL IMAGE EXACT MATCHES — This is what Google Lens app does!
        tasks.append(do_full_lens_exact())
        task_map.append(("full_lens_exact", -1, None))

        # Per-piece Lens (crop → similar+exact products)
        for piece_idx, crop_bytes in crop_tasks:
            tasks.append(do_piece_lens(crop_bytes))
            task_map.append(("piece_lens", piece_idx, None))

        # Shopping (1 merged query per piece)
        for piece_idx, q, pri in search_queries:
            tasks.append(do_shop(q, 8))
            task_map.append(("shop", piece_idx, pri))

        # 🚀 FIRE ALL AT ONCE
        all_results = await asyncio.gather(*tasks)

        # ── Step 4: Distribute results to pieces ──
        piece_lens = {i: [] for i in range(len(pieces))}
        piece_shop = {i: [] for i in range(len(pieces))}
        seen_per_piece = {i: set() for i in range(len(pieces))}

        for task_idx, (task_type, piece_idx, extra) in enumerate(task_map):
            results = all_results[task_idx] if task_idx < len(all_results) else []

            if task_type == "full_lens_exact":
                # 🏆 EXACT matches from full image — distribute to pieces by keyword
                if results:
                    exact_matches = match_lens_to_pieces(results, pieces)
                    for i in range(len(pieces)):
                        for r in exact_matches.get(i, []):
                            link = r.get("link", "")
                            if link not in seen_per_piece[i]:
                                seen_per_piece[i].add(link)
                                piece_lens[i].append(r)
                    # Unmatched exact results go to first available piece (too good to lose)
                    matched_links = set()
                    for i in range(len(pieces)):
                        for r in exact_matches.get(i, []):
                            matched_links.add(r.get("link", ""))
                    for r in results:
                        link = r.get("link", "")
                        if link not in matched_links:
                            for i in range(len(pieces)):
                                if link not in seen_per_piece[i]:
                                    seen_per_piece[i].add(link)
                                    piece_lens[i].append(r)
                                    break
                    exact_total = sum(len(v) for v in exact_matches.values())
                    print(f"  🏆 Full EXACT Lens: {len(results)} results → {exact_total} matched to pieces")
                    for r in results[:5]:
                        print(f"    ✅ {r.get('title','')[:60]} | {r.get('source','')} | exact={r.get('_exact',False)}")

            elif task_type == "piece_lens":
                # Per-piece Lens results go directly to that piece
                for r in results:
                    link = r.get("link", "")
                    if link not in seen_per_piece[piece_idx]:
                        seen_per_piece[piece_idx].add(link)
                        piece_lens[piece_idx].append(r)
                print(f"  [{pieces[piece_idx].get('category')}] Piece Lens: {len(results)} results")

            elif task_type == "shop":
                for r in results:
                    link = r.get("link", "")
                    if link not in seen_per_piece[piece_idx]:
                        seen_per_piece[piece_idx].add(link)
                        r_copy = r.copy()
                        r_copy["_priority"] = extra
                        r_copy["_channel"] = "shopping"
                        piece_shop[piece_idx].append(r_copy)

        # Log Lens results per piece (for debugging)
        for i in range(len(pieces)):
            cat = pieces[i].get("category", "")
            lens_r = piece_lens.get(i, [])
            if lens_r:
                print(f"  [{cat}] Lens top 5:")
                for j, r in enumerate(lens_r[:5]):
                    print(f"    {j+1}. {r.get('title','')[:60]} | {r.get('source','')}")

        # ── Step 6: Build final results per piece (HYBRID SCORING) ──
        results = []
        for i, p in enumerate(pieces):
            brand = p.get("brand", "")
            visible_text = p.get("visible_text", "")
            cat = p.get("category", "")

            shop = piece_shop.get(i, [])
            matched_lens = piece_lens.get(i, [])

            # Brand filter
            if brand and brand != "?":
                matched_lens = filter_rival_brands(matched_lens, brand)
                shop = filter_rival_brands(shop, brand)

            # Collect all links per channel for cross-reference
            shop_links = {r.get("link", "") for r in shop}
            lens_links = {r.get("link", "") for r in matched_lens}

            def score_result(r, base_score=0):
                """Universal scoring function for any channel."""
                score = base_score
                combined = (r.get("title", "") + " " + r.get("link", "") + " " + r.get("source", "")).lower()

                # 🏆 EXACT LENS MATCH — same photo found on web
                if r.get("_exact"):
                    score += 50
                    r["ai_verified"] = True

                # Cross-channel bonus (same product in both lens + shopping = reliable)
                link = r.get("link", "")
                if link in shop_links and link in lens_links:
                    score += 25; r["ai_verified"] = True
                elif (link in shop_links) != (link in lens_links):  # in one but not both
                    score += 5

                # Brand match
                if brand and brand != "?" and len(brand) > 2 and brand.lower() in combined:
                    score += 8
                # OCR text match (visible_text on the garment)
                if visible_text and visible_text.lower() not in ["none", "?", ""]:
                    for vt in visible_text.lower().replace(",", " ").split():
                        if len(vt) > 2 and vt in combined: score += 10; break
                # Price & local bonus
                if r.get("price"): score += 2
                if r.get("is_local"): score += 3
                return score

            seen = set()
            all_items = []

            # Lens results (per-piece crop + full exact = most reliable)
            for r in matched_lens:
                if r["link"] in seen: continue
                # v42: Category mismatch filter (çanta ararken bardak gelmesin)
                if is_category_mismatch(r.get("title", ""), cat):
                    print(f"    ⛔ CAT MISMATCH [{cat}]: {r.get('title','')[:50]}")
                    continue
                if is_non_clothing_product(r.get("title", "")):
                    print(f"    ⛔ NON-CLOTHING [{cat}]: {r.get('title','')[:50]}")
                    continue
                seen.add(r["link"])
                r["_score"] = score_result(r, 18)
                all_items.append(r)

            # Shopping results
            for r in shop:
                if r["link"] in seen: continue
                if is_category_mismatch(r.get("title", ""), cat): continue
                if is_non_clothing_product(r.get("title", "")): continue
                seen.add(r["link"])
                base = 15 if r.get("_priority") == "specific" else 5
                r["_score"] = score_result(r, base)
                all_items.append(r)

            # Sort by score
            all_items.sort(key=lambda x: -x.get("_score", 0))

            # 🇹🇷 TR-FIRST: Local results first, foreign only fills remaining slots
            local_items = [r for r in all_items if r.get("is_local")]
            foreign_items = [r for r in all_items if not r.get("is_local")]
            # Local first (by score), then foreign (by score) — but exact matches always on top
            foreign_exact = [r for r in foreign_items if r.get("_exact")]
            foreign_rest = [r for r in foreign_items if not r.get("_exact")]
            all_items = foreign_exact + local_items + foreign_rest

            # ── Match confidence ──
            top_score = all_items[0].get("_score", 0) if all_items else 0
            has_brand_match = any(
                brand and brand != "?" and brand.lower() in (r.get("title","") + " " + r.get("link","")).lower()
                for r in all_items[:3]
            ) if brand and brand != "?" else False
            has_text_match = False
            if visible_text and visible_text.lower() not in ["none", "?", ""]:
                vt_words = [w for w in visible_text.lower().replace(",", " ").split() if len(w) > 2]
                for r in all_items[:3]:
                    t = r.get("title", "").lower()
                    if any(w in t for w in vt_words): has_text_match = True; break

            if top_score >= 50:
                match_level = "exact"  # Lens exact match (same photo found online)
            elif top_score >= 25 and (has_brand_match or has_text_match):
                match_level = "exact"
            elif top_score >= 15 or has_brand_match:
                match_level = "close"
            else:
                match_level = "similar"

            # Log
            for j, r in enumerate(all_items[:3]):
                ch = r.get("_channel", r.get("_src", "lens"))
                print(f"  [{cat}] #{j+1}: score={r.get('_score',0)} ch={ch} {r.get('title','')[:50]}")
            print(f"  [{cat}] match={match_level} brand={has_brand_match} text={has_text_match}")

            # Clean internal fields
            for r in all_items:
                for k in ["_score", "_priority", "_channel", "_src", "_exact"]: r.pop(k, None)

            results.append({
                "category": cat,
                "short_title": p.get("short_title", cat.title()),
                "color": p.get("color", ""),
                "brand": brand if brand != "?" else "",
                "visible_text": visible_text,
                "products": all_items[:8],
                "lens_count": len(matched_lens),
                "match_level": match_level,
                "crop_image": p.get("_crop_b64", ""),
            })

        return {"success": True, "pieces": results, "country": cc}
    except Exception as e:
        print(f"AUTO ANALYZE FAILED: {e}")
        import traceback; traceback.print_exc()
        return {"success": False, "message": str(e), "pieces": []}


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

    # Manual mode: remove.bg API first, local rembg fallback
    clean_bytes = await remove_bg_api(optimized)
    if clean_bytes is optimized and HAS_REMBG:
        # API failed, try local rembg (tek parça, safe)
        async with REMBG_SEM:
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

# ─── TRENDING DATA (dynamic + curated) ───
TRENDING_CACHE = {}  # lang → {brands, products, ts}
TRENDING_TTL = 86400  # 24 saat cache

BRAND_DATA = {
    "tr": [
        {"name": "Zara", "domain": "zara.com", "url": "https://www.zara.com/tr/"},
        {"name": "Bershka", "domain": "bershka.com", "url": "https://www.bershka.com/tr/"},
        {"name": "Mango", "domain": "mango.com", "url": "https://shop.mango.com/tr"},
        {"name": "Nike", "domain": "nike.com", "url": "https://www.nike.com/tr/"},
        {"name": "Adidas", "domain": "adidas.com", "url": "https://www.adidas.com.tr/"},
        {"name": "H&M", "domain": "hm.com", "url": "https://www2.hm.com/tr_tr/"},
        {"name": "Koton", "domain": "koton.com", "url": "https://www.koton.com/"},
        {"name": "Pull&Bear", "domain": "pullandbear.com", "url": "https://www.pullandbear.com/tr/"},
    ],
    "en": [
        {"name": "Zara", "domain": "zara.com", "url": "https://www.zara.com/"},
        {"name": "Nike", "domain": "nike.com", "url": "https://www.nike.com/"},
        {"name": "Adidas", "domain": "adidas.com", "url": "https://www.adidas.com/"},
        {"name": "H&M", "domain": "hm.com", "url": "https://www2.hm.com/"},
        {"name": "Mango", "domain": "mango.com", "url": "https://shop.mango.com/"},
        {"name": "Uniqlo", "domain": "uniqlo.com", "url": "https://www.uniqlo.com/"},
        {"name": "COS", "domain": "cosstores.com", "url": "https://www.cos.com/"},
        {"name": "ASOS", "domain": "asos.com", "url": "https://www.asos.com/"},
    ],
}

TRENDING_QUERIES = {
    "tr": [
        "kadın trençkot 2025",
        "kadın blazer ceket",
        "nike dunk low kadın",
        "kadın deri omuz çantası",
        "wide leg jean kadın",
        "erkek bomber ceket",
    ],
    "en": [
        "women trench coat 2025",
        "women blazer jacket",
        "nike dunk low women",
        "women leather shoulder bag",
        "wide leg jeans women",
        "men bomber jacket",
    ],
}

def is_product_url(url):
    """URL gerçek bir ürün sayfası mı, yoksa arama/kategori sayfası mı?"""
    if not url: return False
    u = url.lower()

    # ❌ Kesinlikle ürün sayfası DEĞİL (arama/kategori sayfaları)
    search_patterns = ["/sr?", "/search?", "/arama?", "?q=", "?query=", "/kategori/",
                       "/category/", "/collection/", "/collections/", "/list/", "/listing/"]
    if any(sp in u for sp in search_patterns):
        return False

    # ✅ Bilinen mağazaların ürün URL pattern'leri
    product_patterns = {
        "trendyol.com": r'-p-\d+',          # trendyol.com/marka/urun-p-12345678
        "hepsiburada.com": r'-p[m]?-[A-Za-z0-9]+',  # hepsiburada.com/urun-pm-HB00123 veya -p-12345
        "amazon.": r'/dp/[A-Z0-9]+|/gp/product/', # amazon.com/dp/B08XYZ
        "n11.com": r'/urun/',
        "boyner.com": r'/urun/',
        "beymen.com": r'/urun/',
        "defacto.com": r'/\w+-\w+',
        "lcwaikiki.com": r'/tr-tr/',
        "zara.com": r'/tr/.+/p\d+',
        "bershka.com": r'/tr/.+/\d+',
        "hm.com": r'/productpage\.',
        "nike.com": r'/t/',
    }

    for domain, pattern in product_patterns.items():
        if domain in u:
            return bool(re.search(pattern, u))

    # Bilinmeyen domain: arama pattern'i yoksa ürün kabul et
    return True


def _fetch_trending_products(lang="tr"):
    """Google organic + Shopping'den trending ürünleri çek — SADECE DOĞRUDAN ÜRÜN SAYFALARI."""
    cc = "tr" if lang == "tr" else "us"
    cfg = get_country_config(cc)
    products = []
    queries = TRENDING_QUERIES.get(lang, TRENDING_QUERIES["en"])
    for q in queries:
        try:
            found = False

            # 1) Google Shopping — direct link + ürün sayfası kontrolü
            d = GoogleSearch({"engine": "google_shopping", "q": q, "gl": cfg["gl"], "hl": cfg["hl"], "api_key": SERPAPI_KEY, "num": 5}).get_dict()
            for item in d.get("shopping_results", [])[:8]:
                direct_link = item.get("link", "")
                ttl = item.get("title", "")
                src = item.get("source", "")
                pr = item.get("price", str(item.get("extracted_price", "")))
                thumb = item.get("thumbnail", "")

                # Sadece doğrudan mağaza + ürün sayfası kabul et
                if not direct_link or "google.com" in direct_link or is_blocked(direct_link):
                    continue
                if not is_product_url(direct_link):
                    print(f"  Trending SKIP (not product): {direct_link[:80]}")
                    continue

                if ttl and thumb:
                    best_link = localize_url(direct_link, cc)
                    products.append({
                        "title": ttl[:40],
                        "brand": src,
                        "img": thumb,
                        "price": pr,
                        "link": make_affiliate(best_link),
                    })
                    found = True
                    print(f"  Trending OK (shopping): {ttl[:40]} → {best_link[:60]}")
                    break

            # 2) Google organic — fashion domain'lerden ürün sayfası bul
            if not found:
                d2 = GoogleSearch({"engine": "google", "q": q, "gl": cfg["gl"], "hl": cfg["hl"], "api_key": SERPAPI_KEY, "num": 10}).get_dict()

                for item in d2.get("organic_results", [])[:8]:
                    lnk = item.get("link", "")
                    ttl = item.get("title", "")
                    src = item.get("displayed_link", "")
                    thumb = item.get("thumbnail", "")
                    if not lnk or not ttl or is_blocked(lnk): continue
                    if not is_fashion(lnk, ttl, src): continue
                    if not is_product_url(lnk):
                        print(f"  Trending SKIP organic (not product): {lnk[:80]}")
                        continue

                    lnk = localize_url(lnk, cc)
                    snippet = item.get("snippet", "")
                    pr_match = re.search(r'(\d[\d.,]+)\s*(?:TL|₺|\$|€|£)', snippet)
                    products.append({
                        "title": ttl[:40],
                        "brand": get_brand(lnk, src) or src,
                        "img": thumb or "",
                        "price": pr_match.group(0) if pr_match else "",
                        "link": make_affiliate(lnk),
                    })
                    found = True
                    print(f"  Trending OK (organic): {ttl[:40]} → {lnk[:60]}")
                    break

            if not found:
                print(f"  Trending: no product URL found for '{q}'")

        except Exception as e:
            print(f"Trending fetch err ({q}): {e}")
    return products

def _get_trending(lang="tr"):
    """Trending data al — cache varsa cache'den, yoksa SerpAPI'den çek."""
    now = time.time()
    cached = TRENDING_CACHE.get(lang)
    if cached and (now - cached["ts"]) < TRENDING_TTL:
        return cached

    brands = BRAND_DATA.get(lang, BRAND_DATA["en"])
    labels = {"tr": ("🏷️ Popüler Markalar", "🔥 Bu Hafta Trend"), "en": ("🏷️ Popular Brands", "🔥 Trending This Week")}
    lb = labels.get(lang, labels["en"])

    # SerpAPI'den gerçek ürünleri çek
    products = _fetch_trending_products(lang)
    if not products:
        # Fallback — SerpAPI yoksa boş göster
        products = []

    data = {
        "brands": brands,
        "products": products,
        "section_brands": lb[0],
        "section_trending": lb[1],
        "ts": now,
    }
    TRENDING_CACHE[lang] = data
    print(f"🔥 Trending refreshed ({lang}): {len(products)} products, {len(brands)} brands")
    return data

CC_LANG_MAP = {"tr": "tr", "us": "en", "uk": "en", "de": "en", "fr": "en", "sa": "en", "ae": "en", "eg": "en"}

@app.get("/api/trending")
async def trending(country: str = "tr", refresh: bool = False):
    cc = country.lower()
    lang = CC_LANG_MAP.get(cc, "en")
    if refresh:
        TRENDING_CACHE.pop(lang, None)  # Cache temizle
    data = _get_trending(lang)
    return {"success": True,
            "brands": [{"name": b["name"], "url": b.get("url", ""), "domain": b.get("domain", "")} for b in data["brands"]],
            "products": data["products"],
            "section_brands": data["section_brands"], "section_trending": data["section_trending"]}

# ─── BRAND LOGO SVG ENDPOINT ───
@app.get("/api/logo")
async def brand_logo(name: str = ""):
    """Marka adından SVG logo üret — compact display name + brand color."""
    if not name: return Response(content=b"", status_code=400)

    # Known brand display names (short, fits in 80px box)
    BRAND_DISPLAY = {
        "zara": "ZARA", "bershka": "BSK", "mango": "MNG", "nike": "NIKE",
        "adidas": "adi", "h&m": "H&M", "koton": "KTN", "pull&bear": "P&B",
        "uniqlo": "UQ", "cos": "COS", "asos": "ASOS",
    }
    display = BRAND_DISPLAY.get(name.lower(), name.upper()[:5])  # Max 5 chars for unknowns
    safe_display = html.escape(display)

    # Brand colors (distinctive on dark bg)
    colors = {"zara": "#ffffff", "bershka": "#a8d8a8", "mango": "#c8a265", "nike": "#ffffff",
              "adidas": "#ffffff", "h&m": "#cc0000", "koton": "#8a9a8a", "pull&bear": "#7a9a6a",
              "uniqlo": "#c41200", "cos": "#ffffff", "asos": "#b8b8b8"}
    text_color = colors.get(name.lower(), "#e0e0e0")

    # Brand-specific backgrounds
    bg_colors = {"h&m": "#1a0000", "nike": "#111", "adidas": "#0a0a0a", "uniqlo": "#1a0000"}
    bg = bg_colors.get(name.lower(), "#1a1a1a")

    # Font size based on DISPLAY length (not original name)
    fs = 20 if len(display) <= 3 else (16 if len(display) <= 4 else 13)
    # Adidas uses lowercase italic style
    style = 'font-style="italic"' if name.lower() == "adidas" else ''

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">
  <rect width="80" height="80" rx="14" fill="{bg}" stroke="#333" stroke-width="1"/>
  <text x="40" y="43" text-anchor="middle" dominant-baseline="middle"
    fill="{text_color}" font-family="system-ui,-apple-system,sans-serif"
    font-size="{fs}" font-weight="800" letter-spacing="0.5" {style}>{safe_display}</text>
  <text x="40" y="62" text-anchor="middle" dominant-baseline="middle"
    fill="#666" font-family="system-ui,sans-serif"
    font-size="7" font-weight="500">{html.escape(name)}</text>
</svg>'''
    return Response(content=svg.encode(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})

@app.get("/api/health")
async def health(): return {"status": "ok", "version": "v42-fitchy", "serpapi": bool(SERPAPI_KEY), "anthropic": bool(ANTHROPIC_API_KEY), "rembg": HAS_REMBG}

# ─── SESSION STORE (detect → search-piece) ───
DETECT_SESSIONS = {}  # detect_id → {pieces, img_url, crop_data, cc, created_at}
SESSION_TTL = 600  # 10 minutes

def session_cleanup():
    now = time.time()
    expired = [k for k, v in DETECT_SESSIONS.items() if now - v.get("created_at", 0) > SESSION_TTL]
    for k in expired: del DETECT_SESSIONS[k]

# ─── DETECT ENDPOINT (Fast: ~3sec) ───
@app.post("/api/detect")
async def detect_pieces(file: UploadFile = File(...), country: str = Form("tr")):
    """Step 1: Claude detects pieces + crops. Returns previews for user to pick."""
    if not SERPAPI_KEY: raise HTTPException(500, "No API key")
    cc = country.lower()
    contents = await file.read()
    session_cleanup()

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img_obj = img.copy()
        img_obj.thumbnail((1400, 1400))
        img.thumbnail((1400, 1400))
        buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95)
        optimized = buf.getvalue()
        b64 = base64.b64encode(optimized).decode()
        print(f"  Image: {img.size[0]}x{img.size[1]}, {len(optimized)//1024}KB sent to Claude")
    except Exception:
        img_obj = Image.open(io.BytesIO(contents)).convert("RGB")
        optimized = contents
        b64 = base64.b64encode(contents).decode()

    print(f"\n{'='*50}\n=== DETECT v41 === country={cc}")

    try:
        # Claude detect + Upload full image → PARALLEL
        detect_task = claude_detect(b64, cc)
        upload_task = upload_img(optimized)
        pieces, img_url = await asyncio.gather(detect_task, upload_task)

        if not pieces:
            return {"success": True, "detect_id": "", "pieces": [], "country": cc}
        # Only keep main searchable categories
        ALLOWED_CATS = {"jacket", "top", "bottom", "dress", "shoes", "bag", "watch"}
        pieces = [p for p in pieces if p.get("category", "") in ALLOWED_CATS][:4]
        if not pieces:
            return {"success": True, "detect_id": "", "pieces": [], "country": cc}
        print(f"Claude: {len(pieces)} pieces (filtered)")

        # Crop each piece + generate thumbnails
        crop_data = {}  # piece_idx → crop_bytes
        piece_results = []
        for i, p in enumerate(pieces):
            print(f"  → {p.get('category')} | brand={p.get('brand')} | text='{p.get('visible_text','')}' | box={p.get('box_2d')}")
            crop_b64 = ""
            box = p.get("box_2d")
            if box and isinstance(box, list) and len(box) == 4:
                try:
                    cropped_bytes = await asyncio.to_thread(crop_piece, img_obj, box)
                    if cropped_bytes:
                        crop_data[i] = cropped_bytes
                        # Generate 200px thumbnail for picker UI
                        t_img = Image.open(io.BytesIO(cropped_bytes)).convert("RGB")
                        t_img.thumbnail((200, 200))
                        t_buf = io.BytesIO(); t_img.save(t_buf, format="JPEG", quality=80)
                        crop_b64 = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
                        print(f"    Cropped OK ({len(cropped_bytes)//1024}KB)")
                except Exception as e:
                    print(f"    Crop error: {e}")

            piece_results.append({
                "category": p.get("category", ""),
                "short_title": p.get("short_title", p.get("category", "").title()),
                "brand": p.get("brand", "") if p.get("brand", "") != "?" else "",
                "visible_text": p.get("visible_text", ""),
                "color": p.get("color", ""),
                "style_type": p.get("style_type", ""),
                "crop_image": crop_b64,
                "has_crop": i in crop_data,
            })

        # Store session
        detect_id = str(uuid.uuid4())[:12]
        DETECT_SESSIONS[detect_id] = {
            "pieces": pieces,
            "img_url": img_url,
            "crop_data": crop_data,
            "cc": cc,
            "created_at": time.time(),
        }
        print(f"  Session stored: {detect_id} ({len(pieces)} pieces, {len(crop_data)} crops)")

        return {"success": True, "detect_id": detect_id, "pieces": piece_results, "country": cc}
    except Exception as e:
        print(f"DETECT FAILED: {e}")
        import traceback; traceback.print_exc()
        return {"success": False, "message": str(e), "pieces": []}


# ─── SEARCH-PIECE ENDPOINT (Fast: ~3-5sec for single piece) ───
@app.post("/api/search-piece")
async def search_piece(detect_id: str = Form(""), piece_index: int = Form(0), country: str = Form("tr")):
    """Step 2: Search for a single selected piece."""
    if not SERPAPI_KEY: raise HTTPException(500, "No API key")

    session = DETECT_SESSIONS.get(detect_id)
    if not session:
        return {"success": False, "message": "Session expired. Please rescan."}

    cc = session["cc"]
    pieces = session["pieces"]
    if piece_index < 0 or piece_index >= len(pieces):
        return {"success": False, "message": "Invalid piece"}

    p = pieces[piece_index]
    img_url = session.get("img_url", "")
    crop_bytes = session.get("crop_data", {}).get(piece_index)
    cfg = get_country_config(cc)

    cat = p.get("category", "")
    brand = p.get("brand", "")
    visible_text = p.get("visible_text", "")
    print(f"\n{'='*50}\n=== SEARCH PIECE v41: [{cat}] === brand={brand} text='{visible_text}'")

    # Build search queries for this piece (v42: merged OCR into specific)
    q_specific = p.get("search_query_specific", "").strip()
    q_generic = p.get("search_query_generic", "").strip()

    # Merge OCR keywords into specific query
    queries = []
    if q_specific:
        q_lower = q_specific.lower()
        extra_ocr = []
        if visible_text and visible_text.lower() not in ["none", "?", "", "yok"]:
            for word in visible_text.replace(",", " ").split():
                clean = re.sub(r'[^\w]', '', word)
                if len(clean) > 2 and clean.lower() not in q_lower and clean.lower() not in ["none", "yok", "the", "and", "for"]:
                    extra_ocr.append(clean)
                    if len(extra_ocr) >= 2: break
        if extra_ocr:
            queries.append((q_specific + " " + " ".join(extra_ocr), "specific"))
        else:
            queries.append((q_specific, "specific"))
    elif q_generic:
        queries.append((q_generic, "generic"))

    for q, pri in queries:
        print(f"  {pri}: '{q}'")

    try:
        # ── ALL searches in parallel (v42: no google organic) ──
        async def do_shop(q, limit=8):
            async with API_SEM:
                return await asyncio.to_thread(_shop, q, cc, limit)

        async def do_piece_lens():
            if not crop_bytes: return []
            url = await upload_img(crop_bytes)
            if url:
                async with API_SEM:
                    return await asyncio.to_thread(_lens, url, cc, "all")
            return []

        async def do_full_lens_exact():
            if img_url:
                async with API_SEM:
                    return await asyncio.to_thread(_lens, img_url, cc, "exact_matches")
            return []

        # v42 OPTIMIZED: removed full_lens_visual + google organic
        tasks = []
        task_labels = []

        # 1. Full image exact Lens
        tasks.append(do_full_lens_exact()); task_labels.append("exact_lens")
        # 2. Per-piece crop Lens (exact+visual)
        tasks.append(do_piece_lens()); task_labels.append("piece_lens")
        # 3. Shopping (1 merged query)
        for q, pri in queries:
            tasks.append(do_shop(q)); task_labels.append(f"shop_{pri}")

        serpapi_calls = len(tasks)
        print(f"  v42 optimized: {serpapi_calls} SerpAPI calls")

        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # ── Collect results ──
        exact_lens_results = all_results[0] if not isinstance(all_results[0], Exception) else []
        piece_lens_results = all_results[1] if not isinstance(all_results[1], Exception) else []

        shop_results = []
        for idx in range(2, len(all_results)):
            r = all_results[idx] if not isinstance(all_results[idx], Exception) else []
            label = task_labels[idx]
            if label.startswith("shop_"): shop_results.extend(r)

        # ── Filter full-image Lens results to THIS piece using keywords ──
        piece_kws = PIECE_KEYWORDS.get(cat, [])
        def matches_piece(r):
            t = (r.get("title", "") + " " + r.get("source", "")).lower()
            return any(kw in t for kw in piece_kws)

        # Exact lens: filter to this piece (or include all if only 1 piece)
        filtered_exact = []
        for r in exact_lens_results:
            if len(pieces) == 1 or matches_piece(r) or r.get("_exact"):
                filtered_exact.append(r)

        # Combine all lens results (no more full_lens_visual backup)
        all_lens = filtered_exact + piece_lens_results

        print(f"  Results: exact={len(filtered_exact)} piece_lens={len(piece_lens_results)} shop={len(shop_results)}")
        if filtered_exact:
            for r in filtered_exact[:3]:
                print(f"    ✅ EXACT: {r.get('title','')[:50]} | {r.get('source','')}")

        # ── Score everything (v42: 2 channels — lens + shopping) ──
        shop_links = {r.get("link", "") for r in shop_results}
        lens_links = {r.get("link", "") for r in all_lens}

        def score_result(r, base_score=0):
            score = base_score
            combined = (r.get("title", "") + " " + r.get("link", "") + " " + r.get("source", "")).lower()
            if r.get("_exact"): score += 50; r["ai_verified"] = True
            link = r.get("link", "")
            # Cross-channel: same product in both lens + shopping = reliable
            if link in shop_links and link in lens_links:
                score += 25; r["ai_verified"] = True
            if brand and brand != "?" and len(brand) > 2 and brand.lower() in combined: score += 8
            if visible_text and visible_text.lower() not in ["none", "?", ""]:
                for vt in visible_text.lower().replace(",", " ").split():
                    if len(vt) > 2 and vt in combined: score += 10; break
            if r.get("price"): score += 2
            if r.get("is_local"): score += 3
            return score

        seen = set()
        all_items = []

        # Lens (highest priority)
        for r in all_lens:
            if r.get("link") and r["link"] not in seen:
                # v42: Category mismatch + non-clothing filters
                if is_category_mismatch(r.get("title", ""), cat): continue
                if is_non_clothing_product(r.get("title", "")): continue
                seen.add(r["link"])
                r["_score"] = score_result(r, 18)
                all_items.append(r)

        # Shopping
        for r in shop_results:
            if r.get("link") and r["link"] not in seen:
                if is_category_mismatch(r.get("title", ""), cat): continue
                if is_non_clothing_product(r.get("title", "")): continue
                seen.add(r["link"])
                r["_score"] = score_result(r, 15)
                all_items.append(r)

        all_items.sort(key=lambda x: -x.get("_score", 0))

        # 🇹🇷 TR-FIRST: Local results first, foreign only fills remaining slots
        local_items = [r for r in all_items if r.get("is_local")]
        foreign_items = [r for r in all_items if not r.get("is_local")]
        foreign_exact = [r for r in foreign_items if r.get("_exact")]
        foreign_rest = [r for r in foreign_items if not r.get("_exact")]
        all_items = foreign_exact + local_items + foreign_rest

        # Match confidence
        top_score = all_items[0].get("_score", 0) if all_items else 0
        has_brand = any(brand and brand != "?" and brand.lower() in (r.get("title","")+" "+r.get("link","")).lower() for r in all_items[:3]) if brand and brand != "?" else False
        has_text = False
        if visible_text and visible_text.lower() not in ["none", "?", ""]:
            vt_words = [w for w in visible_text.lower().replace(",", " ").split() if len(w) > 2]
            for r in all_items[:3]:
                if any(w in r.get("title", "").lower() for w in vt_words): has_text = True; break

        if top_score >= 50: match_level = "exact"
        elif top_score >= 25 and (has_brand or has_text): match_level = "exact"
        elif top_score >= 15 or has_brand: match_level = "close"
        else: match_level = "similar"

        for j, r in enumerate(all_items[:3]):
            print(f"  #{j+1}: score={r.get('_score',0)} {r.get('title','')[:50]}")
        print(f"  match={match_level} brand={has_brand} text={has_text}")

        # Clean internal fields
        for r in all_items:
            for k in ["_score", "_priority", "_channel", "_src", "_exact"]: r.pop(k, None)

        crop_b64 = ""
        if crop_bytes:
            try:
                t_img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")
                t_img.thumbnail((128, 128))
                t_buf = io.BytesIO(); t_img.save(t_buf, format="JPEG", quality=75)
                crop_b64 = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
            except: pass

        return {
            "success": True,
            "piece": {
                "category": cat,
                "short_title": p.get("short_title", cat.title()),
                "brand": brand if brand != "?" else "",
                "visible_text": visible_text,
                "products": all_items[:8],
                "lens_count": len(all_lens),
                "match_level": match_level,
                "crop_image": crop_b64,
            },
            "country": cc,
            "_search_query": queries[0][0] if queries else "",  # For "load more" button
        }
    except Exception as e:
        print(f"SEARCH PIECE FAILED: {e}")
        import traceback; traceback.print_exc()
        return {"success": False, "message": str(e)}


# ─── LOAD MORE: On-demand Google Organic (saves SerpAPI credits) ───
@app.post("/api/load-more")
async def load_more(query: str = Form(""), country: str = Form("tr"), exclude: str = Form("")):
    """Lazy-load Google organic results — only when user taps 'More Results'."""
    if not SERPAPI_KEY or not query:
        return {"success": False, "products": []}
    cc = country.lower()
    exclude_links = set(json.loads(exclude)) if exclude else set()

    print(f"\n=== LOAD MORE === q='{query}' cc={cc}")
    async with API_SEM:
        results = await asyncio.to_thread(_google_organic, query, cc, 10)

    # Filter out already-shown results
    products = []
    for r in results:
        if r.get("link", "") not in exclude_links:
            products.append(r)
    print(f"  Google organic: {len(results)} total, {len(products)} new")
    return {"success": True, "products": products[:8]}

@app.get("/favicon.ico")
async def favicon(): return Response(content=b"", media_type="image/x-icon")

# ─── PWA: Manifest + Service Worker + Icons ───
@app.get("/manifest.json")
async def pwa_manifest():
    manifest = {
        "name": "Fitchy",
        "short_name": "Fitchy",
        "description": "Fotoğraftaki kıyafeti bul, anında satın al",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0c",
        "theme_color": "#0a0a0c",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    return Response(content=json.dumps(manifest), media_type="application/json")

@app.get("/sw.js")
async def service_worker():
    sw_code = """
const CACHE_NAME = 'fitchy-v42';
const PRECACHE = ['/', '/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(names =>
    Promise.all(names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API calls: network only (never cache search results)
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/sw.js')) return;
  // HTML/CSS/JS: network first, fallback to cache
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) {
        const clone = r.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      }
      return r;
    }).catch(() => caches.match(e.request))
  );
});
""".strip()
    return Response(content=sw_code, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

def _generate_app_icon(size=192):
    """Generate Fitchy app icon as PNG."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark circle background
    padding = int(size * 0.05)
    draw.ellipse([padding, padding, size-padding, size-padding], fill='#0a0a0c')
    # Gold accent ring
    ring_w = max(2, size // 48)
    draw.ellipse([padding, padding, size-padding, size-padding], outline='#d4a853', width=ring_w)
    # "F" letter
    font_size = int(size * 0.5)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "F", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1] - int(size * 0.02)
    draw.text((x, y), "F", fill='#d4a853', font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

_ICON_CACHE = {}

@app.get("/icon-192.png")
async def icon_192():
    if 192 not in _ICON_CACHE:
        _ICON_CACHE[192] = _generate_app_icon(192)
    return Response(content=_ICON_CACHE[192], media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})

@app.get("/icon-512.png")
async def icon_512():
    if 512 not in _ICON_CACHE:
        _ICON_CACHE[512] = _generate_app_icon(512)
    return Response(content=_ICON_CACHE[512], media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})

# ─── IMAGE PROXY (bypass hotlink protection for trending images) ───
IMG_CACHE = {}  # url_hash → (content_type, bytes)

@app.get("/api/img")
async def proxy_img(url: str = ""):
    if not url: return Response(content=b"", status_code=400)
    url_hash = md5(url.encode()).hexdigest()
    if url_hash in IMG_CACHE:
        ct, data = IMG_CACHE[url_hash]
        return Response(content=data, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    try:
        # Extract domain for Referer/Origin spoof
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                "Referer": origin + "/",
                "Origin": origin,
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "same-origin",
            })
            if r.status_code == 200 and len(r.content) > 100:
                ct = r.headers.get("content-type", "image/jpeg")
                if "image" in ct or "octet" in ct:
                    IMG_CACHE[url_hash] = (ct, r.content)
                    return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"Proxy img err: {e}")
    return Response(content=b"", status_code=404)

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
<meta name="theme-color" content="#0a0a0c">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Fitchy">
<meta name="description" content="Fotoğraftaki kıyafeti bul, anında satın al">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Fitchy</title>
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
.piece-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.piece-card{background:var(--card);border-radius:12px;border:2px solid var(--border);overflow:hidden;cursor:pointer;transition:border-color .2s,transform .15s;animation:fadeUp .35s ease both}
.piece-card:hover,.piece-card:active{border-color:var(--accent);transform:scale(1.02)}
.piece-card img{width:100%;height:140px;object-fit:cover;display:block}
.piece-card .pc-info{padding:10px}
.piece-card .pc-cat{font-size:12px;font-weight:700;display:flex;align-items:center;gap:6px}
.piece-card .pc-brand{font-size:9px;font-weight:700;color:var(--bg);background:var(--accent);padding:1px 6px;border-radius:3px;margin-top:4px;display:inline-block}
.piece-card .pc-text{font-size:9px;color:var(--accent);font-style:italic;margin-top:3px}
.piece-card .pc-noimg{width:100%;height:140px;display:flex;align-items:center;justify-content:center;font-size:48px;background:rgba(255,255,255,.03)}
.scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}
.card{flex-shrink:0;width:135px;background:var(--card);border-radius:10px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text)}
.card.local{border-color:var(--accent)}
.card img{width:135px;height:110px;object-fit:cover;display:block}
.card .ci{padding:8px}
.card .cn{font-size:10px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .cs{font-size:9px;color:var(--dim);margin-top:2px}
.card .cp{font-size:13px;font-weight:700;color:var(--accent);margin-top:3px}
.bnav{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:440px;background:rgba(10,10,12,.93);backdrop-filter:blur(20px);border-top:1px solid var(--border);display:flex;padding:8px 0 22px;z-index:50}
.sec-title{font-size:15px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.brand-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:28px}
.brand-chip{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 6px;text-align:center;cursor:pointer;transition:border-color .2s,transform .15s;text-decoration:none;display:block}
.brand-chip:hover,.brand-chip:active{border-color:var(--accent);transform:scale(1.04)}
.brand-chip .b-logo{width:48px;height:48px;border-radius:12px;margin:0 auto 6px;overflow:hidden}
.brand-chip .b-name{font-size:10px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend-scroll{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px;margin-bottom:28px}
.trend-card{flex-shrink:0;width:150px;background:var(--card);border-radius:12px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text);transition:border-color .2s}
.trend-card:hover{border-color:var(--accent)}
.trend-card .tc-img{width:150px;height:170px;overflow:hidden;background:#1a1a1a;display:flex;align-items:center;justify-content:center}
.trend-card .tc-img img{width:100%;height:100%;object-fit:cover;display:block}
.trend-card .tc-img.no-img{background:linear-gradient(135deg,#1a1a1a,#2a2a2a)}
.trend-card .tc-img.no-img img{display:none}
.trend-card .tc-info{padding:10px}
.trend-card .tc-title{font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend-card .tc-brand{font-size:9px;color:var(--muted);margin-top:2px}
.trend-card .tc-price{font-size:14px;font-weight:700;color:var(--accent);margin-top:4px}
</style>
</head>
<body>
<div class="app">
  <div id="home" style="padding:0 20px">
    <div style="padding-top:56px;padding-bottom:28px">
      <p style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:10px">Fitchy</p>
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
    <div id="trendingSection" style="margin-top:28px;padding-bottom:100px"></div>
  </div>
  <div id="rScreen" style="display:none">
    <div style="position:sticky;top:0;z-index:40;background:rgba(10,10,12,.9);backdrop-filter:blur(20px);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
      <div onclick="goHome()" style="cursor:pointer;color:var(--muted);font-size:13px" id="backBtn"></div>
      <div style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2.5px">FITCHY</div>
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
      <div id="piecePicker" style="display:none"></div>
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
  tr:{flag:"\u{1F1F9}\u{1F1F7}",heroTitle:'Gorseldeki outfiti<br><span style="color:var(--accent)">birebir</span> bul.',heroSub:'Fotograf yukle, AI parcalari tespit etsin<br>istedigin parcaya tikla, birebir bulsun.',upload:'Fotograf Yukle',uploadSub:'Galeri veya screenshot',auto:'\u{1F916} Otomatik Tara',manual:'\u{2702}\u{FE0F} Kendim Seceyim',feat1:'Otomatik Tara',feat1d:'AI parcalari tespit eder, sen sec',feat2:'Kendim Seceyim',feat2d:'Parmaginla parcayi sec, birebir bul',back:'\u2190 Geri',cropHint:'\u{1F447} Aramak istedigin parcayi cercevele',manualPh:'Opsiyonel: ne aradigini yaz',find:'\u{1F50D} Bu Parcayi Bul',cancel:'\u2190 Vazgec',loading:'Parcalar tespit ediliyor...',loadingManual:'AI analiz ediyor...',noResult:'Sonuc bulunamadi',noProd:'Urun bulunamadi',retry:'\u{2702}\u{FE0F} Kendim Seceyim ile Tekrar Dene',another:'\u{2702}\u{FE0F} Baska Parca Sec',selected:'Sectigin Parca',lensMatch:'gorsel eslesme',recommended:'\u{2728} Onerilen',lensLabel:'\u{1F3AF} AI Eslesmesi',goStore:'Magazaya Git \u2197',noPrice:'Fiyat icin tikla',alts:'\u{1F4B8} Alternatifler \u{1F449}',navHome:'Kesfet',navFav:'Favoriler',aiMatch:'AI Onayli Eslesme',matchExact:'\u2705 Birebir Eslesme',matchClose:'\u{1F7E1} Yakin Eslesme',matchSimilar:'\u{1F7E0} Benzer Urunler',step_detect:'AI parcalari tespit ediyor...',step_bg:'Gorsel hazirlaniyor...',step_lens:'AI magazalari tariyor...',step_ai:'AI urunleri karsilastiriyor...',step_verify:'Birebir eslesme kontrolu...',step_done:'Sonuclar hazirlaniyor...',piecesFound:'parca tespit edildi',pickPiece:'Aramak istedigin parcaya tikla',searchingPiece:'Parca araniyor...',backToPieces:'\u2190 Diger Parcalar',noDetect:'Parca tespit edilemedi. Kendim Seceyim ile deneyin.',loadMore:'\u{1F50E} Daha Fazla Sonuc',loadingMore:'Ek sonuclar araniyor...'},
  en:{flag:"\u{1F1FA}\u{1F1F8}",heroTitle:'Find the outfit<br>in the photo, <span style="color:var(--accent)">exactly</span>.',heroSub:'Upload a photo, AI detects each piece<br>tap any piece to find it instantly.',upload:'Upload Photo',uploadSub:'Gallery or screenshot',auto:'\u{1F916} Auto Scan',manual:'\u{2702}\u{FE0F} Select Myself',feat1:'Auto Scan',feat1d:'AI detects pieces, you pick one to search',feat2:'Select Myself',feat2d:'Select the piece with your finger, find exact match',back:'\u2190 Back',cropHint:'\u{1F447} Frame the piece you want to find',manualPh:'Optional: describe what you\'re looking for',find:'\u{1F50D} Find This Piece',cancel:'\u2190 Cancel',loading:'Detecting pieces...',loadingManual:'AI analyzing...',noResult:'No results found',noProd:'No products found',retry:'\u{2702}\u{FE0F} Try Select Myself',another:'\u{2702}\u{FE0F} Select Another Piece',selected:'Your Selection',lensMatch:'visual match',recommended:'\u{2728} Recommended',lensLabel:'\u{1F3AF} AI Match',goStore:'Go to Store \u2197',noPrice:'Click for price',alts:'\u{1F4B8} Alternatives \u{1F449}',navHome:'Explore',navFav:'Favorites',aiMatch:'AI Verified Match',matchExact:'\u2705 Exact Match',matchClose:'\u{1F7E1} Close Match',matchSimilar:'\u{1F7E0} Similar Items',step_detect:'AI detecting pieces...',step_lens:'AI scanning stores...',step_match:'Matching products...',step_done:'Preparing results...',step_bg:'Preparing image...',step_search:'Scanning global stores...',step_ai:'AI comparing products...',step_verify:'Verifying exact match...',piecesFound:'pieces detected',pickPiece:'Tap a piece to search for it',searchingPiece:'Searching for piece...',backToPieces:'\u2190 Other Pieces',noDetect:'No pieces detected. Try Select Myself.',loadMore:'\u{1F50E} More Results',loadingMore:'Loading more results...'}
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
  document.getElementById('btnAuto').innerHTML=t('auto');
  document.getElementById('btnManual').innerHTML=t('manual');
  document.getElementById('backBtn').textContent=t('back');
  document.getElementById('cropHint').textContent=t('cropHint');
  document.getElementById('manualQ').placeholder=t('manualPh');
  document.getElementById('btnFind').innerHTML=t('find');
  document.getElementById('btnCancel').innerHTML=t('cancel');
  document.getElementById('navHome').textContent=t('navHome');
  document.getElementById('navFav').textContent=t('navFav');
  loadTrending();
}
function loadTrending(){
  fetch('/api/trending?country='+getCC()).then(function(r){return r.json()}).then(function(d){
    if(!d.success)return;
    var ts=document.getElementById('trendingSection');var h='';
    // Brands with SVG logos from server
    if(d.brands&&d.brands.length){
      h+='<div class="sec-title">'+d.section_brands+'</div><div class="brand-grid">';
      var brandStyles={
        'Zara':{bg:'#000',color:'#fff',text:'ZARA',size:'15px',ls:'3px'},
        'Bershka':{bg:'#000',color:'#fff',text:'BERSHKA',size:'9px',ls:'2px'},
        'Mango':{bg:'#000',color:'#fff',text:'MANGO',size:'10px',ls:'2px'},
        'Nike':{bg:'#000',color:'#fff',text:'NIKE',size:'14px',ls:'2px'},
        'Adidas':{bg:'#000',color:'#fff',text:'ADIDAS',size:'10px',ls:'1px'},
        'H&M':{bg:'#cc0000',color:'#fff',text:'H&M',size:'16px',ls:'0'},
        'Koton':{bg:'#000',color:'#fff',text:'KOTON',size:'10px',ls:'2px'},
        'Pull&Bear':{bg:'#000',color:'#fff',text:'PULL&BEAR',size:'7px',ls:'1px'},
        'Uniqlo':{bg:'#c00',color:'#fff',text:'UNIQLO',size:'10px',ls:'1px'},
        'COS':{bg:'#000',color:'#fff',text:'COS',size:'16px',ls:'3px'},
        'ASOS':{bg:'#000',color:'#fff',text:'ASOS',size:'13px',ls:'1px'},
      };
      for(var i=0;i<d.brands.length;i++){var b=d.brands[i];
        var s=brandStyles[b.name]||{bg:'#1a1a1a',color:'#d4a853',text:b.name.toUpperCase().substring(0,5),size:'11px',ls:'1px'};
        h+='<a href="'+(b.url||'#')+'" target="_blank" rel="noopener" class="brand-chip">';
        h+='<div class="b-logo" style="background:'+s.bg+';display:flex;align-items:center;justify-content:center;font-weight:800;font-size:'+s.size+';color:'+s.color+';letter-spacing:'+s.ls+';font-family:\'DM Sans\',sans-serif">'+s.text+'</div>';
        h+='<div class="b-name">'+b.name+'</div></a>';}
      h+='</div>';
    }
    // Trending products with real images from SerpAPI
    if(d.products&&d.products.length){
      h+='<div class="sec-title">'+d.section_trending+'</div><div class="trend-scroll">';
      for(var i=0;i<d.products.length;i++){var p=d.products[i];
        h+='<a href="'+p.link+'" target="_blank" rel="noopener" class="trend-card">';
        h+='<div class="tc-img"><img src="'+p.img+'" onerror="this.onerror=null;this.parentElement.classList.add(\'no-img\')"></div>';
        h+='<div class="tc-info"><div class="tc-title">'+p.title+'</div><div class="tc-brand">'+p.brand+'</div><div class="tc-price">'+(p.price||t('noPrice'))+'</div></div></a>';}
      h+='</div>';
    }
    ts.innerHTML=h;
  }).catch(function(){})
}
applyLang();
// PWA: Register service worker
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){})}
function getCC(){return CC}
document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});
function loadF(f){if(!f.type.startsWith('image/'))return;cF=f;var r=new FileReader();r.onload=function(e){cPrev=e.target.result;showScreen()};r.readAsDataURL(f)}
function showScreen(){document.getElementById('home').style.display='none';document.getElementById('rScreen').style.display='block';document.getElementById('prev').src=cPrev;document.getElementById('prev').style.maxHeight='260px';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex';document.getElementById('cropMode').style.display='none';document.getElementById('piecePicker').style.display='none';document.getElementById('ld').style.display='none';document.getElementById('err').style.display='none';document.getElementById('res').style.display='none';if(cropper){cropper.destroy();cropper=null}}
function goHome(){if(_busy)return;document.getElementById('home').style.display='block';document.getElementById('rScreen').style.display='none';if(cropper){cropper.destroy();cropper=null}cF=null;cPrev=null;_detectId='';_detectedPieces=[]}

var _detectId='',_detectedPieces=[];
var _lastSearchQuery='',_lastShownLinks=[];

function autoScan(){
  if(_busy)return;
  document.getElementById('actionBtns').style.display='none';
  showLoading(t('loading'),[t('step_detect'),t('step_lens'),t('step_verify'),t('step_done')]);
  var fd=new FormData();fd.append('file',cF);fd.append('country',getCC());
  fetch('/api/full-analyze',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){
    hideLoading();
    if(!d.success)return showErr(d.message||'Error');
    renderAuto(d);
  }).catch(function(e){hideLoading();showErr(e.message)})
}

function showPiecePicker(pieces){
  document.getElementById('prev').style.maxHeight='160px';
  document.getElementById('res').style.display='none';
  document.getElementById('err').style.display='none';
  var pp=document.getElementById('piecePicker');pp.style.display='block';
  var h='<div style="margin:14px 0"><div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><div style="font-size:20px">\u{1F9E9}</div><div><span style="font-size:18px;font-weight:700">'+pieces.length+' '+t('piecesFound')+'</span></div></div><p style="font-size:12px;color:var(--muted)">'+t('pickPiece')+'</p></div>';
  h+='<div class="piece-grid">';
  for(var i=0;i<pieces.length;i++){
    var p=pieces[i];var icon=IC[p.category]||'\u{1F455}';
    h+='<div class="piece-card" onclick="searchPiece('+i+')" style="animation-delay:'+(i*.08)+'s">';
    if(p.crop_image){h+='<img src="'+p.crop_image+'">'} else {h+='<div class="pc-noimg">'+icon+'</div>'}
    h+='<div class="pc-info"><div class="pc-cat">'+icon+' '+(p.short_title||p.category)+'</div>';
    if(p.brand)h+='<div class="pc-brand">'+p.brand+'</div>';
    var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div class="pc-text">"'+vt+'"</div>';
    h+='</div></div>';
  }
  h+='</div><button class="btn-main btn-outline" onclick="startManualFromPicker()" style="margin-top:16px">'+t('manual')+'</button>';
  pp.innerHTML=h;
}

function searchPiece(idx){
  if(_busy)return;
  document.getElementById('piecePicker').style.display='none';
  showLoading(t('searchingPiece'),[t('step_lens'),t('step_verify'),t('step_done')]);
  var fd=new FormData();fd.append('detect_id',_detectId);fd.append('piece_index',idx);fd.append('country',getCC());
  fetch('/api/search-piece',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){
    hideLoading();
    if(!d.success)return showErr(d.message||'Error');
    _lastSearchQuery=d._search_query||'';
    _lastShownLinks=(d.piece&&d.piece.products||[]).map(function(p){return p.link});
    renderPieceResult(d.piece);
  }).catch(function(e){hideLoading();showErr(e.message)})
}

function renderPieceResult(p){
  document.getElementById('prev').style.maxHeight='160px';
  var ra=document.getElementById('res');ra.style.display='block';
  var pr=p.products||[],lc=p.lens_count||0,hero=pr[0],alts=pr.slice(1);
  var iconHtml=p.crop_image?'<img src="'+p.crop_image+'" style="width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">':'<div style="width:52px;height:52px;border-radius:10px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:22px;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';
  var h='<div class="piece"><div class="p-hdr">'+iconHtml+'<div><span class="p-title">'+(p.short_title||p.category)+'</span>';
  if(p.brand&&p.brand!=='?')h+='<span class="p-brand">'+p.brand+'</span>';
  var ml=p.match_level||'similar',mlKey=ml==='exact'?'matchExact':(ml==='close'?'matchClose':'matchSimilar'),mlColor=ml==='exact'?'var(--green)':(ml==='close'?'#eab308':'#f97316');
  h+='<div style="font-size:9px;font-weight:700;color:'+mlColor+';margin-top:3px">'+t(mlKey)+'</div>';
  var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';
  if(lc>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">\u{1F3AF} '+lc+' '+t('lensMatch')+'</div>';
  h+='</div></div>';
  if(!hero){h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">'+t('noProd')+'</div>'}
  else{h+=heroHTML(hero,lc>0);if(alts.length>0)h+=altsHTML(alts)}
  h+='</div>';
  if(_lastSearchQuery)h+='<button id="loadMoreBtn" class="btn-main" style="margin-top:12px;background:var(--card);color:var(--accent);border:1px solid var(--border)" onclick="loadMoreResults()">'+t('loadMore')+'</button>';
  h+='<button class="btn-main btn-green" onclick="backToPieces()" style="margin-top:16px">'+t('backToPieces')+'</button>';
  h+='<button class="btn-main btn-outline" onclick="startManualFromPicker()" style="margin-top:8px">'+t('manual')+'</button>';
  ra.innerHTML=h;
}

function backToPieces(){
  document.getElementById('res').style.display='none';document.getElementById('err').style.display='none';
  if(_detectedPieces&&_detectedPieces.length>0){showPiecePicker(_detectedPieces)}else{showScreen()}
}

function startManualFromPicker(){
  document.getElementById('piecePicker').style.display='none';document.getElementById('res').style.display='none';
  startManual();
}
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
    var ml=p.match_level||'similar';
    var mlKey=ml==='exact'?'matchExact':(ml==='close'?'matchClose':'matchSimilar');
    var mlColor=ml==='exact'?'var(--green)':(ml==='close'?'#eab308':'#f97316');
    h+='<div style="font-size:9px;font-weight:700;color:'+mlColor+';margin-top:3px">'+t(mlKey)+'</div>';
    var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';
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

function _getFavs(){try{return JSON.parse(localStorage.getItem('fitchy_favs')||'[]')}catch(e){return[]}}
function _setFavs(f){try{localStorage.setItem('fitchy_favs',JSON.stringify(f))}catch(e){}}
function _hasFav(link){try{return(localStorage.getItem('fitchy_favs')||'').indexOf(link)>-1}catch(e){return false}}
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

function loadMoreResults(){
  if(_busy||!_lastSearchQuery)return;
  var btn=document.getElementById('loadMoreBtn');
  if(btn){btn.innerHTML=t('loadingMore');btn.style.opacity='0.5';btn.onclick=null}
  var fd=new FormData();fd.append('query',_lastSearchQuery);fd.append('country',getCC());fd.append('exclude',JSON.stringify(_lastShownLinks));
  fetch('/api/load-more',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){
    if(btn)btn.remove();
    if(!d.success||!d.products||!d.products.length)return;
    var ra=document.getElementById('res');
    var container=document.createElement('div');
    container.innerHTML='<div style="font-size:11px;color:var(--dim);margin:12px 0">\u{1F50E} '+t('loadMore')+'</div><div class="scroll">'+d.products.map(function(a){
      var img=a.thumbnail||a.image||'';var isFav=_hasFav(a.link);
      var safeT=(a.title||'').replace(/'/g,"\\'");var safeP=(a.price||'').replace(/'/g,"\\'");var safeB=(a.brand||a.source||'').replace(/'/g,"\\'");
      var h='<a href="'+a.link+'" target="_blank" rel="noopener" class="card'+(a.is_local?' local':'')+'" style="position:relative">';
      if(img)h+='<img src="'+img+'" onerror="this.hidden=true">';
      h+='<div class="ci"><div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div>';
      h+='<div onclick="toggleFav(event,\''+a.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:5px;right:5px;background:rgba(0,0,0,.6);color:#fff;padding:4px;border-radius:50%;cursor:pointer;font-size:10px;z-index:10;line-height:1">'+(isFav?'\u2764\uFE0F':'\u{1F90D}')+'</div></a>';
      return h;
    }).join('')+'</div>';
    // Insert before the back/manual buttons
    var buttons=ra.querySelectorAll('.btn-main');
    if(buttons.length>0)ra.insertBefore(container,buttons[0]);
    else ra.appendChild(container);
    // Track these new links too
    d.products.forEach(function(p){_lastShownLinks.push(p.link)});
  }).catch(function(){if(btn){btn.innerHTML=t('loadMore');btn.style.opacity='1';btn.onclick=loadMoreResults}})
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
