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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, FileResponse
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
    "tr": {"name": "Türkiye", "gl": "tr", "hl": "tr", "lang": "Turkish", "currency": "₺", "local_stores": ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep.", ".com.tr", "bershka.com/tr", "zara.com/tr", "pullandbear.com/tr", "stradivarius.com/tr", "massimodutti.com/tr", "hm.com/tr", "mango.com/tr", "nike.com/tr", "adidas.com.tr", "puma.com/tr", "calvinklein.com.tr", "tommy.com/tr", "gap.com.tr", "lacoste.com.tr", "occasion.com.tr", "derimod.", "ipekyol.", "vakko.", "network.", "dolap.", "gardrops.", "morhipo.", "lidyana.", "gratis.com"], "gender": {"male": "erkek", "female": "kadın"}},
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

def enhance_thumbnail_url(url):
    """Google Shopping thumbnail URL'sini yüksek çözünürlüğe çevir."""
    if not url: return url
    # Fix relative Google thumbnail URLs (missing domain)
    if url.startswith("images?q=tbn:") or url.startswith("/images?q=tbn:"):
        url = "https://encrypted-tbn0.gstatic.com/" + url.lstrip("/")
    # Google encrypted thumbnails — request larger size
    if "encrypted-tbn" in url and "gstatic.com" in url:
        if "=s" in url:
            url = re.sub(r'=s\d+', '=s500', url)
        elif "=w" in url:
            url = re.sub(r'=w\d+-h\d+', '=w500-h500', url)
        else:
            url += "=s500"
    # Google Shopping image URLs
    elif "gstatic.com/shopping" in url:
        if "=s" in url:
            url = re.sub(r'=s\d+', '=s500', url)
        elif "=w" in url:
            url = re.sub(r'=w\d+', '=w500', url)
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
    # v42: Celebrity / gossip / entertainment / news magazines
    "diezminutos.", "diez minutos", "hola.com", "revistavanityfair.",
    "who.com", "people.com", "tmz.", "eonline.", "justjared.",
    "dailymail.", "thesun.", "mirror.co.uk", "express.co.uk",
    "marca.com", "as.com", "abc.es", "elpais.", "elmundo.",
    "hurriyet.", "milliyet.", "sabah.com", "sozcu.", "haberturk.",
    "ensonhaber.", "ntv.", "cnn.", "bbc.", "foxnews.", "reuters.",
    "imdb.", "kinopoisk.", "rottentomatoes.", "letterboxd.",
    "celebrity", "paparazzi", "gossip", "tabloid",
    "/haber/", "/news/", "/noticias/", "/celebrit/", "/famous/",
    "/dizi/", "/series/", "/tv-show/", "/actress/", "/actor/",
    # v42: Blog / outfit inspiration / listicle sites
    "verdicto.", "jennysgou.", "mynet.", "bestoutfitstoday.", "lemon8.", "lemon8.com",
    "thefashionspot.", "whowhatwear.", "stylecaster.",
    "thezoereport.", "thevou.", "fashionactivation.",
    "outfittrends.", "thetrendspotter.", "manofmany.",
    "byrdie.", "instyle.", "marieclaire.",
    "old money", "outfit inspo", "outfit idea",
    "pieces-woman-needs", "pieces-you-need", "classy-winter",
    # v42: Social media profile viewer / stalker sites
    "twstalker", "sotwe.", "tweetdeck.", "nitter.", "threadreaderapp.",
    "socialblade.", "followerwonk.", "tweepi.", "twipu.",
    "pikdo.", "picuki.", "imginn.", "dumpor.", "instastalk",
    "gramhir.", "inflact.", "anon.ws", "storiesig.",
    "whotwi.", "twitteraudit.", "tweettunnel.",
    # Social media profiles (@ mentions in title = not a product)
    "twitter profile", "twitter.com/", "t.co/",
    "yandex.", "bing.com/images", "google.com/imgres", "duckduckgo.", "search.yahoo.", "baidu.",
    "imgur.", "flickr.", "500px.", "deviantart.", "artstation.",
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
    
    # Aranan kategori tespit edilenler arasındaysa → geçir
    if category in cat_scores:
        return False
    
    # Başka bir kategori tespit edildi AMA aranan değil → engelle
    if cat_scores:
        return True
    
    # Hiçbir kategori tespit edilmedi — genel başlık
    # Aksesuar kategorileri (watch, bag, sunglasses, hat, scarf) için
    # "giyim/dış giyim/outfit" gibi genel ifadeler → muhtemelen kıyafet, aksesuar değil
    SPECIFIC_CATS = {"watch", "bag", "sunglasses", "hat", "scarf", "accessory"}
    if category in SPECIFIC_CATS:
        # Genel giyim kelimeleri varsa → aksesuar aramada uyumsuz
        clothing_generics = ["giyim", "giysi", "clothing", "outfit", "kıyafet", "dış giyim",
                             "iç giyim", "modelleri", "koleksiyon", "collection", "fashion",
                             "sezon", "yeni sezon", "oversize", "regular fit", "slim fit",
                             "erkek", "kadın", "unisex"]
        if any(cg in tl for cg in clothing_generics):
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
    # v42: Celebrity / news / entertainment — ürün değil, haber
    "embajador", "actress", "actor", "actriz", "estrena", "villana",
    "celebrity", "famous", "paparazzi", "gossip", "premiere", "gala",
    "red carpet", "kırmızı halı", "ödül", "award", "festival",
    "dizi", "serie", "tv show", "film", "movie", "trailer",
    "interview", "röportaj", "entrevista", "haber", "news", "noticia",
    "oyuncu", "şarkıcı", "singer", "manken",
    "instagram", "tiktok", "youtube", "influencer",
    # v42: Social media profiles
    "twitter profile", "twstalker", "sotwe", "@grimaldi", "profile |",
    # v42: Search engines / image aggregators in title
    "yandex", "pinterest", "google görsel", "google images", "bing images",
    "görselleri görüntüle", "görselleri indirin", "görsel ara",
    # v42: Blog / listicle / outfit inspiration titles (not products)
    "pieces woman needs", "pieces you need", "must-have",
    "outfit inspiration", "outfit inspo", "outfit idea",
    "classy winter", "classy summer", "old money",
    "best outfits", "style guide", "nasıl giyilir", "nasıl kombinlenir",
    "modadaki ruh", "moda trendleri",
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
    "pantalon", "chemise", "veste", "robe", "blouson", "blousons",  # Fransızca
    "hose", "kleid", "jacke", "hemd", "anzug",     # Almanca
    "vestito", "gonna", "camicia", "giacca",        # İtalyanca
    "falda", "camisa", "vestido", "abrigo",         # İspanyolca
    "брюки", "куртка", "платье", "рубашка",         # Rusça
    # v42: Finnish
    "takki", "mekko", "housut", "paita", "kengät", "laukku",
    "naisten", "miesten", "netistä", "bombertakit", "bomberit",
    # v42: Swedish
    "jacka", "byxor", "klänning", "skor", "väska", "herr", "dam",
    # v42: Polish
    "kurtka", "spodnie", "sukienka", "buty", "torebka", "damska", "męska",
    # v42: Danish/Norwegian
    "jakke", "bukser", "kjole", "sko", "veske", "herre", "dame",
    # v42: Czech
    "bunda", "kalhoty", "šaty", "boty", "taška",
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
    # Social media profile detection: "@username" in title = not a product
    if re.search(r'@\w{2,}', title):
        return True
    # Listicle/blog detection: "20+ Pieces", "30+ Classy", "Best 10" etc.
    if re.search(r'\d+\+\s*(pieces|classy|best|outfit|style|essential|wardrobe|item|look)', tl):
        return True
    # "Most Popular", "Best Outfits" style listicle
    if re.search(r'^(most popular|best \d|top \d|\d+ best|\d+ top|\d+ elegant|\d+ essential|\d+ must)', tl):
        return True
    return any(ncp in tl for ncp in NON_CLOTHING_PRODUCTS)

def is_fashion(link, title, src):
    c = (link + " " + src).lower()
    if any(d in c for d in FASHION_DOMAINS): return True
    return any(k in (title + " " + src).lower() for k in FASHION_KW)

RIVAL_BRANDS = ["nike", "adidas", "puma", "zara", "hm", "bershka", "mango", "gucci", "prada", "balenciaga", "converse", "vans", "defacto", "koton", "lcw", "mavi", "colins", "levi", "tommy", "lacoste", "calvin klein", "massimo dutti", "pull&bear", "stradivarius"]

# v42: COLOR CONFLICT MAP — renk uyumsuzluğu tespiti
COLOR_CONFLICTS = {
    "beyaz": ["siyah", "black", "gri", "gray", "grey", "antrasit", "koyu", "dark", "lacivert", "navy", "kahve", "brown", "bordo"],
    "white": ["siyah", "black", "gri", "gray", "grey", "anthracite", "dark", "navy", "brown", "burgundy"],
    "siyah": ["beyaz", "white", "krem", "cream", "bej", "beige", "pembe", "pink", "sarı", "yellow", "açık", "light"],
    "black": ["beyaz", "white", "cream", "beige", "pink", "yellow", "light"],
    "krem": ["siyah", "black", "gri", "gray", "lacivert", "navy", "koyu", "dark"],
    "bej": ["siyah", "black", "gri", "gray", "lacivert", "navy", "koyu", "dark"],
    "mavi": ["kırmızı", "red", "pembe", "pink", "sarı", "yellow", "turuncu", "orange", "bordo"],
    "blue": ["red", "pink", "yellow", "orange", "burgundy"],
    "kırmızı": ["mavi", "blue", "yeşil", "green", "gri", "gray", "lacivert", "navy"],
    "red": ["blue", "green", "gray", "navy"],
    "pembe": ["siyah", "black", "yeşil", "green", "lacivert", "navy", "kahve", "brown"],
    "pink": ["black", "green", "navy", "brown"],
    "lacivert": ["krem", "cream", "pembe", "pink", "sarı", "yellow", "kırmızı", "red", "turuncu"],
    "navy": ["cream", "pink", "yellow", "red", "orange"],
}

# v42: SUB-TYPE CONFLICTS — aynı kategori içinde uyumsuz alt-tipler
# gömlek ≠ süveter, tişört ≠ kazak, vb.
SUB_TYPE_GROUPS = {
    "top": {
        "shirt": ["gömlek", "gomlek", "shirt", "bluz", "blouse"],
        "tshirt": ["tişört", "tisort", "t-shirt", "tee", "tshirt"],
        "sweater": ["kazak", "süveter", "triko", "sweater", "pullover", "jumper", "knit", "örgü"],
        "hoodie": ["hoodie", "sweatshirt", "kapüşon", "kapşon"],
        "polo": ["polo"],
    },
    "jacket": {
        "blazer": ["blazer", "ceket"],
        "bomber": ["bomber", "varsity"],
        "coat": ["mont", "kaban", "coat", "parka", "puffer"],
        "trench": ["trençkot", "trench"],
        "cardigan": ["hırka", "cardigan"],
        "vest": ["yelek", "vest"],
    },
    "bottom": {
        "pants": ["pantolon", "pants", "trousers", "chino", "slacks"],
        "jeans": ["jean", "denim", "jeans"],
        "shorts": ["şort", "short", "bermuda"],
        "skirt": ["etek", "skirt"],
        "jogger": ["jogger", "eşofman", "sweatpant"],
    },
    "shoes": {
        "sneaker": ["sneaker", "spor ayakkabı", "trainer", "runner"],
        "boot": ["bot", "boot", "çizme"],
        "loafer": ["loafer", "mokasen", "oxford"],
        "sandal": ["sandalet", "sandal", "terlik"],
        "heel": ["topuklu", "heel", "stiletto"],
    },
}

def detect_color_conflict(piece_color, result_title):
    """Parça rengi ile sonuç başlığındaki renk çelişiyor mu?"""
    if not piece_color or piece_color in ["?", "none", ""]: return False
    pc = piece_color.lower().strip()
    rt = result_title.lower()
    conflicts = COLOR_CONFLICTS.get(pc, [])
    return any(c in rt for c in conflicts)

def detect_subtype_conflict(piece_style, result_title, category):
    """Aynı kategori içinde alt-tip çelişiyor mu? (gömlek vs süveter)"""
    if not piece_style or not category: return False
    groups = SUB_TYPE_GROUPS.get(category, {})
    if not groups: return False
    ps = piece_style.lower()
    rt = result_title.lower()
    # Parçanın hangi alt-grubunda olduğunu bul
    piece_group = None
    for group_name, keywords in groups.items():
        if any(kw in ps for kw in keywords):
            piece_group = group_name
            break
    if not piece_group: return False
    # Sonucun hangi alt-grubunda olduğunu bul
    result_group = None
    for group_name, keywords in groups.items():
        if any(kw in rt for kw in keywords):
            result_group = group_name
            break
    if not result_group: return False
    # Farklı gruplardaysa → çelişki
    return piece_group != result_group

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
                "thumbnail": enhance_thumbnail_url(m.get("thumbnail", "")), "image": m.get("image", ""),
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
            # v42: Skip search/category pages
            if not is_product_url(lnk): continue
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
                "thumbnail": enhance_thumbnail_url(m.get("thumbnail", "")), "image": m.get("image", ""),
                "is_local": is_local(lnk, src, cfg)})
            if len(res) >= 25: break

    except Exception as e:
        print(f"Lens err ({lens_type}): {e}")

    def score(r):
        s = 0
        if r.get("_exact"): s += 100
        if r.get("price"): s += 10
        if r.get("is_local"): s += 15
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
            # v42: Skip search/category pages — only direct product links
            if not is_product_url(lnk):
                print(f"  ⛔ SHOP SKIP (not product URL): {lnk[:80]}")
                continue
            # v42: Non-clothing product filter (Starbucks bardak vs.)
            if is_non_clothing_product(ttl): continue
            if cc == "tr" and has_foreign_script(ttl): continue
            # v42: Spam domain + foreign clothing word
            if is_spam_domain(lnk, src): continue
            if cc == "tr" and has_foreign_clothing_word(ttl): continue
            seen.add(lnk)
            lnk = localize_url(lnk, cc)
            res.append({"title": ttl, "brand": get_brand(lnk, src), "source": src, "link": make_affiliate(lnk), "price": item.get("price", str(item.get("extracted_price", ""))), "thumbnail": enhance_thumbnail_url(item.get("thumbnail", "")), "image": "", "is_local": is_local(lnk, src, cfg)})
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
                "thumbnail": enhance_thumbnail_url(item.get("thumbnail", "")), "image": "",
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
                "thumbnail": enhance_thumbnail_url(item.get("thumbnail", "")), "image": "",
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
            piece_color = p.get("color", "")
            piece_style = p.get("style_type", "")

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
                rtitle = r.get("title", "")

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
                if r.get("is_local"): score += 15

                # v42: COLOR PENALTY — beyaz ararken gri gelirse cezalandır
                if detect_color_conflict(piece_color, rtitle):
                    score -= 30
                    print(f"      🎨 COLOR PENALTY: [{piece_color}] vs '{rtitle[:40]}'")

                # v42: SUB-TYPE PENALTY — gömlek ararken süveter gelirse cezalandır
                if detect_subtype_conflict(piece_style, rtitle, cat):
                    score -= 25
                    print(f"      👕 SUBTYPE PENALTY: [{piece_style}] vs '{rtitle[:40]}'")

                # v42: CATEGORY RELEVANCE — sonuçta aranan kategorinin kelimesi var mı?
                cat_keywords = PIECE_KEYWORDS.get(cat, [])
                has_target_kw = any(kw in rtitle.lower() for kw in cat_keywords if len(kw) >= 3)
                if has_target_kw:
                    score += 15  # Bonus: sonuçta "saat/watch" veya "çanta/bag" geçiyor
                elif cat in ("watch", "bag", "sunglasses", "hat", "scarf", "accessory"):
                    score -= 20  # Aksesuar aramasında kategori kelimesi yoksa penaltı

                # v42: NON-PRODUCT URL penalty — arama/kategori sayfası ise cezalandır
                if not is_product_url(r.get("link", "")):
                    score -= 40

                return score

            seen = set()
            all_items = []

            # Lens results (per-piece crop + full exact = most reliable)
            for r in matched_lens:
                if r["link"] in seen: continue
                lnk = r.get("link", "")
                ttl = r.get("title", "")
                # Even _exact items must pass domain-level blocks (blogs, social media, news)
                if is_blocked(lnk):
                    print(f"    ⛔ BLOCKED EXACT: {ttl[:50]} | {lnk[:60]}")
                    continue
                # Blog/article detection for exact matches
                if r.get("_exact") and not is_fashion(lnk, ttl, r.get("source", "")):
                    print(f"    ⛔ NON-FASHION EXACT: {ttl[:50]} | {r.get('source','')}")
                    continue
                if not r.get("_exact"):
                    # v42: Category mismatch filter (çanta ararken bardak gelmesin)
                    if is_category_mismatch(ttl, cat):
                        print(f"    ⛔ CAT MISMATCH [{cat}]: {ttl[:50]}")
                        continue
                    if is_non_clothing_product(ttl):
                        print(f"    ⛔ NON-CLOTHING [{cat}]: {ttl[:50]}")
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

            # Sort: _exact items first (regardless of penalty), then by score
            all_items.sort(key=lambda x: (-int(x.get("_exact", False)), -x.get("_score", 0)))

            # 🇹🇷 TR-FIRST: Local results ALWAYS first, foreign only fills remaining slots
            local_items = [r for r in all_items if r.get("is_local")]
            foreign_items = [r for r in all_items if not r.get("is_local")]
            # Local exact first, then local rest, then foreign exact, then foreign rest
            local_exact = [r for r in local_items if r.get("_exact")]
            local_rest = [r for r in local_items if not r.get("_exact")]
            foreign_exact = [r for r in foreign_items if r.get("_exact")]
            foreign_rest = [r for r in foreign_items if not r.get("_exact")]
            all_items = local_exact + local_rest + foreign_exact + foreign_rest

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

            # Check if any top result has _exact flag (regardless of score penalties)
            has_exact_flag = any(r.get("_exact") for r in all_items[:5])
            has_ai_verified = any(r.get("ai_verified") for r in all_items[:3])

            if has_exact_flag or top_score >= 50:
                match_level = "exact"  # Lens exact match (same photo found online)
            elif has_ai_verified or (top_score >= 25 and (has_brand_match or has_text_match)):
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
                # Inject verified/sponsored badges
                badge = get_verified_badge(r.get("link", ""))
                if badge: r["_verified"] = badge

            results.append({
                "category": cat,
                "short_title": p.get("short_title", cat.title()),
                "color": p.get("color", ""),
                "style_type": p.get("style_type", ""),
                "brand": brand if brand != "?" else "",
                "visible_text": visible_text,
                "products": all_items[:8],
                "lens_count": len(matched_lens),
                "match_level": match_level,
                "crop_image": p.get("_crop_b64", ""),
            })
            # Record for popular searches
            if all_items and match_level in ("exact", "close"):
                record_popular_search(p, all_items[0])
            # Record analytics
            record_analytics("scan", {"category": cat, "brand": brand, "color": p.get("color", ""), "style_type": p.get("style_type", ""), "query": q_specific or q_generic, "match_level": match_level, "country": cc, "results_count": len(all_items)})

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

# ─── POPULAR SEARCHES (app-internal tracking) ───
POPULAR_SEARCHES = []  # [{query, img, title, brand, price, link, ts}]
POPULAR_MAX = 12

# ─── TREND ANALYTICS (B2B SaaS veri toplama) ───
TREND_ANALYTICS = []  # Her tarama kaydı
ANALYTICS_MAX = 5000  # Son 5000 taramayı tut

def record_analytics(event_type, data):
    """Her taramayı analitik olarak kaydet — gelecekte B2B dashboard için."""
    global TREND_ANALYTICS
    entry = {
        "ts": time.time(),
        "type": event_type,  # "scan", "search_piece", "manual", "combo"
        "category": data.get("category", ""),
        "brand": data.get("brand", ""),
        "color": data.get("color", ""),
        "style": data.get("style_type", ""),
        "query": data.get("query", ""),
        "match_level": data.get("match_level", ""),
        "country": data.get("country", "tr"),
        "results_count": data.get("results_count", 0),
    }
    TREND_ANALYTICS.insert(0, entry)
    if len(TREND_ANALYTICS) > ANALYTICS_MAX:
        TREND_ANALYTICS = TREND_ANALYTICS[:ANALYTICS_MAX]

def record_popular_search(piece_data, top_product):
    """Başarılı bir arama sonucunu popular searches'e kaydet."""
    global POPULAR_SEARCHES
    if not top_product or not top_product.get("link"): return
    query = piece_data.get("short_title", piece_data.get("category", ""))
    if not query: return
    
    # En iyi görseli bul (image > thumbnail)
    img = top_product.get("image", "") or top_product.get("thumbnail", "")
    if not img: return
    
    import time
    entry = {
        "query": query,
        "img": img,
        "title": top_product.get("title", "")[:40],
        "brand": top_product.get("brand", "") or top_product.get("source", ""),
        "price": top_product.get("price", ""),
        "link": top_product.get("link", ""),
        "ts": time.time(),
    }
    
    # Aynı query varsa güncelle, yoksa ekle
    POPULAR_SEARCHES = [p for p in POPULAR_SEARCHES if p["query"].lower() != query.lower()]
    POPULAR_SEARCHES.insert(0, entry)
    POPULAR_SEARCHES = POPULAR_SEARCHES[:POPULAR_MAX]

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
    search_patterns = [
        "/sr?", "/search?", "/search/", "/arama?", "/arama/",
        "?q=", "?query=", "?search=", "?keyword=",
        "/kategori/", "/category/", "/categories/",
        "/collection/", "/collections/", "/koleksiyon/",
        "/list/", "/listing/", "/browse/",
        "/c/", "/shop/", "/store/",  # generic category paths
        "?text=", "?term=", "&q=",
        "/women/", "/men/", "/kadin/", "/erkek/",  # category landing pages
    ]
    if any(sp in u for sp in search_patterns):
        # Exception: some stores use /shop/ or /c/ in product URLs too
        # Check if there's also a product identifier after
        has_product_id = bool(re.search(r'-p-\d|/dp/|/product/|/urun/|/p\d{4,}|productpage|/t/[A-Z]', u))
        if not has_product_id:
            return False

    # ✅ Bilinen mağazaların ürün URL pattern'leri
    product_patterns = {
        "trendyol.com": r'-p-\d+',
        "hepsiburada.com": r'-p[m]?-[A-Za-z0-9]+',
        "amazon.": r'/dp/[a-zA-Z0-9]+|/gp/product/',
        "n11.com": r'/urun/',
        "boyner.com": r'/urun/|/p/',
        "beymen.com": r'/urun/|/p/',
        "defacto.com": r'/\w+-\w+-\d+',
        "lcwaikiki.com": r'/tr-tr/.*\d',
        "zara.com": r'/tr/.+/p\d+|/p\d{4,}',
        "bershka.com": r'/tr/.+/\d+|/\d{8,}',
        "pullandbear.com": r'/tr/.+/\d+|/\d{8,}',
        "stradivarius.com": r'/tr/.+/\d+|/\d{8,}',
        "hm.com": r'/productpage\.|/p\.',
        "nike.com": r'/t/[A-Za-z]',
        "adidas.": r'/[A-Z]{2}\d{4}|/product/',
        "mango.com": r'/\d{8,}',
        "koton.com": r'/product/|/urun/',
        "flo.com": r'/urun/',
        "occasion.com.tr": r'/urun/|/product/',
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
                thumb = item.get("product_image", "") or item.get("thumbnail", "")

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
                        "img": enhance_thumbnail_url(thumb),
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
                    thumb = item.get("rich_snippet_image", "") or item.get("thumbnail", "")
                    if not lnk or not ttl or is_blocked(lnk): continue
                    if not is_fashion(lnk, ttl, src): continue
                    if not is_product_url(lnk):
                        print(f"  Trending SKIP organic (not product): {lnk[:80]}")
                        continue

                    lnk = localize_url(lnk, cc)
                    snippet = item.get("snippet", "")
                    pr_match = re.search(r'(\d[\d.,]+)\s*(?:TL|₺|\$|€|£)', snippet)
                    # Clean brand: extract domain name if raw URL
                    brand_name = get_brand(lnk, src)
                    if not brand_name:
                        try:
                            brand_name = urllib.parse.urlparse(lnk).netloc.replace("www.", "").split(".")[0].title()
                        except:
                            brand_name = src.split("/")[0] if "/" in src else src
                    products.append({
                        "title": ttl[:40],
                        "brand": brand_name,
                        "img": enhance_thumbnail_url(thumb) or "",
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
    
    # Brands data (always available)
    brands_data = BRAND_DATA.get(lang, BRAND_DATA.get("tr", []))
    section_brands = "Markalar" if lang == "tr" else "Brands"
    
    # Popular searches from app usage (high quality images, verified products)
    if POPULAR_SEARCHES:
        popular_products = [{
            "title": p["title"],
            "brand": p["brand"],
            "img": p["img"],
            "price": p["price"],
            "link": p["link"],
        } for p in POPULAR_SEARCHES]
        section_trending = "🔥 Popüler Aramalar" if lang == "tr" else "🔥 Popular Searches"
    else:
        popular_products = []
        section_trending = ""
    
    return {"success": True,
            "brands": [{"name": b["name"], "url": b.get("url", ""), "domain": b.get("domain", "")} for b in brands_data],
            "products": popular_products,
            "section_brands": section_brands, "section_trending": section_trending}

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

        piece_color = p.get("color", "")
        piece_style = p.get("style_type", "")

        def score_result(r, base_score=0):
            score = base_score
            combined = (r.get("title", "") + " " + r.get("link", "") + " " + r.get("source", "")).lower()
            rtitle = r.get("title", "")
            if r.get("_exact"): score += 50; r["ai_verified"] = True
            link = r.get("link", "")
            if link in shop_links and link in lens_links:
                score += 25; r["ai_verified"] = True
            if brand and brand != "?" and len(brand) > 2 and brand.lower() in combined: score += 8
            if visible_text and visible_text.lower() not in ["none", "?", ""]:
                for vt in visible_text.lower().replace(",", " ").split():
                    if len(vt) > 2 and vt in combined: score += 10; break
            if r.get("price"): score += 2
            if r.get("is_local"): score += 15
            # v42: COLOR PENALTY
            if detect_color_conflict(piece_color, rtitle):
                score -= 30
            # v42: SUB-TYPE PENALTY
            if detect_subtype_conflict(piece_style, rtitle, cat):
                score -= 25
            # v42: CATEGORY RELEVANCE
            cat_keywords = PIECE_KEYWORDS.get(cat, [])
            has_target_kw = any(kw in rtitle.lower() for kw in cat_keywords if len(kw) >= 3)
            if has_target_kw:
                score += 15
            elif cat in ("watch", "bag", "sunglasses", "hat", "scarf", "accessory"):
                score -= 20
            # v42: NON-PRODUCT URL penalty
            if not is_product_url(r.get("link", "")):
                score -= 40
            return score

        seen = set()
        all_items = []

        # Lens (highest priority)
        for r in all_lens:
            if r.get("link") and r["link"] not in seen:
                lnk = r.get("link", "")
                ttl = r.get("title", "")
                if is_blocked(lnk): continue
                if r.get("_exact") and not is_fashion(lnk, ttl, r.get("source", "")): continue
                if not r.get("_exact"):
                    if is_category_mismatch(ttl, cat): continue
                    if is_non_clothing_product(ttl): continue
                seen.add(lnk)
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

        all_items.sort(key=lambda x: (-int(x.get("_exact", False)), -x.get("_score", 0)))

        # 🇹🇷 TR-FIRST: Local results ALWAYS first
        local_items = [r for r in all_items if r.get("is_local")]
        foreign_items = [r for r in all_items if not r.get("is_local")]
        local_exact = [r for r in local_items if r.get("_exact")]
        local_rest = [r for r in local_items if not r.get("_exact")]
        foreign_exact = [r for r in foreign_items if r.get("_exact")]
        foreign_rest = [r for r in foreign_items if not r.get("_exact")]
        all_items = local_exact + local_rest + foreign_exact + foreign_rest

        # Match confidence
        top_score = all_items[0].get("_score", 0) if all_items else 0
        has_brand = any(brand and brand != "?" and brand.lower() in (r.get("title","")+" "+r.get("link","")).lower() for r in all_items[:3]) if brand and brand != "?" else False
        has_text = False
        if visible_text and visible_text.lower() not in ["none", "?", ""]:
            vt_words = [w for w in visible_text.lower().replace(",", " ").split() if len(w) > 2]
            for r in all_items[:3]:
                if any(w in r.get("title", "").lower() for w in vt_words): has_text = True; break

        has_exact_flag = any(r.get("_exact") for r in all_items[:5])
        has_ai_verified = any(r.get("ai_verified") for r in all_items[:3])

        if has_exact_flag or top_score >= 50: match_level = "exact"
        elif has_ai_verified or (top_score >= 25 and (has_brand or has_text)): match_level = "exact"
        elif top_score >= 15 or has_brand: match_level = "close"
        else: match_level = "similar"

        for j, r in enumerate(all_items[:3]):
            print(f"  #{j+1}: score={r.get('_score',0)} {r.get('title','')[:50]}")
        print(f"  match={match_level} brand={has_brand} text={has_text}")

        # Clean internal fields
        for r in all_items:
            for k in ["_score", "_priority", "_channel", "_src", "_exact"]: r.pop(k, None)
            # Inject verified badges
            badge = get_verified_badge(r.get("link", ""))
            if badge: r["_verified"] = badge

        crop_b64 = ""
        if crop_bytes:
            try:
                t_img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")
                t_img.thumbnail((128, 128))
                t_buf = io.BytesIO(); t_img.save(t_buf, format="JPEG", quality=75)
                crop_b64 = "data:image/jpeg;base64," + base64.b64encode(t_buf.getvalue()).decode()
            except: pass

        # Record for popular searches
        if all_items and match_level in ("exact", "close"):
            record_popular_search(p, all_items[0])
        # Record analytics
        record_analytics("search_piece", {"category": cat, "brand": brand, "color": p.get("color", ""), "style_type": p.get("style_type", ""), "query": queries[0][0] if queries else "", "match_level": match_level, "country": cc, "results_count": len(all_items)})

        return {
            "success": True,
            "piece": {
                "category": cat,
                "short_title": p.get("short_title", cat.title()),
                "brand": brand if brand != "?" else "",
                "visible_text": visible_text,
                "color": p.get("color", ""),
                "style_type": p.get("style_type", ""),
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

@app.get("/logo")
async def get_app_logo():
    for ext in ["png", "jpg", "jpeg"]:
        if os.path.exists(f"logo.{ext}"): return FileResponse(f"logo.{ext}")
    return Response(content=b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;", media_type="image/gif")

# ─── PWA: Manifest + Service Worker + Icons ───
@app.get("/manifest.json")
async def pwa_manifest():
    manifest = {
        "name": "Fitchy",
        "short_name": "Fitchy",
        "description": "Fotoğraftaki kıyafeti bul, anında satın al",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#05020a",
        "theme_color": "#05020a",
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

# ─── FITCHY APP ICON (embedded from custom logo) ───
_ICON_192_B64 = "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"

_ICON_512_B64 = "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xhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMcYYY4wxxhhjjDHGGGOMMX+e/X+Hhul/gnH4FQAAAABJRU5ErkJggg=="

@app.get("/icon-192.png")
async def icon_192():
    return Response(content=base64.b64decode(_ICON_192_B64), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})

@app.get("/icon-512.png")
async def icon_512():
    return Response(content=base64.b64decode(_ICON_512_B64), media_type="image/png",
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
            if r.status_code == 200 and len(r.content) > 500:
                ct = r.headers.get("content-type", "image/jpeg")
                if "image" in ct or "octet" in ct:
                    # Upscale small images for better display quality
                    try:
                        from PIL import Image
                        img_pil = Image.open(io.BytesIO(r.content))
                        w, h = img_pil.size
                        if w < 300 or h < 300:
                            # Upscale to at least 300px with LANCZOS
                            scale = max(300/w, 300/h)
                            new_w, new_h = int(w*scale), int(h*scale)
                            img_pil = img_pil.resize((new_w, new_h), Image.LANCZOS)
                            buf = io.BytesIO()
                            img_pil.save(buf, format="JPEG", quality=90)
                            enhanced = buf.getvalue()
                            IMG_CACHE[url_hash] = ("image/jpeg", enhanced)
                            return Response(content=enhanced, media_type="image/jpeg",
                                          headers={"Cache-Control": "public, max-age=86400"})
                    except:
                        pass
                    IMG_CACHE[url_hash] = (ct, r.content)
                    return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"Proxy img err: {e}")
    return Response(content=b"", status_code=404)

@app.get("/api/countries")
async def countries(): return {cc: {"name": cfg["name"], "currency": cfg["currency"]} for cc, cfg in COUNTRIES.items()}

# ─── URL THUMBNAIL EXTRACTION (Link Yapıştır) ───
@app.post("/api/url-thumbnail")
async def url_thumbnail(request: Request):
    """Extract og:image from a social media URL (TikTok, Instagram, Pinterest etc.)"""
    try:
        data = await request.json()
        url = data.get("url", "").strip()
        if not url:
            return {"success": False, "message": "URL gerekli"}
        
        # Validate URL
        if not url.startswith("http"):
            url = "https://" + url
        
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Fetch the page HTML to extract og:image
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "Accept": "text/html,application/xhtml+xml",
            })
            
            if r.status_code != 200:
                return {"success": False, "message": "Sayfa yüklenemedi"}
            
            html = r.text[:50000]  # Limit parse size
            
            # Extract og:image
            og_match = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if not og_match:
                og_match = re.search(r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']', html, re.I)
            
            if not og_match:
                # Try twitter:image
                og_match = re.search(r'<meta\s+(?:property|name)=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            
            if not og_match:
                return {"success": False, "message": "Görsel bulunamadı"}
            
            img_url = og_match.group(1)
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            
            # Download the image
            img_r = await client.get(img_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/*",
            })
            
            if img_r.status_code != 200 or len(img_r.content) < 1000:
                return {"success": False, "message": "Görsel indirilemedi"}
            
            # Resize if too large, convert to JPEG
            from PIL import Image
            img_pil = Image.open(io.BytesIO(img_r.content)).convert("RGB")
            img_pil.thumbnail((1200, 1200))
            buf = io.BytesIO()
            img_pil.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()
            
            print(f"URL THUMBNAIL OK: {url[:60]} → {img_url[:60]} ({len(b64)//1024}KB)")
            return {"success": True, "image_b64": b64}
    
    except Exception as e:
        print(f"URL THUMBNAIL ERR: {e}")
        return {"success": False, "message": "Link işlenemedi"}

# ─── OUTFIT COMBO: "Bunu Neyle Giyerim?" ───
@app.post("/api/outfit-combo")
async def outfit_combo(request: Request):
    """Claude'a parça bilgisi gönder, kombin önerisi + alışveriş linkleri al."""
    try:
        data = await request.json()
        category = data.get("category", "")
        title = data.get("title", "")
        brand = data.get("brand", "")
        color = data.get("color", "")
        style = data.get("style", "")
        cc = data.get("country", "tr")
        cfg = get_country_config(cc)
        lang = cfg["lang"]

        if not ANTHROPIC_API_KEY:
            return {"success": False, "message": "AI unavailable"}

        prompt = f"""The user found this item: {title} ({category}, {color}, {brand}, style: {style}).
Suggest 3 complementary pieces to complete an outfit. For each piece give:
- category (jacket/top/bottom/shoes/bag/accessory)
- description (2-3 words in {lang})
- search_query (4-6 word {lang} shopping query)
- why (1 sentence in {lang} explaining why it works)

Return ONLY a JSON array:
[{{"category":"bottom","description":"koyu gri kargo","search_query":"koyu gri kargo pantolon erkek","why":"..."}}]"""

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": CLAUDE_MODEL, "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]})
            text = resp.json().get("content", [{}])[0].get("text", "").strip()
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if not m:
                return {"success": False, "message": "AI parse error"}
            suggestions = json.loads(m.group())

        # Her öneri için shopping arama yap (parallel)
        async def search_suggestion(s):
            q = s.get("search_query", "")
            if not q: return {**s, "products": []}
            async with API_SEM:
                results = await asyncio.to_thread(_shop, q, cc, 3)
            return {**s, "products": results[:3]}

        combo_results = await asyncio.gather(*[search_suggestion(s) for s in suggestions[:3]])

        record_analytics("combo", {"category": category, "brand": brand, "color": color, "style_type": style, "country": cc, "results_count": sum(len(c.get("products", [])) for c in combo_results)})

        return {"success": True, "suggestions": combo_results}

    except Exception as e:
        print(f"COMBO ERR: {e}")
        return {"success": False, "message": str(e)}

# ─── TREND ANALYTICS DASHBOARD ───
@app.get("/api/analytics")
async def analytics_dashboard():
    """B2B trend analytics - kategori, marka, renk, stil trendleri."""
    if not TREND_ANALYTICS:
        return {"success": True, "total_scans": 0, "trends": {}}

    from collections import Counter
    now = time.time()

    # Son 24 saat
    recent = [a for a in TREND_ANALYTICS if now - a["ts"] < 86400]
    # Son 7 gün
    weekly = [a for a in TREND_ANALYTICS if now - a["ts"] < 604800]

    def top_counts(items, field, limit=10):
        c = Counter(a[field] for a in items if a.get(field))
        return [{"name": k, "count": v} for k, v in c.most_common(limit)]

    return {
        "success": True,
        "total_scans": len(TREND_ANALYTICS),
        "last_24h": len(recent),
        "last_7d": len(weekly),
        "trends": {
            "categories_24h": top_counts(recent, "category"),
            "brands_24h": top_counts(recent, "brand"),
            "colors_24h": top_counts(recent, "color"),
            "styles_24h": top_counts(recent, "style"),
            "categories_7d": top_counts(weekly, "category"),
            "brands_7d": top_counts(weekly, "brand"),
            "colors_7d": top_counts(weekly, "color"),
            "styles_7d": top_counts(weekly, "style"),
        },
        "match_rates": {
            "exact": len([a for a in recent if a.get("match_level") == "exact"]),
            "close": len([a for a in recent if a.get("match_level") == "close"]),
            "similar": len([a for a in recent if a.get("match_level") == "similar"]),
        },
        "countries": top_counts(weekly, "country", 5),
    }

# ─── SPONSORED / VERIFIED BRAND SYSTEM ───
# Marka sponsorluk + verified partner veritabanı
SPONSORED_DUPES = {
    # category → [{brand, query, label, cpc}]
    # Örnek: Lüks ceket tarandığında Koton muadilini göster
    "jacket": [
        {"brand": "Koton", "query": "koton deri ceket", "label": "Uygun Fiyatlı Alternatif", "badge": "fitchy. Sponsorlu Muadil", "cpc": 5.0},
        {"brand": "DeFacto", "query": "defacto ceket", "label": "Bütçe Dostu", "badge": "fitchy. Sponsorlu Muadil", "cpc": 3.0},
    ],
    "shoes": [
        {"brand": "FLO", "query": "flo sneaker", "label": "Uygun Fiyatlı Alternatif", "badge": "fitchy. Sponsorlu Muadil", "cpc": 4.0},
    ],
}

VERIFIED_BRANDS = {
    # domain → {badge, discount, priority_boost}
    "nike.com": {"badge": "Resmi Mağaza", "discount": "", "glow": "cyan"},
    "zara.com": {"badge": "Resmi Mağaza", "discount": "", "glow": "cyan"},
    "adidas.": {"badge": "Resmi Mağaza", "discount": "", "glow": "cyan"},
    "trendyol.": {"badge": "Güvenilir Satıcı", "discount": "", "glow": "green"},
    "hepsiburada.": {"badge": "Güvenilir Satıcı", "discount": "", "glow": "green"},
    "boyner.": {"badge": "Resmi Mağaza", "discount": "", "glow": "cyan"},
}

def get_verified_badge(link):
    """Ürün linkine göre verified badge bilgisi döndür."""
    link_lower = link.lower()
    for domain, info in VERIFIED_BRANDS.items():
        if domain in link_lower:
            return info
    return None

def get_sponsored_dupe(category, is_luxury=False):
    """Kategori + lüks marka ise sponsorlu muadil öner."""
    if not is_luxury: return None
    dupes = SPONSORED_DUPES.get(category, [])
    return dupes[0] if dupes else None

@app.post("/api/sponsored-search")
async def sponsored_search(request: Request):
    """Sponsorlu muadil araması — lüks ürün bulunduğunda tetiklenir."""
    try:
        data = await request.json()
        category = data.get("category", "")
        cc = data.get("country", "tr")

        dupes = SPONSORED_DUPES.get(category, [])
        if not dupes:
            return {"success": True, "sponsored": []}

        results = []
        for dupe in dupes[:2]:
            async with API_SEM:
                products = await asyncio.to_thread(_shop, dupe["query"], cc, 2)
            if products:
                p = products[0]
                p["_sponsored"] = True
                p["_sponsor_badge"] = dupe["badge"]
                p["_sponsor_brand"] = dupe["brand"]
                results.append(p)

        return {"success": True, "sponsored": results}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ─── FIT-CHECK: AI Outfit Roast & Score ───
FITCHECK_PROMPT = """Sen fitchy.'nin efsanevi, acımasız ama sevilen moda yargıcısın. Herkes senin yorumlarını SS alıp paylaşıyor çünkü çok komiksin.

Kullanıcı kendi kombinini çekip sana gönderdi. Sahne senin.

GÖREV:
1) Kombine 0-100 arası "Drip Score" ver (çoğu kişi 45-85 arası alır, 90+ efsane, 30- felaket)
2) Acımasız ama sevecen bir yorum yaz (4-5 cümle). Fotoğraftaki parçalara spesifik değin! "O ceket", "o ayakkabı", "o etek" gibi somut ol. Genel konuşma.
3) 2-3 somut öneri ver (hangi parça değişmeli, ne eklenmeli)

YORUM STİLİ ÖRNEKLERİ:
- "Deri ceket efsane duruyor, karanlık prens havasını yakalamışsın... AMA altındaki beyaz spor ayakkabılar bütün büyüyü bozuyor. Chelsea bot lazım buraya, Chelsea bot. Acil müdahale şart 🔥"
- "Oversize kazağı sevgiyle giymişsin belli ama altındaki dar pantolon 2017'den kalma gibi duruyor? Bol paça veya düz kesim dene, silüetin bambaşka olur 💀"
- "Old money havası var sayılır ama o kemer... o kemer her şeyi ele veriyor. Minimalist deri kemer tak, bu kombin Paris moda haftasına hazır 🇫🇷"

KRİTİK KURALLAR:
- TÜRKÇE YAZ! İngilizce kelime kullanımını minimumda tut. Sadece Türk gençlerinin gerçekten kullandığı 2-3 kelime olabilir (vibe, basic, fit gibi). Cümlelerin %90'ı Türkçe olsun.
- Emoji kullan ama abartma (cümle başına max 1)
- Kırıcı olma ama dürüst ol, sevgiyle dalga geç
- Fotoğrafta gördüğün GERÇEK parçalara referans ver (renk, tür, detay)
- Mevsim uygunluğu, renk uyumu, silüet dengesi, ayakkabı-kombin uyumu, aksesuar eksikliği değerlendir

YANIT FORMATI (sadece JSON, başka hiçbir şey yazma):
{"score": 72, "emoji": "💅", "roast": "...", "tips": ["...", "...", "..."]}"""

FITCHECK_PROMPT_EN = """You are fitchy.'s legendary, brutally honest but beloved fashion judge. People screenshot your reviews and share them on social media because you're hilarious.

The user sent their own outfit photo. The stage is yours.

TASK:
1) Give a "Drip Score" out of 100 (most people get 45-85, 90+ is legendary, 30- is a disaster)
2) Write a savage but loving roast (4-5 sentences). Reference SPECIFIC pieces! "That jacket", "those shoes", "that belt". Be specific. Write like it could go viral on Twitter/X.
3) Give 2-3 concrete tips (what to swap, what to add)

ROAST STYLE EXAMPLES:
- "The leather jacket is giving main character energy... BUT those white sneakers underneath? They're committing treason against the whole fit bestie. Chelsea boots. I said what I said. 🔥"
- "The oversized blazer is clearly worn with love but that skinny jean is giving 2017 flashbacks. Try wide-leg or straight fit, the silhouette would be *chef's kiss* 💀"

IMPORTANT:
- Gen-Z language: lots of emojis, "bestie", "slay", "ate", "serve", "it girl/boy", "main character" slang
- NEVER be mean-spirited, roast with love
- Reference ACTUAL pieces you see in the photo
- Evaluate season, color harmony, silhouette, shoe-outfit match, accessories

RESPONSE FORMAT (JSON only, nothing else):
{"score": 72, "emoji": "💅", "roast": "...", "tips": ["...", "...", "..."]}"""

@app.post("/api/fit-check")
async def fit_check(request: Request):
    try:
        body = await request.json()
        image_data = body.get("image", "")
        lang = body.get("lang", "tr")

        if not image_data:
            return {"success": False, "message": "No image"}

        # Strip data URI prefix
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        prompt = FITCHECK_PROMPT if lang == "tr" else FITCHECK_PROMPT_EN

        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 800,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            },
            timeout=30.0
        )
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "")

        # Parse JSON from response
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "message": str(e), "score": 50, "emoji": "🤔", "roast": "Fotoğrafı analiz edemedim ama eminim harika görünüyorsundur bestie! 💅", "tips": []}

# ─── VIRTUAL TRY-ON (Sanal Kabin) ───
VTON_STORE = {}

@app.post("/api/vton-save-body")
async def vton_save_body(request: Request):
    """Save user's body photo for VTON sessions"""
    try:
        body = await request.json()
        image = body.get("image", "")
        session_id = body.get("session", "default")
        if not image:
            return {"success": False, "message": "No image"}
        VTON_STORE[session_id] = image
        return {"success": True, "message": "Body photo saved"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/vton-tryon")
async def vton_tryon(request: Request):
    """Virtual try-on: analyze how garment would look on user"""
    try:
        body = await request.json()
        garment_title = body.get("title", "")
        garment_img = body.get("garment_img", "")
        session_id = body.get("session", "default")
        lang = body.get("lang", "tr")

        body_img = VTON_STORE.get(session_id, "")
        if not body_img:
            return {"success": False, "message": "No body photo saved", "need_body": True}

        # Strip data URI prefix for body
        body_b64 = body_img.split(",", 1)[-1] if "," in body_img else body_img

        # Strip data URI prefix for garment if base64
        garment_b64 = None
        if garment_img and garment_img.startswith("data:"):
            garment_b64 = garment_img.split(",", 1)[-1]

        vton_prompt_tr = f"""Sen bir sanal giyinme kabini asistanısın. Kullanıcının fotoğrafı ve "{garment_title}" adlı kıyafet var.

GÖREV: Bu kıyafetin kullanıcıda nasıl duracağını analiz et.

YANIT FORMATI (sadece JSON):
{{"fit_score": 85, "emoji": "💃", "analysis": "Bu parça sende harika durur! Vücut tipine çok uygun...", "size_tip": "M beden tam olur", "style_note": "Bu parçayı yüksek bel jean ile kombinle"}}"""

        vton_prompt_en = f"""You are a virtual fitting room assistant. The user's photo and a garment called "{garment_title}" are provided.

TASK: Analyze how this garment would look on the user.

RESPONSE FORMAT (JSON only):
{{"fit_score": 85, "emoji": "💃", "analysis": "This piece would look amazing on you! Great fit for your body type...", "size_tip": "Size M would be perfect", "style_note": "Pair this with high-waist jeans"}}"""

        prompt = vton_prompt_tr if lang == "tr" else vton_prompt_en

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": body_b64}},
            {"type": "text", "text": prompt}
        ]
        # If garment image is base64, include it
        if garment_b64:
            content.insert(1, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": garment_b64}})

        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 500,
                "messages": [{"role": "user", "content": content}]
            },
            timeout=30.0
        )
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "")
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/", response_class=HTMLResponse)
async def home(): return HTML_PAGE

# ─── FRONTEND (NEON GLASSMORPHISM UI v43) ───
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#05020a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="fitchy.">
<meta name="description" content="Find the outfit in the photo, instantly shop it">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>fitchy.</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.css" rel="stylesheet">
<style>
:root {
  --bg: #05020a;
  --card: rgba(25, 15, 45, 0.4);
  --border: rgba(255, 255, 255, 0.1);
  --border-glow: rgba(255, 32, 121, 0.4);
  --text: #f8f8f2;
  --muted: #8b859e;
  --accent: #ff2079;
  --cyan: #00e5ff;
  --purple: #4d00ff;
  --green: #22c55e;
  --red: #ff4466;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background-color:var(--bg);color:var(--text);font-family:'Outfit',sans-serif;display:flex;justify-content:center;min-height:100vh;overflow-x:hidden}
body::before{content:"";position:fixed;top:-10%;left:-20%;width:70%;height:70%;background:radial-gradient(circle,rgba(77,0,255,.15) 0%,transparent 60%);z-index:-1;pointer-events:none}
body::after{content:"";position:fixed;bottom:-10%;right:-20%;width:70%;height:70%;background:radial-gradient(circle,rgba(0,229,255,.1) 0%,transparent 60%);z-index:-1;pointer-events:none}
::-webkit-scrollbar{display:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
.app{width:100%;max-width:440px;min-height:100vh;padding-bottom:120px;position:relative}
.glass{background:var(--card);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:20px}
.text-gradient{background:linear-gradient(135deg,var(--accent),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent}

.btn-main{border:none;border-radius:16px;padding:16px;width:100%;font:700 16px 'Outfit',sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;transition:transform .2s,box-shadow .2s}
.btn-main:active{transform:scale(0.97)}
.btn-magenta{background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;box-shadow:0 4px 20px rgba(255,32,121,.4)}
.btn-cyan{background:linear-gradient(135deg,var(--cyan),#00b3ff);color:#000;box-shadow:0 4px 20px rgba(0,229,255,.3)}
.btn-outline{background:rgba(0,229,255,.05);color:var(--cyan);border:1px solid rgba(0,229,255,.3) !important;box-shadow:inset 0 0 15px rgba(0,229,255,.05)}

.hero{border-radius:20px;overflow:hidden;margin-bottom:16px;position:relative;transition:border-color .3s;border:1px solid var(--border)}
.hero img{width:100%;height:260px;object-fit:cover;display:block;border-bottom:1px solid var(--border)}
.hero .badge{position:absolute;top:12px;left:12px;background:var(--cyan);color:#000;font-size:11px;font-weight:800;padding:6px 12px;border-radius:8px;box-shadow:0 4px 15px rgba(0,229,255,.4);text-transform:uppercase;letter-spacing:.5px}
.hero .info{padding:16px}
.hero .t{font-size:16px;font-weight:600;line-height:1.3}
.hero .s{font-size:12px;color:var(--cyan);margin-top:4px;font-weight:500}
.hero .row{display:flex;align-items:center;justify-content:space-between;margin-top:12px}
.hero .price{font-size:22px;font-weight:800;color:#fff}
.hero .btn{background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.2);border-radius:10px;padding:8px 16px;font:700 13px 'Outfit',sans-serif;cursor:pointer;backdrop-filter:blur(5px)}

.scroll{display:flex;gap:12px;overflow-x:auto;padding-bottom:10px;margin-top:10px}
.card{flex-shrink:0;width:140px;border-radius:16px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text);transition:border-color .2s,transform .15s;position:relative}
.card:active{transform:scale(0.97)}
.card img{width:140px;height:140px;object-fit:cover;display:block;border-bottom:1px solid var(--border)}
.card .ci{padding:10px}
.card .cn{font-size:11px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .cs{font-size:10px;color:var(--cyan);margin-top:2px;font-weight:500}
.card .cp{font-size:14px;font-weight:700;color:#fff;margin-top:4px}

.piece-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0}
.piece-card{border-radius:16px;border:1px solid var(--border);overflow:hidden;cursor:pointer;transition:all .2s;animation:fadeUp .35s ease both}
.piece-card:hover,.piece-card:active{border-color:var(--accent);box-shadow:0 0 20px rgba(255,32,121,.2);transform:translateY(-3px)}
.piece-card img{width:100%;height:160px;object-fit:cover;display:block}
.piece-card .pc-info{padding:12px}
.piece-card .pc-cat{font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}
.piece-card .pc-brand{font-size:10px;font-weight:700;color:#000;background:var(--cyan);padding:2px 8px;border-radius:4px;margin-top:6px;display:inline-block}
.piece-card .pc-text{font-size:10px;color:var(--accent);font-style:italic;margin-top:4px}
.piece-card .pc-noimg{width:100%;height:160px;display:flex;align-items:center;justify-content:center;font-size:48px;background:rgba(255,255,255,.02)}

.bnav{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);width:calc(100% - 40px);max-width:400px;background:rgba(10,5,20,.7);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid var(--border);border-radius:30px;display:flex;padding:12px 20px;justify-content:space-around;box-shadow:0 20px 40px rgba(0,0,0,.8);z-index:50}
.bnav-item{display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;opacity:.5;transition:all .2s}
.bnav-item.active{opacity:1}
.bnav-item .icon{font-size:22px;color:#fff}
.bnav-item.active .icon{color:var(--accent);text-shadow:0 0 10px var(--border-glow)}
.bnav-item .lbl{font-size:10px;font-weight:600;color:#fff}

.crop-container{position:relative;width:100%;max-height:400px;margin:16px 0;border-radius:20px;overflow:hidden;background:#000;border:1px solid var(--border)}
.crop-container img{display:block;max-width:100%}
input[type="text"]{width:100%;padding:16px;border-radius:16px;border:1px solid var(--border);background:rgba(0,0,0,.4);color:#fff;font:400 15px 'Outfit',sans-serif;margin:12px 0;outline:none;transition:border-color .2s}
input[type="text"]:focus{border-color:var(--cyan);box-shadow:0 0 15px rgba(0,229,255,.2)}

.loader-orb{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,var(--cyan),var(--accent));animation:spin 1s linear infinite;margin-right:16px;box-shadow:0 0 20px var(--border-glow)}

/* Trending section */
.trend-scroll{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px}
.trend-card{flex-shrink:0;width:130px;border-radius:16px;border:1px solid var(--border);overflow:hidden;color:var(--text);cursor:pointer;transition:all .2s}
.trend-card:hover{border-color:var(--accent)}
.trend-card:active{transform:scale(0.97);border-color:var(--cyan)}
.trend-card .tc-img{width:130px;height:170px;overflow:hidden;position:relative}
.trend-card .tc-img img{width:100%;height:100%;object-fit:cover;display:block}
.trend-card .tc-info{padding:10px}
.trend-card .tc-title{font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trend-card .tc-brand{font-size:9px;color:var(--cyan);margin-top:2px}
.no-img{background:linear-gradient(135deg,#1a1a2e,#16213e)!important}
.no-img img{display:none!important}

/* Link paste */
.link-area{background:transparent;border:1.5px solid var(--border);border-radius:16px;padding:14px 20px;display:flex;align-items:center;gap:14px;cursor:pointer;margin-bottom:8px;transition:border-color .2s}
.link-area:active{border-color:var(--accent)}

/* Social Profile */
.profile-header{text-align:center;padding:16px 0 20px}
.profile-avatar{width:88px;height:88px;border-radius:50%;border:3px solid var(--accent);box-shadow:0 0 30px rgba(255,32,121,.3);margin:0 auto 14px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,rgba(255,32,121,.15),rgba(77,0,255,.15));font-size:38px;cursor:pointer;position:relative;overflow:hidden}
.profile-avatar img{width:100%;height:100%;object-fit:cover}
.profile-name{font-size:20px;font-weight:800;letter-spacing:-.3px}
.profile-handle{font-size:13px;color:var(--cyan);font-weight:600;margin-top:2px}
.profile-bio{font-size:12px;color:var(--muted);margin-top:8px;max-width:260px;margin-left:auto;margin-right:auto;line-height:1.4}
.profile-stats{display:flex;justify-content:center;gap:28px;margin-top:16px}
.profile-stat{text-align:center}
.profile-stat .num{font-size:18px;font-weight:800}
.profile-stat .lbl{font-size:10px;color:var(--muted);font-weight:600;margin-top:2px}
.profile-actions{display:flex;gap:10px;justify-content:center;margin-top:16px}
.profile-btn{padding:10px 20px;border-radius:14px;font:700 12px 'Outfit',sans-serif;cursor:pointer;border:none;letter-spacing:.3px;transition:all .2s}
.profile-btn.primary{background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;box-shadow:0 4px 20px rgba(255,32,121,.3)}
.profile-btn.secondary{background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--border)}
.folder-tabs{display:flex;gap:8px;padding:0 4px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;margin-top:20px;padding-bottom:4px}
.folder-tabs::-webkit-scrollbar{display:none}
.folder-tab{white-space:nowrap;padding:8px 16px;border-radius:12px;font:600 12px 'Outfit',sans-serif;cursor:pointer;background:rgba(255,255,255,.04);border:1px solid var(--border);color:var(--muted);transition:all .2s;flex-shrink:0}
.folder-tab.active{background:linear-gradient(135deg,rgba(255,32,121,.15),rgba(77,0,255,.1));border-color:rgba(255,32,121,.4);color:var(--accent)}
.folder-tab .cnt{font-size:10px;opacity:.6;margin-left:4px}
.item-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.item-card{border-radius:16px;overflow:hidden;position:relative;background:var(--card);border:1px solid var(--border);transition:border-color .2s}
.item-card img{width:100%;height:160px;object-fit:cover;border-bottom:1px solid var(--border)}
.item-card .info{padding:10px}
.item-card .title{font-size:11px;font-weight:600;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}
.item-card .brand{font-size:10px;color:var(--cyan)}
.item-card .price{font-weight:800;font-size:14px;margin-top:6px}
.item-badge{position:absolute;top:8px;left:8px;display:flex;gap:4px}
.item-badge span{padding:3px 8px;border-radius:8px;font-size:9px;font-weight:700;backdrop-filter:blur(8px)}
.lock-badge{background:rgba(0,0,0,.7);color:var(--muted)}
.refitch-badge{background:rgba(255,32,121,.8);color:#fff}
.refitch-btn{position:absolute;bottom:60px;right:8px;background:rgba(0,0,0,.8);border:1px solid rgba(255,32,121,.3);color:var(--accent);padding:6px 10px;border-radius:10px;font:700 10px 'Outfit',sans-serif;cursor:pointer;backdrop-filter:blur(4px);display:flex;align-items:center;gap:4px;transition:all .2s}
.refitch-btn:active{background:var(--accent);color:#fff}
.vis-toggle{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.7);padding:6px;border-radius:50%;cursor:pointer;font-size:14px;line-height:1;backdrop-filter:blur(4px)}

/* Fit-Check Score */
.fitcheck-result{text-align:center;padding:24px 0}
.drip-score{font-size:72px;font-weight:900;letter-spacing:-3px;line-height:1}
.drip-label{font-size:13px;font-weight:700;color:var(--muted);margin-top:4px;letter-spacing:2px;text-transform:uppercase}
.drip-bar{width:100%;height:8px;border-radius:4px;background:rgba(255,255,255,.06);margin:16px 0;overflow:hidden}
.drip-bar-fill{height:100%;border-radius:4px;transition:width 1.5s cubic-bezier(.22,1,.36,1)}
.roast-text{font-size:15px;line-height:1.6;color:var(--text);margin:16px 0;padding:0 8px}
.tip-card{padding:10px 14px;border-radius:12px;background:rgba(0,229,255,.06);border:1px solid rgba(0,229,255,.15);font-size:12px;color:var(--cyan);margin:8px 0;text-align:left}
.share-fitcheck{display:inline-flex;align-items:center;gap:8px;padding:12px 24px;border-radius:16px;background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;font:700 13px 'Outfit',sans-serif;border:none;cursor:pointer;margin:12px 6px 0;box-shadow:0 4px 20px rgba(255,32,121,.3)}
.vton-btn{display:inline-flex;align-items:center;gap:4px;padding:6px 12px;border-radius:10px;background:linear-gradient(135deg,rgba(0,229,255,.15),rgba(77,0,255,.1));border:1px solid rgba(0,229,255,.25);color:var(--cyan);font:700 10px 'Outfit',sans-serif;cursor:pointer;margin-top:6px;transition:all .2s}
.vton-btn:active{background:var(--cyan);color:#000}

/* VTON Modal */
.vton-modal{position:fixed;inset:0;z-index:1000;background:rgba(5,2,10,.95);backdrop-filter:blur(30px);display:none;flex-direction:column;align-items:center;justify-content:center;padding:20px}
.vton-modal.show{display:flex}
</style>
</head>
<body>
<div class="app">
  <div id="home" style="padding:0 20px">
    <div style="padding-top:50px;padding-bottom:30px">
      <div style="display:flex;justify-content:center;margin-bottom:16px">
        <img src="data:image/png;base64,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" style="width:120px;height:120px;border-radius:0;background:transparent;box-shadow:none;object-fit:contain">
        <div id="fallback-logo" style="display:none;align-items:center;justify-content:center;width:120px;height:120px;border-radius:0;background:transparent;border:none;color:var(--accent);font-weight:800;font-size:48px;box-shadow:none">f</div>
      </div>
      <h1 id="heroTitle" style="font-size:36px;line-height:1.1;letter-spacing:-.5px;text-align:center"></h1>
      <p id="heroSub" style="font-size:15px;color:var(--muted);margin-top:14px;line-height:1.5;text-align:center"></p>
    </div>

    <button class="btn-main btn-magenta" onclick="document.getElementById('fi').click()" style="font-size:17px;padding:18px;border-radius:20px" id="uploadBtn"></button>
    <input type="file" id="fi" accept="image/jpeg,image/png,image/webp" style="display:none">
    
    <div id="trustBadge" style="text-align:center;margin-top:14px;font-size:12px;color:var(--muted);font-weight:500;letter-spacing:.3px"></div>

    <div style="display:flex;align-items:center;gap:10px;margin:16px 0 10px"><div style="flex:1;height:1px;background:var(--border)"></div><span style="font-size:10px;color:var(--muted);font-weight:600">VEYA</span><div style="flex:1;height:1px;background:var(--border)"></div></div>

    <div onclick="showLinkInput()" id="linkPasteBtn" class="link-area">
      <div style="font-size:20px">&#x1F517;</div>
      <div>
        <div id="linkTitle" style="font-size:13px;font-weight:600;color:var(--muted)"></div>
        <div style="font-size:10px;color:var(--muted);opacity:.6">TikTok, Instagram, Pinterest</div>
      </div>
    </div>
    <div id="linkInputArea" style="display:none;margin-bottom:12px">
      <div style="display:flex;gap:8px">
        <input type="text" id="linkInput" placeholder="https://..." style="flex:1;margin:0;padding:14px">
        <button onclick="scanFromLink()" style="background:linear-gradient(135deg,var(--cyan),#00b3ff);color:#000;border:none;border-radius:16px;padding:14px 20px;font:700 13px 'Outfit',sans-serif;cursor:pointer;white-space:nowrap" id="linkGoBtn">Tarat</button>
      </div>
    </div>

    <!-- FIT-CHECK BUTTON -->
    <div onclick="startFitCheck()" style="margin:16px 0 0;padding:18px 20px;border-radius:20px;background:linear-gradient(135deg,rgba(255,32,121,.12),rgba(77,0,255,.08));border:1px solid rgba(255,32,121,.25);cursor:pointer;display:flex;align-items:center;gap:14px;transition:all .2s;box-shadow:0 4px 20px rgba(255,32,121,.08)" id="fitCheckCard">
      <div style="width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,var(--accent),var(--purple));display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;box-shadow:0 4px 16px rgba(255,32,121,.3)">🔥</div>
      <div style="flex:1">
        <div id="fitCheckTitle" style="font-size:15px;font-weight:800;color:var(--text)"></div>
        <div id="fitCheckSub" style="font-size:12px;color:var(--muted);margin-top:3px"></div>
      </div>
      <div style="font-size:18px;color:var(--accent)">→</div>
    </div>
    <input type="file" id="fitCheckInput" accept="image/*" capture="environment" style="display:none" onchange="handleFitCheck(event)">

    <div id="trendingSection" style="margin-top:24px;padding-bottom:20px"></div>
  </div>

  <div id="rScreen" style="display:none">
    <div style="position:sticky;top:0;z-index:40;background:rgba(3,1,8,.7);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);padding:16px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
      <div onclick="goHome()" style="cursor:pointer;color:var(--muted);font-size:14px;font-weight:600" id="backBtn"></div>
      <div style="font-size:22px;font-weight:900;letter-spacing:1px" class="text-gradient">fitchy.</div>
      <div style="width:40px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <div class="glass" style="border-radius:16px;overflow:hidden;margin:16px 0;position:relative;padding:4px">
        <img id="prev" src="" style="width:100%;display:block;object-fit:cover;max-height:260px;border-radius:12px">
        <div style="position:absolute;inset:0;background:linear-gradient(to top,var(--bg) 0%,transparent 40%);pointer-events:none;border-radius:16px"></div>
      </div>
      <div id="actionBtns" style="display:flex;flex-direction:column;gap:12px">
        <button class="btn-main btn-magenta" onclick="autoScan()" id="btnAuto"></button>
        <button class="btn-main glass" onclick="startManual()" style="color:var(--text);border:1px solid var(--border)" id="btnManual"></button>
      </div>
      <div id="cropMode" style="display:none">
        <p id="cropHint" style="font-size:14px;color:var(--cyan);font-weight:600;margin-bottom:12px;text-align:center"></p>
        <div class="crop-container"><img id="cropImg" src=""></div>
        <input id="manualQ" type="text">
        <button class="btn-main btn-cyan" onclick="cropAndSearch()" id="btnFind"></button>
        <button class="btn-main" onclick="cancelManual()" style="margin-top:8px;background:transparent;color:var(--muted);border:1px solid var(--border)" id="btnCancel"></button>
      </div>
      <div id="piecePicker" style="display:none"></div>
      <div id="ld" style="display:none;margin-top:20px"></div>
      <div id="err" style="display:none"></div>
      <div id="res" style="display:none;margin-top:16px"></div>
    </div>
  </div>

  <!-- VTON Modal -->
  <div class="vton-modal" id="vtonModal">
    <div style="width:100%;max-width:400px;text-align:center">
      <div style="font-size:28px;margin-bottom:8px">🪞</div>
      <div class="text-gradient" style="font-size:22px;font-weight:800;margin-bottom:6px" id="vtonModalTitle">Sanal Kabin</div>
      <div id="vtonModalBody" style="color:var(--muted);font-size:13px;margin-bottom:20px"></div>
      <div id="vtonModalContent"></div>
      <button onclick="closeVton()" style="margin-top:16px;background:rgba(255,255,255,.06);border:1px solid var(--border);color:var(--text);padding:12px 28px;border-radius:14px;font:600 13px 'Outfit',sans-serif;cursor:pointer">✕ Kapat</button>
    </div>
  </div>
  <input type="file" id="vtonBodyInput" accept="image/*" capture="environment" style="display:none" onchange="handleVtonBody(event)">

  <div class="bnav">
    <div class="bnav-item active" onclick="goHome()"><div class="icon">✧</div><div id="navHome" class="lbl"></div></div>
    <div class="bnav-item" onclick="showFavs()"><div class="icon">♡</div><div id="navFav" class="lbl"></div></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.js"></script>
<script>
var IC={hat:"\uD83E\uDDE2",sunglasses:"\uD83D\uDD76\uFE0F",top:"\uD83D\uDC55",jacket:"\uD83E\uDDE5",bag:"\uD83D\uDC5C",accessory:"\uD83D\uDC8D",watch:"\u231A",bottom:"\uD83D\uDC56",dress:"\uD83D\uDC57",shoes:"\uD83D\uDC5F",scarf:"\uD83E\uDDE3"};
var cF=null,cPrev=null,cropper=null,CC='us';
var L={
  tr:{heroTitle:'G\u00f6rseldeki kombini<br><span class="text-gradient">birebir</span> bul.',heroSub:'Instagram\'da, TikTok\'ta veya sokakta k\u0131skand\u0131\u011f\u0131n o kombini an\u0131nda bul.<br>Ekran g\u00f6r\u00fcnt\u00fcs\u00fcn\u00fc y\u00fckle, gerisini fitchy\'ye b\u0131rak.',upload:'\u2728 Kombini Tarat',auto:'\u2728 Ak\u0131ll\u0131 Tarama (\u00d6nerilen)',manual:'\u2702\uFE0F Sadece Bir Par\u00e7a Se\u00e7',trustBadge:'\uD83D\uDD0D ZARA, NIKE, TRENDYOL ve 500+ markada aran\u0131yor...',trendTitle:'\uD83D\uDD25 \u015eu An Trend Olanlar',back:'\u2190 Geri',cropHint:'\uD83D\uDC47 Aramak istedi\u011fin par\u00e7ay\u0131 \u00e7er\u00e7evele',manualPh:'Ne ar\u0131yorsun? (Opsiyonel)',find:'\uD83D\uDD0D Par\u00e7ay\u0131 Bul',cancel:'\u0130ptal',loading:'Siber a\u011fa ba\u011flan\u0131l\u0131yor...',loadingManual:'AI e\u015fle\u015ftiriyor...',noResult:'Par\u00e7a tespit edilemedi.',noProd:'Bu par\u00e7a i\u00e7in e\u015fle\u015fme bulunamad\u0131.',retry:'\u2702\uFE0F Manuel Se\u00e7imi Dene',another:'\u2702\uFE0F Ba\u015fka Par\u00e7a Se\u00e7',selected:'Se\u00e7imin',lensMatch:'g\u00f6rsel e\u015fle\u015fme',recommended:'\u2728 \u00d6nerilen',lensLabel:'\uD83C\uDFAF AI E\u015fle\u015fmesi',goStore:'Sat\u0131n Al \u2197',noPrice:'Fiyat\u0131 G\u00f6r',alts:'\uD83D\uDCB8 Alternatifler \u2192',navHome:'Ke\u015ffet',navFav:'Dolap\u0131m',aiMatch:'AI Onayl\u0131',matchExact:'\u2705 Birebir E\u015fle\u015fme',matchClose:'\uD83D\uDD25 Y\u00fcksek Benzerlik',matchSimilar:'\u2728 Benzer \u00dcr\u00fcnler',step_detect:'K\u0131yafetler tespit ediliyor...',step_bg:'G\u00f6rsel haz\u0131rlan\u0131yor...',step_lens:'Ma\u011fazalar taran\u0131yor...',step_ai:'AI \u00fcr\u00fcnleri k\u0131yasl\u0131yor...',step_verify:'E\u015fle\u015fmeler do\u011frulan\u0131yor...',step_done:'Sonu\u00e7lar haz\u0131r!',piecesFound:'par\u00e7a bulundu',pickPiece:'Aramak istedi\u011fin par\u00e7aya dokun',searchingPiece:'\u00dcr\u00fcn aran\u0131yor...',backToPieces:'\u2190 Di\u011fer Par\u00e7alar',noDetect:'Par\u00e7a bulunamad\u0131. Manuel se\u00e7imi deneyin.',loadMore:'A\u011f\u0131 Geni\u015flet \u2193',loadingMore:'Taran\u0131yor...',linkPaste:'Link Yap\u0131\u015ft\u0131r & Tarat',linkGo:'Tarat',linkLoading:'Link taran\u0131yor...',comboBtn:'\u2728 Bunu Neyle Giyerim?',comboLoading:'AI kombin \u00f6nerisi haz\u0131rl\u0131yor...',comboTitle:'\uD83D\uDC57 AI Kombin \u00d6nerisi',verified:'Resmi Ma\u011faza',sponsored:'Sponsorlu Muadil',fitCheck:'\uD83D\uDD25 AI Fit-Check',fitCheckSub:'Kombinin ka\u00e7 puan? AI yargilat!',fitCheckLoading:'AI stilist inceliyor...',fitCheckScore:'Drip Score',fitCheckTips:'\uD83D\uDCA1 \u00d6neriler',fitCheckShare:'Sonucu Payla\u015f',fitCheckAnother:'Ba\u015fka Kombin Dene',vtonBtn:'\u2728 \u00dczerimde G\u00f6r',vtonSaveBody:'Tam boy foto\u011fraf\u0131n\u0131 y\u00fckle',vtonLoading:'Sanal kabin haz\u0131rlan\u0131yor...',vtonResult:'AI Fit Analizi',vtonNoBody:'\u00d6nce foto\u011fraf\u0131n\u0131 y\u00fckle'},
  en:{heroTitle:'Find the outfit<br>in the photo, <span class="text-gradient">exactly</span>.',heroSub:'Spot a fire outfit on Instagram, TikTok or IRL?<br>Screenshot it, let fitchy find every piece.',upload:'\u2728 Scan Outfit',auto:'\u2728 Auto Scan (Recommended)',manual:'\u2702\uFE0F Select Manually',trustBadge:'\uD83D\uDD0D Searching ZARA, NIKE, H&M and 500+ brands...',trendTitle:'\uD83D\uDD25 Trending Now',back:'\u2190 Back',cropHint:'\uD83D\uDC47 Frame the piece you want to search',manualPh:'What are you looking for?',find:'\uD83D\uDD0D Find Piece',cancel:'Cancel',loading:'Analyzing image...',loadingManual:'AI matching...',noResult:'No pieces detected.',noProd:'No exact match found.',retry:'\u2702\uFE0F Try Manual Selection',another:'\u2702\uFE0F Select Another Piece',selected:'Your Selection',lensMatch:'visual match',recommended:'\u2728 Recommended',lensLabel:'\uD83C\uDFAF AI Match',goStore:'Shop \u2197',noPrice:'Check Price',alts:'\uD83D\uDCB8 Alternatives \u2192',navHome:'Explore',navFav:'Closet',aiMatch:'AI Verified',matchExact:'\u2705 Exact Match',matchClose:'\uD83D\uDD25 Close Match',matchSimilar:'\u2728 Similar Items',step_detect:'Detecting garments...',step_lens:'Scanning global stores...',step_match:'Matching products...',step_done:'Ready!',step_bg:'Preparing image...',step_search:'Scanning...',step_ai:'AI comparing details...',step_verify:'Verifying matches...',piecesFound:'pieces found',pickPiece:'Tap a piece to search',searchingPiece:'Searching...',backToPieces:'\u2190 Other Pieces',noDetect:'No pieces found. Try manual selection.',loadMore:'Expand Search \u2193',loadingMore:'Scanning...',linkPaste:'Paste Link & Scan',linkGo:'Scan',linkLoading:'Scanning link...',comboBtn:'\u2728 What Goes With This?',comboLoading:'AI building outfit...',comboTitle:'\uD83D\uDC57 AI Outfit Suggestion',verified:'Official Store',sponsored:'Sponsored Dupe',fitCheck:'\uD83D\uDD25 Fit-Check',fitCheckSub:'Rate your outfit!',fitCheckLoading:'AI stylist analyzing...',fitCheckScore:'Drip Score',fitCheckTips:'\uD83D\uDCA1 Tips',fitCheckShare:'Share Result',fitCheckAnother:'Try Another Outfit',vtonBtn:'\u2728 Try On Me',vtonSaveBody:'Upload your full-body photo',vtonLoading:'Virtual fitting room loading...',vtonResult:'AI Fit Analysis',vtonNoBody:'Upload your photo first'}
};
var CC_LANG={tr:'tr',us:'en',uk:'en',de:'en',fr:'en',sa:'en',ae:'en',eg:'en'};
function t(key){var lg=CC_LANG[CC]||'en';return(L[lg]||L.en)[key]||(L.en)[key]||key}
function detectCountry(){
  var tz=(Intl.DateTimeFormat().resolvedOptions().timeZone||'').toLowerCase();
  if(tz.indexOf('istanbul')>-1||(navigator.language||'').toLowerCase().startsWith('tr'))return 'tr';
  return 'us';
}
CC=detectCountry();

function applyLang(){
  document.getElementById('heroTitle').innerHTML=t('heroTitle');
  document.getElementById('heroSub').innerHTML=t('heroSub');
  document.getElementById('uploadBtn').innerHTML=t('upload');
  document.getElementById('trustBadge').textContent=t('trustBadge');
  document.getElementById('btnAuto').innerHTML=t('auto');
  document.getElementById('btnManual').innerHTML=t('manual');
  document.getElementById('backBtn').textContent=t('back');
  document.getElementById('cropHint').textContent=t('cropHint');
  document.getElementById('manualQ').placeholder=t('manualPh');
  document.getElementById('btnFind').innerHTML=t('find');
  document.getElementById('btnCancel').innerHTML=t('cancel');
  document.getElementById('navHome').textContent=t('navHome');
  document.getElementById('navFav').textContent=t('navFav');
  document.getElementById('linkTitle').textContent=t('linkPaste');
  document.getElementById('linkGoBtn').textContent=t('linkGo');
  document.getElementById('fitCheckTitle').textContent=t('fitCheck');
  document.getElementById('fitCheckSub').textContent=t('fitCheckSub');
  loadTrending();
}
applyLang();
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){})}
function getCC(){return CC}
// Fix relative Google thumbnail URLs
function _fixThumb(u){if(!u)return u;if((u.indexOf('images?q=tbn:')===0||u.indexOf('/images?q=tbn:')===0)&&u.indexOf('gstatic.com')===-1)return 'https://encrypted-tbn0.gstatic.com/'+u.replace(/^\//,'');return u}

document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});
document.getElementById('linkInput').addEventListener('keyup',function(e){if(e.key==='Enter')scanFromLink()});

// ─── STATIC DEMO IMAGES (fallback when no popular searches) ───
var DEMO_IMGS=['https://images.unsplash.com/photo-1521223890158-f9f7c3d5d504?w=400&h=500&fit=crop&crop=top','https://images.unsplash.com/photo-1552374196-1ab2a1c593e8?w=400&h=500&fit=crop&crop=top','https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=400&h=500&fit=crop&crop=top','https://images.unsplash.com/photo-1507680434567-5739c80be1ac?w=400&h=500&fit=crop&crop=top'];
var DEMO_LABELS_TR=['Deri Ceket Kombin','Viral Sneaker','Sokak Stili','\u015e\u0131k Kombin'];
var DEMO_LABELS_EN=['Leather Jacket Fit','Viral Sneakers','Street Style','Chic Outfit'];

function loadTrending(){
  var ts=document.getElementById('trendingSection');
  fetch('/api/trending?country='+getCC()).then(function(r){return r.json()}).then(function(d){
    if(!d.success){renderDemoTrend(ts);return}
    var h='';
    // Popular product cards (from real searches)
    if(d.products&&d.products.length){
      h+='<div style="font-size:15px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px">'+d.section_trending+'</div>';
      h+='<div class="trend-scroll">';
      for(var i=0;i<d.products.length;i++){var p=d.products[i];
        var tImg=_fixThumb(p.img);
        h+='<div onclick="demoScan(\''+encodeURIComponent(tImg)+'\')" class="glass trend-card">';
        h+='<div class="tc-img"><img src="'+tImg+'" data-orig="'+tImg+'" onerror="imgErr(this)"></div>';
        h+='<div class="tc-info"><div class="tc-title">'+p.title+'</div><div class="tc-brand">'+(p.brand||'')+'</div></div></div>';
      }
      h+='</div>';
    }else{
      // Fall back to static demo images
      renderDemoTrend(ts);return;
    }
    ts.innerHTML=h;
  }).catch(function(){renderDemoTrend(ts)})
}

function renderDemoTrend(ts){
  var labels=(CC_LANG[CC]==='tr')?DEMO_LABELS_TR:DEMO_LABELS_EN;
  var h='<div style="font-size:15px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px">'+t('trendTitle')+'</div>';
  h+='<div class="trend-scroll">';
  for(var i=0;i<DEMO_IMGS.length;i++){
    h+='<div onclick="demoScanUrl(\''+DEMO_IMGS[i]+'\')" class="glass trend-card">';
    h+='<div class="tc-img"><img src="'+DEMO_IMGS[i]+'" onerror="imgErr(this)"></div>';
    h+='<div class="tc-info"><div class="tc-title">'+labels[i]+'</div><div class="tc-brand" style="color:var(--accent);font-size:10px">\u2728 '+t('auto')+' \u2197</div></div></div>';
  }
  h+='</div>';
  ts.innerHTML=h;
}

// ─── IMAGE ERROR CASCADE: direct → proxy → placeholder ───
function imgErr(el){
  el.onerror=null;
  var orig=el.getAttribute('data-orig')||el.src;
  var tried=el.getAttribute('data-tried')||'0';
  // Fix relative Google thumbnail URLs
  if(tried==='0'&&orig&&(orig.indexOf('images?q=tbn:')>-1||orig.match(/^images\?/))&&orig.indexOf('gstatic.com')===-1){
    el.setAttribute('data-tried','0.5');
    el.onerror=function(){imgErr(this)};
    el.src='https://encrypted-tbn0.gstatic.com/'+orig.replace(/^\//,'');
    return;
  }
  if((tried==='0'||tried==='0.5')&&orig){
    el.setAttribute('data-tried','1');
    el.onerror=function(){imgErr(this)};
    var proxyUrl=orig.indexOf('gstatic.com')>-1?orig:(el.getAttribute('data-orig')||orig);
    el.src='/api/img?url='+encodeURIComponent(proxyUrl);
  }else{
    var d=document.createElement('div');
    d.style.cssText='width:100%;height:100%;min-height:90px;background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f172a 100%)';
    el.replaceWith(d);
  }
}

// ─── LINK PASTE FEATURE ───
function showLinkInput(){
  var area=document.getElementById('linkInputArea');
  if(area.style.display==='none'){
    area.style.display='block';
    document.getElementById('linkInput').focus();
    document.getElementById('linkPasteBtn').style.borderColor='var(--accent)';
  }else{
    area.style.display='none';
    document.getElementById('linkPasteBtn').style.borderColor='var(--border)';
  }
}
function scanFromLink(){
  var url=document.getElementById('linkInput').value.trim();
  if(!url){document.getElementById('linkInput').style.borderColor='var(--red)';return}
  var btn=document.getElementById('linkGoBtn');
  btn.textContent=t('linkLoading');btn.disabled=true;
  fetch('/api/url-thumbnail',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})})
  .then(function(r){return r.json()})
  .then(function(d){
    btn.textContent=t('linkGo');btn.disabled=false;
    if(d.success&&d.image_b64){
      var byteStr=atob(d.image_b64);var ab=new ArrayBuffer(byteStr.length);var ia=new Uint8Array(ab);
      for(var i=0;i<byteStr.length;i++)ia[i]=byteStr.charCodeAt(i);
      var blob=new Blob([ab],{type:'image/jpeg'});
      cF=new File([blob],'link_scan.jpg',{type:'image/jpeg'});
      var reader=new FileReader();
      reader.onload=function(e){cPrev=e.target.result;showScreen();setTimeout(autoScan,300)};
      reader.readAsDataURL(cF);
    }else{
      document.getElementById('linkInput').style.borderColor='var(--red)';
      document.getElementById('linkInput').value='';
      document.getElementById('linkInput').placeholder=d.message||'Link taranamadi';
    }
  }).catch(function(){btn.textContent=t('linkGo');btn.disabled=false})
}

// ─── DEMO SCAN (trending card → scan) ───
function demoScan(encodedImgUrl){
  if(_busy)return;
  var imgUrl=decodeURIComponent(encodedImgUrl);
  document.getElementById('trendingSection').innerHTML='<div style="text-align:center;padding:40px 0"><div class="loader-orb" style="margin:0 auto 16px"></div><div style="color:var(--muted);font-size:13px">'+t('loading')+'</div></div>';
  fetch('/api/img?url='+encodeURIComponent(imgUrl))
  .then(function(r){if(!r.ok)throw new Error();return r.blob()})
  .then(function(blob){
    cF=new File([blob],'demo.jpg',{type:blob.type||'image/jpeg'});
    var reader=new FileReader();
    reader.onload=function(e){cPrev=e.target.result;showScreen();setTimeout(autoScan,300)};
    reader.readAsDataURL(cF);
  }).catch(function(){loadTrending()})
}
function demoScanUrl(imgUrl){
  if(_busy)return;
  document.getElementById('trendingSection').innerHTML='<div style="text-align:center;padding:40px 0"><div class="loader-orb" style="margin:0 auto 16px"></div><div style="color:var(--muted);font-size:13px">'+t('loading')+'</div></div>';
  fetch(imgUrl).then(function(r){return r.blob()}).then(function(blob){
    cF=new File([blob],'trend.jpg',{type:'image/jpeg'});
    cPrev=URL.createObjectURL(blob);showScreen();autoScan();
  }).catch(function(){loadTrending()})
}

// ─── CORE NAVIGATION ───
function loadF(f){if(!f.type.startsWith('image/'))return;cF=f;var r=new FileReader();r.onload=function(e){cPrev=e.target.result;showScreen()};r.readAsDataURL(f)}
function showScreen(){
  document.getElementById('home').style.display='none';
  document.getElementById('rScreen').style.display='block';
  document.getElementById('prev').src=cPrev;
  document.getElementById('prev').style.maxHeight='260px';
  document.getElementById('prev').style.display='block';
  document.getElementById('actionBtns').style.display='flex';
  document.getElementById('cropMode').style.display='none';
  document.getElementById('piecePicker').style.display='none';
  document.getElementById('ld').style.display='none';
  document.getElementById('err').style.display='none';
  document.getElementById('res').style.display='none';
  if(cropper){cropper.destroy();cropper=null}
}
function goHome(){
  if(_busy)return;
  document.getElementById('home').style.display='block';
  document.getElementById('rScreen').style.display='none';
  if(cropper){cropper.destroy();cropper=null}
  cF=null;cPrev=null;_detectId='';_detectedPieces=[];
  document.getElementById('linkInputArea').style.display='none';
  document.getElementById('linkPasteBtn').style.borderColor='var(--border)';
  document.getElementById('linkInput').value='';
  document.querySelectorAll('.bnav-item').forEach(function(el){el.classList.remove('active')});
  document.querySelectorAll('.bnav-item')[0].classList.add('active');
  loadTrending();
}

// ─── SCAN LOGIC ───
var _detectId='',_detectedPieces=[],_lastSearchQuery='',_lastShownLinks=[],_ldTimer=null,_busy=false,_currentPiece=null;

function autoScan(){
  if(_busy)return;
  document.getElementById('actionBtns').style.display='none';
  showLoading(t('loading'),[t('step_detect'),t('step_lens'),t('step_verify'),t('step_done')]);
  var fd=new FormData();fd.append('file',cF);fd.append('country',getCC());
  fetch('/api/detect',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){
    hideLoading();
    if(!d.success)return showErr(d.message||'Error');
    if(!d.pieces||d.pieces.length===0){document.getElementById('actionBtns').style.display='flex';return showErr(t('noDetect'))}
    _detectId=d.detect_id;_detectedPieces=d.pieces;
    showPiecePicker(d.pieces);
  }).catch(function(e){hideLoading();showErr(e.message)})
}

function showPiecePicker(pieces){
  document.getElementById('prev').style.maxHeight='140px';
  document.getElementById('res').style.display='none';
  document.getElementById('err').style.display='none';
  var pp=document.getElementById('piecePicker');pp.style.display='block';
  var h='<div style="margin:16px 0"><div style="display:flex;align-items:center;gap:10px;margin-bottom:6px"><div style="font-size:24px">\u2728</div><div><span style="font-size:20px;font-weight:800">'+pieces.length+' '+t('piecesFound')+'</span></div></div><p style="font-size:13px;color:var(--cyan)">'+t('pickPiece')+'</p></div>';
  h+='<div class="piece-grid">';
  for(var i=0;i<pieces.length;i++){
    var p=pieces[i];var icon=IC[p.category]||'\uD83D\uDC55';
    h+='<div class="glass piece-card" onclick="searchPiece('+i+')" style="animation-delay:'+(i*.08)+'s">';
    if(p.crop_image){h+='<img src="'+p.crop_image+'">'}else{h+='<div class="pc-noimg">'+icon+'</div>'}
    h+='<div class="pc-info"><div class="pc-cat">'+icon+' '+(p.short_title||p.category)+'</div>';
    if(p.brand)h+='<div class="pc-brand">'+p.brand+'</div>';
    var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div class="pc-text">"'+vt+'"</div>';
    h+='</div></div>';
  }
  h+='</div><button class="btn-main" onclick="startManualFromPicker()" style="margin-top:20px;background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--muted)">'+t('manual')+'</button>';
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
  _currentPiece=p;  // Store for combo
  document.getElementById('prev').style.maxHeight='120px';
  var ra=document.getElementById('res');ra.style.display='block';
  var pr=p.products||[],lc=p.lens_count||0,hero=pr[0],alts=pr.slice(1);
  var iconHtml=p.crop_image?'<img src="'+p.crop_image+'" style="width:56px;height:56px;border-radius:12px;object-fit:cover;border:2px solid '+(lc>0?'var(--cyan)':'var(--border)')+';box-shadow:0 0 10px rgba(0,229,255,.2)">':'<div style="width:56px;height:56px;border-radius:12px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:24px;border:2px solid '+(lc>0?'var(--cyan)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';
  var h='<div class="piece"><div class="p-hdr" style="display:flex;align-items:center;gap:12px;margin-bottom:12px">'+iconHtml+'<div><span style="font-size:18px;font-weight:700">'+(p.short_title||p.category)+'</span>';
  if(p.brand&&p.brand!=='?')h+='<span style="font-size:9px;font-weight:800;color:#000;background:var(--cyan);padding:3px 8px;border-radius:6px;margin-left:8px">'+p.brand+'</span>';
  var ml=p.match_level||'similar',mlKey=ml==='exact'?'matchExact':(ml==='close'?'matchClose':'matchSimilar'),mlColor=ml==='exact'?'var(--green)':(ml==='close'?'var(--cyan)':'var(--accent)');
  h+='<div style="font-size:11px;font-weight:800;color:'+mlColor+';margin-top:4px">'+t(mlKey)+'</div>';
  var vt=p.visible_text||'';if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:11px;color:var(--muted);font-style:italic;margin-top:3px">"'+vt+'"</div>';
  h+='</div></div>';
  if(!hero){h+='<div class="glass" style="padding:20px;text-align:center;color:var(--muted);font-size:13px">'+t('noProd')+'</div>'}
  else{h+=heroHTML(hero,lc>0);if(alts.length>0)h+=altsHTML(alts)}
  h+='</div>';
  // Combo button
  h+='<button id="comboBtn" class="btn-main" style="margin-top:16px;background:linear-gradient(135deg,rgba(255,32,121,.15),rgba(77,0,255,.15));color:var(--accent);border:1px solid rgba(255,32,121,.4);box-shadow:0 0 20px rgba(255,32,121,.1)" onclick="loadCombo()">'+t('comboBtn')+'</button>';
  h+='<div id="comboResults" style="display:none"></div>';
  if(_lastSearchQuery)h+='<button id="loadMoreBtn" class="btn-main" style="margin-top:12px;background:rgba(255,32,121,.1);color:var(--accent);border:1px solid rgba(255,32,121,.3)" onclick="loadMoreResults()">'+t('loadMore')+'</button>';
  h+='<button class="btn-main" style="margin-top:12px;background:rgba(0,229,255,.1);color:var(--cyan);border:1px solid rgba(0,229,255,.3)" onclick="backToPieces()">'+t('backToPieces')+'</button>';
  ra.innerHTML=h;
}

function backToPieces(){document.getElementById('res').style.display='none';document.getElementById('err').style.display='none';if(_detectedPieces&&_detectedPieces.length>0){showPiecePicker(_detectedPieces)}else{showScreen()}}
function startManualFromPicker(){document.getElementById('piecePicker').style.display='none';document.getElementById('res').style.display='none';startManual()}
function startManual(){document.getElementById('actionBtns').style.display='none';document.getElementById('prev').style.display='none';document.getElementById('cropMode').style.display='block';document.getElementById('cropImg').src=cPrev;document.getElementById('manualQ').value='';setTimeout(function(){if(cropper)cropper.destroy();cropper=new Cropper(document.getElementById('cropImg'),{viewMode:1,dragMode:'move',autoCropArea:0.5,responsive:true,background:false,guides:true,highlight:true,cropBoxMovable:true,cropBoxResizable:true})},100)}
function cancelManual(){if(cropper){cropper.destroy();cropper=null}document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';document.getElementById('actionBtns').style.display='flex'}
function cropAndSearch(){if(!cropper)return;var canvas=cropper.getCroppedCanvas({maxWidth:800,maxHeight:800});if(!canvas)return;document.getElementById('cropMode').style.display='none';document.getElementById('prev').style.display='block';showLoading(t('loadingManual'),[t('step_bg'),t('step_lens'),t('step_verify')]);canvas.toBlob(function(blob){var q=document.getElementById('manualQ').value.trim();var fd=new FormData();fd.append('file',blob,'crop.jpg');fd.append('query',q);fd.append('country',getCC());fetch('/api/manual-search',{method:'POST',body:fd}).then(function(r){return r.json()}).then(function(d){hideLoading();if(!d.success)return showErr('Error');renderManual(d,canvas.toDataURL('image/jpeg',0.7))}).catch(function(e){hideLoading();showErr(e.message)})},'image/jpeg',0.85);if(cropper){cropper.destroy();cropper=null}}

function showLoading(txt,steps){_busy=true;var l=document.getElementById('ld');l.style.display='block';var msgs=steps||[txt];var idx=0;function render(){l.innerHTML='<div class="glass" style="display:flex;align-items:center;gap:16px;padding:20px;margin:16px 0;border-color:var(--border-glow)"><div class="loader-orb"></div><div><div style="font-size:14px;font-weight:700;color:#fff">'+msgs[idx]+'</div>'+(msgs.length>1?'<div style="font-size:11px;color:var(--cyan);margin-top:4px;font-weight:600">'+(idx+1)+'/'+msgs.length+'</div>':'')+'</div></div>'}render();if(msgs.length>1){if(_ldTimer)clearInterval(_ldTimer);_ldTimer=setInterval(function(){idx=(idx+1)%msgs.length;render()},3000)}}
function hideLoading(){_busy=false;if(_ldTimer){clearInterval(_ldTimer);_ldTimer=null}document.getElementById('ld').style.display='none'}
function showErr(m){var e=document.getElementById('err');e.style.display='block';e.innerHTML='<div class="glass" style="background:rgba(255,42,95,.1);border-color:rgba(255,42,95,.3);padding:16px;margin:16px 0;font-size:14px;color:var(--red);font-weight:500">'+m+'</div>'}

function renderManual(d,cropSrc){
  document.getElementById('prev').style.maxHeight='140px';var pr=d.products||[];var ra=document.getElementById('res');ra.style.display='block';
  var displayImg=d.crop_image||cropSrc;
  var h='<div style="display:flex;align-items:center;gap:14px;margin-bottom:16px"><img src="'+displayImg+'" style="width:56px;height:56px;border-radius:12px;object-fit:cover;border:2px solid var(--accent);box-shadow:0 0 15px var(--border-glow)"><div><span style="font-size:16px;font-weight:700">'+t('selected')+'</span>';
  if(d.query_used)h+='<div style="font-size:11px;color:var(--cyan);margin-top:4px">\uD83D\uDD0D "'+d.query_used+'"</div>';
  if(d.lens_count>0)h+='<div style="font-size:10px;color:var(--green);margin-top:2px;font-weight:700">\uD83C\uDFAF '+d.lens_count+' '+t('lensMatch')+'</div>';
  h+='</div></div>';
  if(pr.length>0){h+=heroHTML(pr[0],d.lens_count>0);if(pr.length>1)h+=altsHTML(pr.slice(1))}
  else h+='<div class="glass" style="padding:20px;text-align:center;color:var(--muted);font-size:13px">'+t('noProd')+'</div>';
  ra.innerHTML=h+'<button class="btn-main" onclick="showScreen()" style="margin-top:24px;background:rgba(255,255,255,.05);border:1px solid var(--border);color:#fff">'+t('another')+'</button>';
}

// ─── FAVORITES ───
function _getFavs(){try{return JSON.parse(localStorage.getItem('fitchy_favs')||'[]')}catch(e){return[]}}
function _setFavs(f){try{localStorage.setItem('fitchy_favs',JSON.stringify(f))}catch(e){}}
function _hasFav(link){try{return(localStorage.getItem('fitchy_favs')||'').indexOf(link)>-1}catch(e){return false}}
function _getProfile(){try{return JSON.parse(localStorage.getItem('fitchy_profile')||'null')}catch(e){return null}}
function _setProfile(p){try{localStorage.setItem('fitchy_profile',JSON.stringify(p))}catch(e){}}
function _getFolders(){try{return JSON.parse(localStorage.getItem('fitchy_folders')||'[]')}catch(e){return[]}}
function _setFolders(f){try{localStorage.setItem('fitchy_folders',JSON.stringify(f))}catch(e){}}
function _initProfile(){
  var p=_getProfile();
  if(!p){p={name:'',handle:'@fitchy_user',bio:'',avatar:'',followers:0,following:0,refitches:0};_setProfile(p)}
  var folders=_getFolders();
  if(!folders.length){_setFolders([
    {id:'all',name:CC_LANG[CC]==='tr'?'Tümü':'All',icon:'✧',system:true},
    {id:'fav',name:CC_LANG[CC]==='tr'?'Favoriler':'Favorites',icon:'♡',system:true},
    {id:'date',name:CC_LANG[CC]==='tr'?'İlk Buluşma':'Date Night',icon:'🌙'},
    {id:'casual',name:CC_LANG[CC]==='tr'?'Günlük':'Casual',icon:'☀️'},
    {id:'wishlist',name:'Wishlist',icon:'💸'}
  ])}
  return p
}
function toggleFav(e,link,img,title,price,brand){
  e.preventDefault();e.stopPropagation();
  var favs=_getFavs();var idx=favs.findIndex(function(f){return f.link===link});
  if(idx>-1){favs.splice(idx,1);e.target.innerHTML='\u2661';e.target.style.color='var(--text)'}
  else{favs.push({link:link,img:img,title:title,price:price,brand:brand,folder:'fav',visible:true,ts:Date.now(),refitches:Math.floor(Math.random()*50)});e.target.innerHTML='\u2665';e.target.style.color='var(--accent)'}
  _setFavs(favs)
}
function toggleItemVis(idx){
  var favs=_getFavs();if(favs[idx]){favs[idx].visible=!favs[idx].visible;_setFavs(favs);showFavs()}
}
function moveToFolder(idx,folderId){
  var favs=_getFavs();if(favs[idx]){favs[idx].folder=folderId;_setFavs(favs);showFavs()}
}
function addFolder(){
  var isTr=CC_LANG[CC]==='tr';
  var name=prompt(isTr?'Klasör adı:':'Folder name:');
  if(!name)return;
  var folders=_getFolders();
  var id='f_'+Date.now();
  folders.push({id:id,name:name,icon:'📂'});
  _setFolders(folders);showFavs()
}
var _activeFolder='all';
function showFavs(){
  _initProfile();
  var isTr=CC_LANG[CC]==='tr';
  document.querySelectorAll('.bnav-item').forEach(function(el){el.classList.remove('active')});
  document.querySelectorAll('.bnav-item')[1].classList.add('active');
  document.getElementById('home').style.display='none';
  document.getElementById('rScreen').style.display='block';
  var ab=document.getElementById('actionBtns');if(ab)ab.style.display='none';
  var cm=document.getElementById('cropMode');if(cm)cm.style.display='none';
  var pv=document.getElementById('prev');if(pv)pv.style.display='none';
  var pp=document.getElementById('piecePicker');if(pp)pp.style.display='none';
  var ra=document.getElementById('res');ra.style.display='block';
  var profile=_getProfile();var favs=_getFavs();var folders=_getFolders();
  // Profile header
  var h='<div class="profile-header">';
  h+='<div class="profile-avatar" onclick="editProfile()">';
  if(profile.avatar)h+='<img src="'+profile.avatar+'" onerror="this.style.display=\'none\'">';
  else h+='<span style="font-size:36px">'+(profile.name?profile.name[0].toUpperCase():'👤')+'</span>';
  h+='</div>';
  h+='<div class="profile-name">'+(profile.name||(isTr?'Profilini Düzenle':'Edit Profile'))+'</div>';
  h+='<div class="profile-handle">'+(profile.handle||'@fitchy_user')+'</div>';
  if(profile.bio)h+='<div class="profile-bio">'+profile.bio+'</div>';
  // Stats
  var totalRefitches=favs.reduce(function(s,f){return s+(f.refitches||0)},0);
  h+='<div class="profile-stats">';
  h+='<div class="profile-stat"><div class="num">'+favs.length+'</div><div class="lbl">'+(isTr?'Parça':'Pieces')+'</div></div>';
  h+='<div class="profile-stat"><div class="num">'+totalRefitches+'</div><div class="lbl">Re-fitch</div></div>';
  h+='<div class="profile-stat"><div class="num">'+profile.followers+'</div><div class="lbl">'+(isTr?'Takipçi':'Followers')+'</div></div>';
  h+='</div>';
  // Action buttons
  h+='<div class="profile-actions">';
  h+='<button class="profile-btn primary" onclick="shareProfile()">'+(isTr?'🔗 Profili Paylaş':'🔗 Share Profile')+'</button>';
  h+='<button class="profile-btn secondary" onclick="editProfile()">'+(isTr?'✏️ Düzenle':'✏️ Edit')+'</button>';
  h+='</div>';
  h+='</div>';
  // Folder tabs
  h+='<div class="folder-tabs">';
  for(var fi=0;fi<folders.length;fi++){
    var fo=folders[fi];
    var cnt=fo.id==='all'?favs.length:favs.filter(function(f){return f.folder===fo.id}).length;
    h+='<div class="folder-tab'+(fo.id===_activeFolder?' active':'')+'" onclick="_activeFolder=\''+fo.id+'\';showFavs()">'+fo.icon+' '+fo.name+'<span class="cnt">'+cnt+'</span></div>';
  }
  h+='<div class="folder-tab" onclick="addFolder()" style="border-style:dashed">+ '+(isTr?'Yeni':'New')+'</div>';
  h+='</div>';
  // Filter items by folder
  var filtered=_activeFolder==='all'?favs:favs.filter(function(f){return f.folder===_activeFolder});
  if(filtered.length===0){
    var emptyMsg=isTr?'Bu klasörde henüz parça yok':'No pieces in this folder yet';
    if(favs.length===0)emptyMsg=isTr?'Beğendiğin ürünlerin ♡ butonuna bas — burada görünecek!':'Tap ♡ on items you love — they\'ll appear here!';
    h+='<div class="glass" style="text-align:center;padding:40px;color:var(--muted);margin-top:20px;font-size:13px">'+emptyMsg+'</div>';
  }else{
    h+='<div class="item-grid">';
    for(var i=0;i<filtered.length;i++){
      var f=filtered[i];var realIdx=favs.indexOf(f);
      var safeT=(f.title||'').replace(/'/g,"\\'");var safeP=(f.price||'').replace(/'/g,"\\'");var safeB=(f.brand||'').replace(/'/g,"\\'");
      h+='<div class="item-card" style="'+(f.visible?'':'opacity:.6;border-color:rgba(255,255,255,.05)')+'">';
      // Badges
      h+='<div class="item-badge">';
      if(!f.visible)h+='<span class="lock-badge">🔒 '+(isTr?'Gizli':'Private')+'</span>';
      if((f.refitches||0)>10)h+='<span class="refitch-badge">🔄 '+(f.refitches||0)+'</span>';
      h+='</div>';
      // Visibility toggle
      h+='<div class="vis-toggle" onclick="event.stopPropagation();toggleItemVis('+realIdx+')">'+(f.visible?'👁':'🔒')+'</div>';
      h+='<a href="'+f.link+'" target="_blank" style="text-decoration:none;color:var(--text)">';
      if(f.img)h+='<img src="'+f.img+'" onerror="this.style.display=\'none\'">';
      else h+='<div style="width:100%;height:160px;background:linear-gradient(135deg,#1a1a2e,#16213e);display:flex;align-items:center;justify-content:center;font-size:32px;border-bottom:1px solid var(--border)">👗</div>';
      h+='<div class="info"><div class="title">'+f.title+'</div><div class="brand">'+(f.brand||'')+'</div><div class="price">'+(f.price||'')+'</div></div></a>';
      // Re-fitch button
      h+='<div class="refitch-btn" onclick="event.stopPropagation();refitchItem('+realIdx+')">🔄 Re-fitch</div>';
      // Remove fav
      h+='<div onclick="event.stopPropagation();toggleFav(event,\''+f.link+'\',\''+(f.img||'')+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\');showFavs()" style="position:absolute;top:8px;right:36px;background:rgba(0,0,0,.7);color:var(--accent);padding:6px;border-radius:50%;cursor:pointer;font-size:14px;line-height:1;backdrop-filter:blur(4px)">\u2665</div>';
      // Folder move
      h+='<div onclick="event.stopPropagation();showFolderMenu('+realIdx+')" style="position:absolute;bottom:10px;left:8px;background:rgba(0,0,0,.7);color:var(--muted);padding:5px 8px;border-radius:8px;cursor:pointer;font-size:10px;backdrop-filter:blur(4px)">📂</div>';
      h+='</div>';
    }
    h+='</div>';
  }
  h+='<button class="btn-main" onclick="goHome()" style="margin-top:24px;background:rgba(255,255,255,.05);border:1px solid var(--border);color:#fff">'+t('back')+'</button>';
  h+='<div style="height:80px"></div>';
  ra.innerHTML=h;
}
function editProfile(){
  var isTr=CC_LANG[CC]==='tr';
  var p=_getProfile()||{name:'',handle:'@user',bio:'',avatar:'',followers:0,following:0,refitches:0};
  var name=prompt(isTr?'İsmin:':'Your name:',p.name||'');if(name===null)return;
  var handle=prompt(isTr?'Kullanıcı adın (@):':'Username (@):',p.handle||'@');if(handle===null)return;
  if(handle&&handle[0]!=='@')handle='@'+handle;
  var bio=prompt(isTr?'Biyo (kısa açıklama):':'Bio:',p.bio||'');if(bio===null)return;
  p.name=name;p.handle=handle;p.bio=bio;
  // Simulate followers for demo
  if(!p.followers)p.followers=Math.floor(Math.random()*500)+12;
  _setProfile(p);showFavs()
}
function shareProfile(){
  var p=_getProfile();var handle=(p&&p.handle)||'@user';
  var url='fitchy.app/'+handle.replace('@','');
  if(navigator.share){navigator.share({title:'fitchy. '+handle,text:(CC_LANG[CC]==='tr'?'Dolabımı keşfet! ':'Check out my closet! ')+url,url:'https://'+url}).catch(function(){})}
  else{navigator.clipboard.writeText('https://'+url).then(function(){alert((CC_LANG[CC]==='tr'?'Link kopyalandı! 🔗':'Link copied! 🔗')+'\nhttps://'+url)}).catch(function(){})}
}
function refitchItem(idx){
  var favs=_getFavs();if(favs[idx]){favs[idx].refitches=(favs[idx].refitches||0)+1;_setFavs(favs);showFavs()}
}
function showFolderMenu(idx){
  var isTr=CC_LANG[CC]==='tr';var folders=_getFolders();
  var names=folders.filter(function(f){return!f.system||f.id==='fav'}).map(function(f){return f.icon+' '+f.name});
  var choice=prompt((isTr?'Hangi klasöre taşıyayım?\n':'Move to folder:\n')+names.join(', '));
  if(!choice)return;
  var match=folders.find(function(f){return f.name.toLowerCase()===choice.toLowerCase()||choice.indexOf(f.name)>-1});
  if(match)moveToFolder(idx,match.id);
}

// ─── PRODUCT CARDS ───
function heroHTML(p,isLens){
  var img=_fixThumb(p.image||p.thumbnail||'');
  var verified=p.ai_verified;var score=p.match_score||0;
  var badgeText=verified?t('aiMatch'):(isLens?t('lensLabel'):t('recommended'));
  var borderColor=verified?'rgba(0,229,255,.5)':p._verified?(p._verified.glow==='cyan'?'rgba(0,229,255,.5)':'rgba(34,197,94,.5)'):'rgba(255,32,121,.5)';
  var isFav=_hasFav(p.link);
  var safeT=(p.title||'').replace(/'/g,"\\'");var safeP=(p.price||'').replace(/'/g,"\\'");var safeB=(p.brand||'').replace(/'/g,"\\'");
  var imgUrl=img||'';
  var h='<div style="position:relative"><a href="'+p.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)"><div class="glass hero" style="border-color:'+borderColor+'">';
  if(imgUrl)h+='<img src="'+imgUrl+'" data-orig="'+imgUrl+'" onerror="imgErr(this)">';
  h+='<div class="badge" style="'+(verified?'background:var(--cyan);color:#000':'')+'">'+badgeText+'</div>';
  // Verified/Sponsored badge
  var vb=p._verified;
  if(vb)h+='<div style="position:absolute;top:12px;right:12px;background:'+(vb.glow==='cyan'?'rgba(0,229,255,.9)':'rgba(34,197,94,.9)')+';color:#000;font-size:9px;font-weight:800;padding:4px 10px;border-radius:8px;backdrop-filter:blur(4px);letter-spacing:.3px">\u2611\uFE0F '+(vb.badge||t('verified'))+'</div>';
  else if(p._sponsored)h+='<div style="position:absolute;top:12px;right:12px;background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;font-size:9px;font-weight:800;padding:4px 10px;border-radius:8px;backdrop-filter:blur(4px);letter-spacing:.3px">\u2728 '+t('sponsored')+'</div>';
  else if(score>=7)h+='<div style="position:absolute;top:12px;right:12px;background:rgba(0,0,0,.8);color:var(--cyan);font-size:11px;font-weight:800;padding:4px 10px;border-radius:8px;border:1px solid rgba(0,229,255,.3);backdrop-filter:blur(4px)">'+score+'/10</div>';
  h+='<div class="info"><div class="t">'+p.title+'</div><div class="s">'+(p.brand||p.source||'')+'</div><div class="row"><span class="price">'+(p.price||t('noPrice'))+'</span><button class="btn">'+t('goStore')+'</button></div></div></div></a>';
  // VTON button
  h+='<div onclick="event.stopPropagation();openVton(\''+safeT+'\',\''+imgUrl+'\')" style="position:absolute;bottom:80px;left:12px;background:linear-gradient(135deg,rgba(77,0,255,.85),rgba(255,32,121,.85));color:#fff;padding:8px 14px;border-radius:12px;cursor:pointer;font:700 11px Outfit,sans-serif;z-index:10;backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.15);display:flex;align-items:center;gap:5px;box-shadow:0 4px 16px rgba(77,0,255,.4)"><span>🪞</span>'+t('vtonBtn')+'</div>';
  h+='<div onclick="toggleFav(event,\''+p.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:12px;right:'+(score>=7?'60px':'12px')+';background:rgba(0,0,0,.7);color:'+(isFav?'var(--accent)':'var(--text)')+';padding:8px;border-radius:50%;cursor:pointer;font-size:18px;z-index:10;line-height:1;backdrop-filter:blur(4px);border:1px solid rgba(255,255,255,.1)">'+(isFav?'\u2665':'\u2661')+'</div></div>';
  return h;
}

function altsHTML(list){
  var h='<div style="font-size:13px;font-weight:700;color:var(--text);margin:16px 0 10px;display:flex;align-items:center;gap:6px">'+t('alts')+'</div><div class="scroll">';
  for(var i=0;i<list.length;i++){
    var a=list[i];var img=_fixThumb(a.thumbnail||a.image||'');var isFav=_hasFav(a.link);
    var safeT=(a.title||'').replace(/'/g,"\\'");var safeP=(a.price||'').replace(/'/g,"\\'");var safeB=(a.brand||a.source||'').replace(/'/g,"\\'");
    var imgUrl=img||'';
    h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="glass card" style="'+(a.ai_verified?'border-color:rgba(0,229,255,.4)':a._verified?(a._verified.glow==='cyan'?'border-color:rgba(0,229,255,.3)':'border-color:rgba(34,197,94,.3)'):'')+';">';
    if(imgUrl)h+='<img src="'+imgUrl+'" data-orig="'+imgUrl+'" onerror="imgErr(this)">';
    h+='<div class="ci">';
    if(a._verified)h+='<div style="font-size:8px;color:'+(a._verified.glow==='cyan'?'var(--cyan)':'var(--green)')+';font-weight:800;margin-bottom:3px">\u2611\uFE0F '+(a._verified.badge||'')+'</div>';
    else if(a.ai_verified)h+='<div style="font-size:9px;color:var(--cyan);font-weight:800;margin-bottom:4px;letter-spacing:.5px">\u2713 '+t('aiMatch')+'</div>';
    h+='<div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div>';
    h+='<div onclick="toggleFav(event,\''+a.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.7);color:'+(isFav?'var(--accent)':'var(--text)')+';padding:6px;border-radius:50%;cursor:pointer;font-size:14px;z-index:10;line-height:1;backdrop-filter:blur(4px)">'+(isFav?'\u2665':'\u2661')+'</div></a>';
  }
  return h+'</div>';
}

// ─── FAVORITES PAGE (see social profile system above) ───

// ─── FIT-CHECK: AI Outfit Roast & Score ───
function startFitCheck(){
  document.getElementById('fitCheckInput').click();
}
function handleFitCheck(e){
  var file=e.target.files[0];if(!file)return;
  var reader=new FileReader();
  reader.onload=function(ev){
    var imgData=ev.target.result;
    // Show loading in result screen
    document.getElementById('home').style.display='none';
    document.getElementById('rScreen').style.display='block';
    var ra=document.getElementById('res');ra.style.display='block';
    ra.innerHTML='<div style="text-align:center;padding:40px 20px"><img src="'+imgData+'" style="width:160px;height:220px;object-fit:cover;border-radius:24px;border:2px solid var(--border);box-shadow:0 0 30px rgba(255,32,121,.2);margin-bottom:24px"><div class="loader-orb" style="width:48px;height:48px;margin:0 auto 16px"></div><div style="font-size:16px;font-weight:800;color:var(--text)">'+t('fitCheckLoading')+'</div><div style="font-size:13px;color:var(--muted);margin-top:8px">'+(CC_LANG[CC]==='tr'?'AI stilist kombinine bakıyor... 👀<br><span style="font-size:11px;opacity:.6">Acımasız olabilir, hazır ol bestie 💅</span>':'AI stylist is judging... 👀<br><span style="font-size:11px;opacity:.6">Might be savage, brace yourself bestie 💅</span>')+'</div></div>';
    fetch('/api/fit-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:imgData,lang:CC_LANG[CC]||'tr'})})
    .then(function(r){return r.json()})
    .then(function(d){showFitCheckResult(d,imgData)})
    .catch(function(){showFitCheckResult({success:false,score:50,emoji:'🤔',roast:'Bir sorun oluştu ama eminim harika görünüyorsun bestie! 💅',tips:[]},imgData)});
  };
  reader.readAsDataURL(file);
  e.target.value='';
}
function showFitCheckResult(d,imgData){
  var ra=document.getElementById('res');
  var score=d.score||50;var emoji=d.emoji||'🔥';
  // Score color + label
  var col,label;
  if(score>=90){col='#00e5ff';label='LEGENDARY 👑';}
  else if(score>=80){col='#00e5ff';label='SLAY 💅';}
  else if(score>=70){col='var(--accent)';label='FIRE 🔥';}
  else if(score>=60){col='var(--accent)';label='DECENT ✨';}
  else if(score>=40){col='#ff9800';label='MID 😐';}
  else{col='#f44336';label='HELP 💀';}
  var h='<div class="fitcheck-result" id="fitCheckResultCard">';
  // fitchy branding for screenshots
  h+='<div style="font-family:Outfit,sans-serif;font-size:16px;font-weight:800;margin-bottom:16px"><span class="text-gradient">fitchy.</span> <span style="color:var(--muted);font-weight:500;font-size:12px">fit-check</span></div>';
  // Photo with glow border
  h+='<div style="position:relative;display:inline-block"><img src="'+imgData+'" style="width:160px;height:220px;object-fit:cover;border-radius:24px;border:3px solid '+col+';box-shadow:0 0 40px '+col+'50,0 0 80px '+col+'20">';
  h+='<div style="position:absolute;bottom:-12px;left:50%;transform:translateX(-50%);font-size:36px;filter:drop-shadow(0 2px 8px rgba(0,0,0,.5))">'+emoji+'</div></div>';
  // Score display
  h+='<div style="margin-top:24px"><div class="drip-score" style="color:'+col+';font-size:80px">'+score+'</div>';
  h+='<div class="drip-label" style="color:'+col+';letter-spacing:3px">'+label+'</div></div>';
  h+='<div class="drip-bar"><div class="drip-bar-fill" style="width:0%;background:linear-gradient(90deg,'+col+',var(--purple))"></div></div>';
  // Roast text
  if(d.roast)h+='<div class="roast-text" style="font-size:16px;line-height:1.7;padding:4px 12px">'+d.roast+'</div>';
  // Tips
  if(d.tips&&d.tips.length){
    h+='<div style="margin-top:16px;font-size:12px;font-weight:700;color:var(--muted);letter-spacing:1px">'+t('fitCheckTips')+'</div>';
    for(var i=0;i<d.tips.length;i++){h+='<div class="tip-card" style="font-size:13px;line-height:1.5">'+d.tips[i]+'</div>'}
  }
  // Buttons
  h+='<div style="margin-top:20px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">';
  h+='<button class="share-fitcheck" onclick="shareFitCheck('+score+')">📸 '+t('fitCheckShare')+'</button>';
  h+='<button class="share-fitcheck" style="background:rgba(255,255,255,.06);box-shadow:none;border:1px solid var(--border)" onclick="startFitCheck()">🔄 '+t('fitCheckAnother')+'</button>';
  h+='</div>';
  h+='<button class="btn-main" onclick="goHome()" style="margin-top:16px;background:rgba(255,255,255,.05);border:1px solid var(--border);color:#fff">'+t('back')+'</button>';
  h+='</div>';
  ra.innerHTML=h;
  // Animate the bar fill with delay
  setTimeout(function(){var bar=document.querySelector('.drip-bar-fill');if(bar)bar.style.width=score+'%'},200);
}
function shareFitCheck(score){
  var emoji=score>=90?'👑':score>=80?'💅':score>=70?'🔥':score>=50?'✨':'💀';
  var text=(CC_LANG[CC]==='tr'?
    emoji+' fitchy. bana '+score+'/100 Drip Score verdi!\n\nSen de kombinini yargılat → fitchy.app':
    emoji+' fitchy. gave me '+score+'/100 on my Drip Score!\n\nGet your outfit roasted → fitchy.app');
  if(navigator.share){navigator.share({title:'fitchy. Fit-Check '+emoji,text:text}).catch(function(){})}
  else{navigator.clipboard.writeText(text).then(function(){alert('Copied! 📋')}).catch(function(){})}
}

// ─── VIRTUAL TRY-ON (Sanal Kabin) ───
var _vtonSession='vton_'+Math.random().toString(36).slice(2,8);
var _vtonBodySaved=false;
function openVton(title,garmentImg){
  var modal=document.getElementById('vtonModal');
  modal.classList.add('show');
  var isTr=CC_LANG[CC]==='tr';
  document.getElementById('vtonModalTitle').textContent=isTr?'🪞 Sanal Kabin':'🪞 Virtual Fitting Room';
  if(!_vtonBodySaved){
    document.getElementById('vtonModalBody').textContent=t('vtonSaveBody');
    document.getElementById('vtonModalContent').innerHTML='<div style="margin:20px 0"><div style="font-size:48px;margin-bottom:12px">📸</div><div style="font-size:12px;color:var(--muted);margin-bottom:16px">'+(isTr?'Tam boy, düz duruşlu bir fotoğraf yükle':'Upload a full-body, straight-on photo')+'</div><button onclick="document.getElementById(\'vtonBodyInput\').click()" style="background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;border:none;padding:14px 28px;border-radius:16px;font:700 14px Outfit,sans-serif;cursor:pointer">'+(isTr?'📷 Fotoğraf Yükle':'📷 Upload Photo')+'</button></div>';
    modal._pendingTitle=title;modal._pendingGarment=garmentImg;
  }else{
    runVtonAnalysis(title,garmentImg);
  }
}
function handleVtonBody(e){
  var file=e.target.files[0];if(!file)return;
  var reader=new FileReader();
  reader.onload=function(ev){
    var imgData=ev.target.result;
    // Save body photo
    fetch('/api/vton-save-body',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:imgData,session:_vtonSession})})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.success){
        _vtonBodySaved=true;
        var modal=document.getElementById('vtonModal');
        if(modal._pendingTitle)runVtonAnalysis(modal._pendingTitle,modal._pendingGarment);
      }
    });
  };
  reader.readAsDataURL(file);e.target.value='';
}
function runVtonAnalysis(title,garmentImg){
  var isTr=CC_LANG[CC]==='tr';
  document.getElementById('vtonModalBody').textContent='';
  document.getElementById('vtonModalContent').innerHTML='<div style="padding:30px"><div class="loader-orb" style="width:40px;height:40px;margin:0 auto 16px"></div><div style="font-size:13px;color:var(--muted)">'+t('vtonLoading')+'</div></div>';
  fetch('/api/vton-tryon',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,garment_img:garmentImg||'',session:_vtonSession,lang:CC_LANG[CC]||'tr'})})
  .then(function(r){return r.json()})
  .then(function(d){
    if(!d.success){document.getElementById('vtonModalContent').innerHTML='<div style="color:var(--muted);padding:20px;font-size:13px">'+d.message+'</div>';return}
    var col=d.fit_score>=80?'#00e5ff':d.fit_score>=60?'var(--accent)':'#ff9800';
    var h='<div style="padding:10px">';
    h+='<div style="font-size:40px;margin:8px 0">'+(d.emoji||'👗')+'</div>';
    h+='<div style="font-size:48px;font-weight:900;color:'+col+';letter-spacing:-2px">'+d.fit_score+'</div>';
    h+='<div style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:1px;text-transform:uppercase;margin:4px 0 12px">FIT SCORE</div>';
    h+='<div class="drip-bar"><div class="drip-bar-fill" style="width:'+d.fit_score+'%;background:'+col+'"></div></div>';
    if(d.analysis)h+='<div style="font-size:14px;line-height:1.6;color:var(--text);margin:12px 0;padding:0 4px">'+d.analysis+'</div>';
    if(d.size_tip)h+='<div class="tip-card">📏 '+d.size_tip+'</div>';
    if(d.style_note)h+='<div class="tip-card">💡 '+d.style_note+'</div>';
    h+='</div>';
    document.getElementById('vtonModalContent').innerHTML=h;
    document.getElementById('vtonModalTitle').textContent=t('vtonResult')+' — '+title;
  })
  .catch(function(err){document.getElementById('vtonModalContent').innerHTML='<div style="color:var(--muted);padding:20px;font-size:13px">Hata: '+err+'</div>'});
}
function closeVton(){
  document.getElementById('vtonModal').classList.remove('show');
}

// ─── OUTFIT COMBO ("Bunu Neyle Giyerim?") ───
function loadCombo(){
  if(!_currentPiece)return;
  var btn=document.getElementById('comboBtn');
  if(btn){btn.innerHTML='<div class="loader-orb" style="width:20px;height:20px;margin:0"></div> '+t('comboLoading');btn.onclick=null;btn.style.opacity='0.7'}
  var body=JSON.stringify({category:_currentPiece.category,title:_currentPiece.short_title||'',brand:_currentPiece.brand||'',color:_currentPiece.color||'',style:_currentPiece.style_type||'',country:getCC()});
  fetch('/api/outfit-combo',{method:'POST',headers:{'Content-Type':'application/json'},body:body})
  .then(function(r){return r.json()})
  .then(function(d){
    if(btn)btn.remove();
    var cr=document.getElementById('comboResults');
    if(!d.success||!d.suggestions||!d.suggestions.length){cr.style.display='block';cr.innerHTML='<div class="glass" style="padding:16px;text-align:center;color:var(--muted);font-size:13px">Kombin önerisi oluşturulamadı</div>';return}
    cr.style.display='block';
    var h='<div style="margin-top:16px"><div style="font-size:15px;font-weight:800;margin-bottom:12px;display:flex;align-items:center;gap:8px" class="text-gradient">'+t('comboTitle')+'</div>';
    for(var i=0;i<d.suggestions.length;i++){
      var s=d.suggestions[i];var icon=IC[s.category]||'\uD83D\uDC55';
      h+='<div class="glass" style="padding:16px;margin-bottom:12px;border-color:rgba(255,32,121,.2)">';
      h+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px"><span style="font-size:20px">'+icon+'</span><div><div style="font-size:14px;font-weight:700">'+(s.description||s.category)+'</div><div style="font-size:11px;color:var(--muted);margin-top:2px;font-style:italic">'+(s.why||'')+'</div></div></div>';
      if(s.products&&s.products.length){
        h+='<div class="scroll" style="margin-top:8px">';
        for(var j=0;j<s.products.length;j++){
          var p=s.products[j];var img=p.thumbnail||p.image||'';
          h+='<a href="'+p.link+'" target="_blank" rel="noopener" class="glass card" style="width:120px">';
          if(img)h+='<img src="'+img+'" data-orig="'+img+'" onerror="imgErr(this)" style="width:120px;height:120px">';
          h+='<div class="ci"><div class="cn">'+p.title+'</div><div class="cs">'+(p.brand||p.source||'')+'</div><div class="cp">'+(p.price||'')+'</div></div></a>';
        }
        h+='</div>';
      }
      h+='</div>';
    }
    h+='</div>';
    cr.innerHTML=h;
  }).catch(function(){if(btn){btn.innerHTML=t('comboBtn');btn.onclick=loadCombo;btn.style.opacity='1'}})
}

// ─── LOAD MORE ───
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
    container.innerHTML='<div style="font-size:13px;font-weight:700;color:var(--cyan);margin:20px 0 10px;display:flex;align-items:center;gap:6px">\u2727 '+t('loadMore')+'</div><div class="scroll">'+d.products.map(function(a){
      var img=a.thumbnail||a.image||'';var isFav=_hasFav(a.link);
      var safeT=(a.title||'').replace(/'/g,"\\'");var safeP=(a.price||'').replace(/'/g,"\\'");var safeB=(a.brand||a.source||'').replace(/'/g,"\\'");
      var imgUrl=img||'';
      var h='<a href="'+a.link+'" target="_blank" rel="noopener" class="glass card" style="position:relative">';
      if(imgUrl)h+='<img src="'+imgUrl+'" data-orig="'+imgUrl+'" onerror="imgErr(this)">';
      h+='<div class="ci"><div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div>';
      h+='<div onclick="toggleFav(event,\''+a.link+'\',\''+img+'\',\''+safeT+'\',\''+safeP+'\',\''+safeB+'\')" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.7);color:'+(isFav?'var(--accent)':'var(--text)')+';padding:6px;border-radius:50%;cursor:pointer;font-size:14px;z-index:10;line-height:1;backdrop-filter:blur(4px)">'+(isFav?'\u2665':'\u2661')+'</div></a>';
      return h;
    }).join('')+'</div>';
    var buttons=ra.querySelectorAll('.btn-main');
    if(buttons.length>0)ra.insertBefore(container,buttons[0]);
    else ra.appendChild(container);
    d.products.forEach(function(p){_lastShownLinks.push(p.link)});
  }).catch(function(){if(btn){btn.innerHTML=t('loadMore');btn.style.opacity='1';btn.onclick=loadMoreResults}})
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
