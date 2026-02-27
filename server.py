import os
import json
import requests as req
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from serpapi import GoogleSearch

app = FastAPI(title="FitFinder API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Marka Haritası ───
BRAND_MAP = {
    "trendyol.com": "TRENDYOL", "hepsiburada.com": "HEPSIBURADA",
    "boyner.com.tr": "BOYNER", "defacto.com": "DEFACTO",
    "lcwaikiki.com": "LC WAIKIKI", "koton.com": "KOTON",
    "network.com.tr": "NETWORK", "vakko.com": "VAKKO",
    "beymen.com": "BEYMEN", "morhipo.com": "MORHIPO",
    "n11.com": "N11", "flo.com.tr": "FLO",
    "sneakscloud.com": "SNEAKS CLOUD", "superstep.com.tr": "SUPERSTEP",
    "tozlu.com": "TOZLU", "modanisa.com": "MODANISA",
    "colins.com": "COLIN'S", "mavi.com": "MAVI",
    "ipekyol.com": "IPEKYOL", "machka.com.tr": "MACHKA",
    "twist.com.tr": "TWIST", "yargici.com": "YARGICI",
    "derimod.com.tr": "DERIMOD", "kemal-tanca.com": "KEMAL TANCA",
    "zara.com": "ZARA", "bershka.com": "BERSHKA",
    "pullandbear.com": "PULL&BEAR", "stradivarius.com": "STRADIVARIUS",
    "massimodutti.com": "MASSIMO DUTTI", "hm.com": "H&M",
    "mango.com": "MANGO", "cos.com": "COS",
    "uniqlo.com": "UNIQLO", "gap.com": "GAP",
    "asos.com": "ASOS", "shein.com": "SHEIN",
    "nike.com": "NIKE", "adidas.com": "ADIDAS",
    "puma.com": "PUMA", "newbalance.com": "NEW BALANCE",
    "converse.com": "CONVERSE", "vans.com": "VANS",
    "gucci.com": "GUCCI", "louisvuitton.com": "LOUIS VUITTON",
    "prada.com": "PRADA", "burberry.com": "BURBERRY",
    "calvinklein.com": "CALVIN KLEIN", "tommy.com": "TOMMY HILFIGER",
    "ralphlauren.com": "RALPH LAUREN", "lacoste.com": "LACOSTE",
    "hugoboss.com": "HUGO BOSS", "levi.com": "LEVIS",
    "balenciaga.com": "BALENCIAGA",
}

TR_SCORES = {
    "trendyol.com": 100, "hepsiburada.com": 95, "boyner.com": 90,
    "beymen.com": 88, "defacto.com": 85, "lcwaikiki.com": 85,
    "koton.com": 85, "flo.com": 82, "n11.com": 80,
    "morhipo.com": 78, "superstep.com": 78, "mavi.com": 85,
    "colins.com": 80, "ipekyol.com": 82, "network.com": 80,
    "vakko.com": 85, "sneakscloud.com": 78,
}

GLOBAL_SCORES = {
    "zara.com": 60, "hm.com": 58, "bershka.com": 55,
    "mango.com": 55, "nike.com": 65, "adidas.com": 65,
    "asos.com": 50, "shein.com": 40, "amazon.com": 45,
}


def detect_brand(link, source):
    combined = (link + " " + source).lower()
    for domain, brand in BRAND_MAP.items():
        if domain in combined:
            return brand
    return source.upper() if source else ""


def get_score(link, source):
    combined = (link + " " + source).lower()
    for d, s in TR_SCORES.items():
        if d in combined: return s
    for d, s in GLOBAL_SCORES.items():
        if d in combined: return s
    return 30


def sort_products(products):
    return sorted(products, key=lambda p: get_score(p.get("link",""), p.get("source","")), reverse=True)


def is_tr_store(link, source):
    combined = (link + " " + source).lower()
    return any(d in combined for d in TR_SCORES)


def upload_image(image_bytes):
    try:
        r = req.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except: pass
    try:
        r = req.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        if r.status_code == 200:
            url = r.json().get("data", {}).get("url", "")
            if url: return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except: pass
    return None


TR_TRANSLATIONS = {
    "jacket": "ceket", "leather jacket": "deri ceket", "bomber jacket": "bomber ceket",
    "leather bomber jacket": "deri bomber ceket", "denim jacket": "kot ceket",
    "puffer jacket": "mont", "coat": "kaban", "blazer": "blazer ceket",
    "hoodie": "kapusonlu sweatshirt", "hoodie sweatshirt": "kapusonlu sweatshirt",
    "sweatshirt": "sweatshirt", "t-shirt": "tisort", "shirt": "gomlek",
    "polo shirt": "polo tisort", "tank top": "atlet", "top": "ust",
    "sweater": "kazak", "cardigan": "hırka", "vest": "yelek",
    "jeans": "kot pantolon", "straight leg jeans": "duz kesim kot pantolon",
    "wide leg jeans": "bol paca kot pantolon", "skinny jeans": "dar kot pantolon",
    "pants": "pantolon", "trousers": "pantolon", "shorts": "sort",
    "cargo pants": "kargo pantolon", "chinos": "chino pantolon",
    "sneakers": "spor ayakkabi", "athletic sneakers": "spor ayakkabi",
    "boots": "bot", "loafers": "loafer ayakkabi", "shoes": "ayakkabi",
    "sandals": "sandalet", "heels": "topuklu ayakkabi",
    "baseball cap": "sapka", "hat": "sapka", "beanie": "bere",
    "sunglasses": "gunes gozlugu", "bag": "canta", "backpack": "sirt cantasi",
    "scarf": "atki", "belt": "kemer", "watch": "kol saati",
    "dress": "elbise", "skirt": "etek",
    "brown": "kahverengi", "black": "siyah", "white": "beyaz",
    "blue": "mavi", "red": "kirmizi", "green": "yesil",
    "gray": "gri", "grey": "gri", "beige": "bej", "navy": "lacivert",
    "cream": "krem", "pink": "pembe", "orange": "turuncu", "yellow": "sari",
}


def to_turkish(text):
    text_lower = text.lower().strip()
    # Tam eslestirme
    if text_lower in TR_TRANSLATIONS:
        return TR_TRANSLATIONS[text_lower]
    # Parcali eslestirme
    result = text_lower
    for en, tr in sorted(TR_TRANSLATIONS.items(), key=lambda x: len(x[0]), reverse=True):
        if en in result:
            result = result.replace(en, tr)
    return result


def search_shopping(query, limit=6):
    products = []
    seen = set()

    # Turkce query olustur
    tr_query = to_turkish(query)

    queries_to_try = []
    if tr_query != query.lower():
        queries_to_try.append(("tr", tr_query))
    queries_to_try.append(("tr", query))
    queries_to_try.append(("us", query))

    for gl, q in queries_to_try:
        if len(products) >= limit:
            break
        try:
            params = {
                "engine": "google_shopping",
                "q": q,
                "gl": gl if gl != "us" else "us",
                "hl": "tr" if gl == "tr" else "en",
                "api_key": SERPAPI_KEY,
            }
            print(f"  SerpAPI call: q='{q}' gl={params['gl']} hl={params['hl']}")
            search = GoogleSearch(params)
            data = search.get_dict()

            # Log what we got back
            if "error" in data:
                print(f"  SerpAPI ERROR: {data['error']}")
                continue

            shopping = data.get("shopping_results", [])
            print(f"  SerpAPI returned {len(shopping)} shopping results")
            if len(shopping) > 0 and len(products) == 0:
                print(f"  First item keys: {list(shopping[0].keys())}")
                print(f"  First item: {json.dumps(shopping[0], ensure_ascii=False)[:300]}")

            for item in shopping:
                link = item.get("product_link") or item.get("link") or ""
                title = item.get("title", "")
                if not link or link in seen or not title: continue
                if is_blocked(link): continue
                seen.add(link)
                source = item.get("source", "")
                products.append({
                    "title": title, "brand": detect_brand(link, source),
                    "source": source, "link": link,
                    "price": item.get("price", ""), "thumbnail": item.get("thumbnail", ""),
                    "is_tr": is_tr_store(link, source),
                })
                if len(products) >= limit: break
        except Exception as e:
            print(f"  Shopping search EXCEPTION for '{q}': {type(e).__name__}: {e}")

    return sort_products(products)[:limit]


def detect_pieces_with_claude(image_b64):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": """Bu fotograftaki kisinin uzerindeki HER kiyafet ve aksesuar parcasini tespit et.

Her parca icin:
- category: jacket|top|bottom|dress|shoes|bag|hat|sunglasses|watch|accessory|scarf
- description: Kisa urun aciklamasi (Ingilizce, alisveris aramasi icin)
- color: Renk (Ingilizce)
- brand: Logo/yazi gorunuyorsa marka, yoksa "?"

SADECE JSON array dondur:
[{"category":"...","description":"...","color":"...","brand":"..."}]"""},
                    ],
                }],
            },
            timeout=30,
        )
        data = r.json()
        text = data.get("content", [{}])[0].get("text", "")
        import re
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"Claude detect error: {e}")
    return None


BLOCKED_DOMAINS = [
    "pinterest.", "instagram.", "facebook.", "twitter.", "x.com",
    "tiktok.", "youtube.", "reddit.", "tumblr.", "flickr.",
    "weheartit.", "lookbook.", "chictopia.", "polyvore.",
    "blogspot.", "wordpress.", "medium.com", "wattpad.",
]


def is_blocked(link):
    link_lower = link.lower()
    return any(d in link_lower for d in BLOCKED_DOMAINS)


def search_lens(image_url):
    products = []
    seen = set()
    try:
        search = GoogleSearch({
            "engine": "google_lens", "url": image_url,
            "api_key": SERPAPI_KEY, "hl": "tr", "country": "tr",
        })
        for match in search.get_dict().get("visual_matches", []):
            link = match.get("link", "")
            title = match.get("title", "")
            if not link or link in seen or not title: continue
            if is_blocked(link): continue
            seen.add(link)
            source = match.get("source", "")
            price_info = match.get("price", {})
            products.append({
                "title": title, "brand": detect_brand(link, source),
                "source": source, "link": link,
                "price": price_info.get("value", ""), "thumbnail": match.get("thumbnail", ""),
                "is_tr": is_tr_store(link, source),
            })
            if len(products) >= 10: break
    except: pass
    return sort_products(products)


# ─── API Endpoints ───

@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    contents = await file.read()
    import base64
    image_b64 = base64.b64encode(contents).decode()

    # 1) Google Lens
    image_url = upload_image(contents)
    lens_results = []
    if image_url:
        lens_results = search_lens(image_url)

    # 2) Claude Vision - parca tespiti
    pieces = detect_pieces_with_claude(image_b64)
    if not pieces:
        return {
            "success": True,
            "lens_results": lens_results,
            "pieces": [],
            "message": "Google Lens sonuclari yuklendi. Parca tespiti icin ANTHROPIC_API_KEY gerekli."
        }

    # 3) Her parca icin Google Shopping
    piece_results = []
    for piece in pieces:
        desc = piece.get("description", "")
        color = piece.get("color", "")
        brand = piece.get("brand", "")

        # Query olustur
        parts = []
        if brand and brand != "?":
            parts.append(brand)
        parts.append(desc)
        if color:
            parts.append(color)
        query = " ".join(parts).strip()

        print(f"Searching for piece: {piece.get('category')} -> query: '{query}'")
        products = search_shopping(query) if query else []
        print(f"  Found {len(products)} products")

        piece_results.append({
            "category": piece.get("category", ""),
            "description": desc,
            "color": color,
            "brand": piece.get("brand", ""),
            "products": products,
        })

    return {
        "success": True,
        "lens_results": lens_results,
        "pieces": piece_results,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "serpapi": bool(SERPAPI_KEY),
        "anthropic": bool(ANTHROPIC_API_KEY),
    }


# ─── Web UI ───

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


HTML_PAGE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>FitFinder</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0c0c0e; --card: #141416; --card2: #1a1a1e;
  --border: #222226; --text: #f0ece4; --muted: #7a7872; --dim: #4a4843;
  --accent: #d4a853; --accent-soft: rgba(212,168,83,.08);
  --accent-border: rgba(212,168,83,.2);
  --green: #6fcf7c; --red: #e85d5d;
}
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
body { background: var(--bg); color: var(--text); font-family: 'DM Sans', sans-serif; min-height: 100vh; display: flex; justify-content: center; }
::-webkit-scrollbar { display: none; }
@keyframes fadeUp { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }
@keyframes fadeIn { from { opacity:0; } to { opacity:1; } }
@keyframes spin { to { transform:rotate(360deg); } }
@keyframes shimmer { 0% { background-position:-200% 0; } 100% { background-position:200% 0; } }
@keyframes pulse { 0%,100% { opacity:.6; } 50% { opacity:1; } }

.app { width: 100%; max-width: 440px; min-height: 100vh; position: relative; }
.screen { animation: fadeIn .3s ease; }

/* Header */
.header { position: sticky; top:0; z-index:40; background: rgba(12,12,14,.88); backdrop-filter: blur(20px); padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); }
.header .back { cursor: pointer; display: flex; align-items: center; gap: 8; color: var(--muted); font-size: 12px; }
.header .logo { font-size: 11px; font-weight: 600; color: var(--accent); letter-spacing: 2.5px; text-transform: uppercase; }
.header .save-btn { cursor: pointer; font-size: 12px; color: var(--accent); font-weight: 600; }

/* Home */
.home-content { padding: 0 20px; }
.hero { padding-top: 56px; padding-bottom: 40px; }
.hero .tag { font-size: 11px; font-weight: 600; color: var(--accent); letter-spacing: 3px; text-transform: uppercase; margin-bottom: 12px; }
.hero h1 { font-size: 38px; font-weight: 700; font-family: 'Cormorant Garamond', serif; line-height: 1.1; }
.hero h1 em { font-style: italic; color: var(--accent); }
.hero .sub { font-size: 14px; color: var(--muted); margin-top: 14px; line-height: 1.6; }

.upload-btn { background: var(--accent); border-radius: 14px; padding: 18px 24px; display: flex; align-items: center; gap: 14px; cursor: pointer; margin-bottom: 12px; }
.upload-btn .icon { width: 48px; height: 48px; border-radius: 12px; background: rgba(0,0,0,.15); display: flex; align-items: center; justify-content: center; font-size: 24px; }
.upload-btn .label { font-size: 16px; font-weight: 700; color: var(--bg); }
.upload-btn .sublabel { font-size: 12px; color: rgba(0,0,0,.5); margin-top: 2px; }

.drop-zone { border: 2px dashed var(--border); border-radius: 14px; padding: 24px; text-align: center; cursor: pointer; color: var(--dim); font-size: 13px; }

.features { margin-top: 48px; display: flex; flex-direction: column; gap: 20px; padding-bottom: 100px; }
.feature { display: flex; gap: 14px; align-items: flex-start; animation: fadeUp .5s ease both; }
.feature .fi { font-size: 18px; width: 40px; height: 40px; border-radius: 10px; background: var(--accent-soft); border: 1px solid var(--accent-border); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.feature .ft { font-size: 14px; font-weight: 600; }
.feature .fd { font-size: 12px; color: var(--muted); margin-top: 2px; }

/* Results */
.preview-wrap { border-radius: 16px; overflow: hidden; margin: 16px 0; position: relative; background: #111; }
.preview-wrap img { width: 100%; display: block; object-fit: cover; transition: max-height .4s; }
.preview-wrap .gradient { position: absolute; inset: 0; background: linear-gradient(transparent 40%, rgba(12,12,14,.95)); pointer-events: none; }
.preview-wrap .badge { position: absolute; bottom: 14px; left: 16px; background: rgba(0,0,0,.7); backdrop-filter: blur(8px); border-radius: 8px; padding: 5px 12px; font-size: 12px; font-weight: 600; color: var(--green); }

.scan-btn { background: var(--accent); border-radius: 14px; padding: 16px; display: flex; align-items: center; justify-content: center; gap: 10px; cursor: pointer; border: none; width: 100%; font-size: 16px; font-weight: 700; color: var(--bg); font-family: 'DM Sans', sans-serif; }

.loading-box { display: flex; align-items: center; gap: 14px; background: var(--card); border-radius: 14px; padding: 18px 20px; border: 1px solid var(--border); margin-bottom: 16px; }
.spinner { width: 28px; height: 28px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
.spinner-sm { width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
.skeleton { border-radius: 6px; background: linear-gradient(90deg, var(--card) 25%, var(--card2) 50%, var(--card) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; }

.error-box { background: rgba(232,93,93,.06); border: 1px solid rgba(232,93,93,.15); border-radius: 12px; padding: 12px 16px; margin-top: 12px; font-size: 13px; color: var(--red); }
.retry-btn { margin-top: 10px; padding: 8px 16px; background: var(--card); border-radius: 8px; display: inline-block; cursor: pointer; font-size: 12px; color: var(--accent); font-weight: 600; border: 1px solid var(--accent-border); }

/* Pieces */
.section-title { font-size: 22px; font-weight: 700; font-family: 'Cormorant Garamond', serif; }
.section-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }

.piece-header { display: flex; align-items: center; gap: 12px; padding: 14px 16px; background: var(--card); border: 1px solid var(--accent-border); cursor: pointer; }
.piece-header.open { border-radius: 14px 14px 0 0; border-bottom: none; }
.piece-header.closed { border-radius: 14px; }
.piece-icon { font-size: 28px; }
.piece-cat { font-size: 10px; font-weight: 700; color: var(--accent); letter-spacing: 1.5px; text-transform: uppercase; }
.piece-brand { font-size: 9px; font-weight: 800; color: var(--bg); background: var(--accent); padding: 2px 7px; border-radius: 4px; margin-left: 8px; }
.piece-desc { font-size: 14px; font-weight: 600; margin-top: 2px; }
.piece-color { font-size: 11px; color: var(--muted); margin-top: 2px; }
.piece-right { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.piece-count { font-size: 11px; color: var(--green); font-weight: 600; }
.piece-arrow { font-size: 16px; color: var(--muted); transition: transform .2s; }

.piece-body { background: var(--bg); border: 1px solid var(--accent-border); border-top: none; border-radius: 0 0 14px 14px; padding: 12px; display: flex; flex-direction: column; gap: 8px; animation: fadeIn .2s ease; }
.piece-loading { font-size: 11px; color: var(--muted); text-align: center; animation: pulse 1.5s infinite; }

/* Product Card */
.product { display: flex; gap: 12px; padding: 12px; background: var(--card); border-radius: 12px; border: 1px solid var(--border); text-decoration: none; color: var(--text); animation: fadeUp .3s ease both; }
.product img { width: 68px; height: 68px; border-radius: 8px; object-fit: cover; background: var(--card2); flex-shrink: 0; }
.product .placeholder { width: 68px; height: 68px; border-radius: 8px; background: var(--card2); display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }
.product .title { font-size: 12px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.product .store-badge { font-size: 9px; font-weight: 800; padding: 1px 6px; border-radius: 3px; display: inline-block; margin-top: 3px; }
.product .store-tr { color: var(--bg); background: var(--accent); }
.product .store-global { color: var(--text); background: var(--border); }
.product .price-row { display: flex; align-items: center; justify-content: space-between; margin-top: 6px; }
.product .price { font-size: 15px; font-weight: 700; color: var(--accent); }
.product .buy-link { font-size: 10px; color: var(--accent); font-weight: 600; }

/* Lens Section */
.lens-title { font-size: 18px; font-weight: 700; font-family: 'Cormorant Garamond', serif; margin-top: 24px; margin-bottom: 4px; }
.lens-sub { font-size: 11px; color: var(--dim); margin-bottom: 12px; }

/* Bottom Nav */
.bottom-nav { position: fixed; bottom: 0; left: 50%; transform: translateX(-50%); width: 100%; max-width: 440px; background: rgba(12,12,14,.92); backdrop-filter: blur(20px); border-top: 1px solid var(--border); display: flex; padding: 8px 0 22px; z-index: 50; }
.nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px; cursor: pointer; }
.nav-icon { font-size: 20px; }
.nav-label { font-size: 10px; font-weight: 600; letter-spacing: .5px; }
.nav-active { color: var(--accent); }
.nav-inactive { color: var(--dim); }
</style>
</head>
<body>
<div class="app" id="app">

  <!-- HOME SCREEN -->
  <div id="screen-home" class="screen">
    <div class="home-content">
      <div class="hero">
        <div class="tag">FitFinder</div>
        <h1>Komple outfiti<br><em>parca parca</em> bul.</h1>
        <p class="sub">Fotograf yukle. AI her kiyafet parcasini tespit edip<br>Trendyol, Hepsiburada ve daha fazlasinda arar.</p>
      </div>
      <div class="upload-btn" onclick="document.getElementById('fileInput').click()">
        <div class="icon">&#x1F4F7;</div>
        <div>
          <div class="label">Fotograf Yukle</div>
          <div class="sublabel">Galeri, kamera veya screenshot</div>
        </div>
      </div>
      <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
        veya surukle &amp; birak
      </div>
      <input type="file" id="fileInput" accept="image/*" style="display:none">

      <div class="features">
        <div class="feature" style="animation-delay:.3s">
          <div class="fi">&#x1F1F9;&#x1F1F7;</div>
          <div><div class="ft">Turk Magazalar Once</div><div class="fd">Trendyol, Hepsiburada, Boyner, Beymen...</div></div>
        </div>
        <div class="feature" style="animation-delay:.4s">
          <div class="fi">&#x1F50D;</div>
          <div><div class="ft">Google Lens + Shopping</div><div class="fd">Gorsel arama + metin arama birlikte</div></div>
        </div>
        <div class="feature" style="animation-delay:.5s">
          <div class="fi">&#x1F6D2;</div>
          <div><div class="ft">Direkt Satin Al</div><div class="fd">Urune tikla, magazaya git, satin al</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- RESULTS SCREEN -->
  <div id="screen-results" class="screen" style="display:none">
    <div class="header">
      <div class="back" onclick="goHome()">&#x2190; Geri</div>
      <div class="logo">FitFinder</div>
      <div id="saveBtn" class="save-btn" style="display:none" onclick="saveOutfit()">Kaydet &#x2661;</div>
      <div id="savePlaceholder" style="width:48px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <div class="preview-wrap">
        <img id="previewImg" src="" style="max-height:360px">
        <div class="gradient"></div>
        <div id="pieceBadge" class="badge" style="display:none"></div>
      </div>
      <button id="scanBtn" class="scan-btn" onclick="analyze()">&#x1F50D; Kiyafetleri Tara &amp; Bul</button>
      <div id="loadingArea" style="display:none"></div>
      <div id="errorArea" style="display:none"></div>
      <div id="resultsArea" style="display:none"></div>
    </div>
  </div>

  <!-- BOTTOM NAV -->
  <div class="bottom-nav">
    <div class="nav-item" onclick="switchTab('home')">
      <div class="nav-icon nav-active" id="navHomeIcon">&#x2B21;</div>
      <div class="nav-label nav-active" id="navHomeLabel">Kesfet</div>
    </div>
    <div class="nav-item" onclick="switchTab('saved')">
      <div class="nav-icon nav-inactive" id="navSavedIcon">&#x2661;</div>
      <div class="nav-label nav-inactive" id="navSavedLabel">Kaydedilenler</div>
    </div>
  </div>
</div>

<script>
var currentFile = null;
var currentPreview = null;
var currentPieces = null;
var savedOutfits = [];

var ICONS = { hat:"&#x1F9E2;", sunglasses:"&#x1F576;&#xFE0F;", top:"&#x1F455;", jacket:"&#x1F9E5;", bag:"&#x1F45C;", accessory:"&#x1F48D;", watch:"&#x231A;", bottom:"&#x1F456;", dress:"&#x1F457;", shoes:"&#x1F45F;", scarf:"&#x1F9E3;" };

document.getElementById('fileInput').addEventListener('change', function(e) {
  if (e.target.files[0]) handleFile(e.target.files[0]);
});

var dz = document.getElementById('dropZone');
dz.addEventListener('dragover', function(e) { e.preventDefault(); });
dz.addEventListener('drop', function(e) { e.preventDefault(); if(e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });

function handleFile(file) {
  if (!file.type.startsWith('image/')) return;
  currentFile = file;
  var r = new FileReader();
  r.onload = function(e) {
    currentPreview = e.target.result;
    showResults();
  };
  r.readAsDataURL(file);
}

function showResults() {
  document.getElementById('screen-home').style.display = 'none';
  document.getElementById('screen-results').style.display = 'block';
  document.getElementById('previewImg').src = currentPreview;
  document.getElementById('previewImg').style.maxHeight = '360px';
  document.getElementById('scanBtn').style.display = 'flex';
  document.getElementById('loadingArea').style.display = 'none';
  document.getElementById('errorArea').style.display = 'none';
  document.getElementById('resultsArea').style.display = 'none';
  document.getElementById('pieceBadge').style.display = 'none';
  document.getElementById('saveBtn').style.display = 'none';
  document.getElementById('savePlaceholder').style.display = 'block';
  currentPieces = null;
}

function goHome() {
  document.getElementById('screen-home').style.display = 'block';
  document.getElementById('screen-results').style.display = 'none';
  currentFile = null; currentPreview = null; currentPieces = null;
}

function switchTab(tab) {
  var hi = document.getElementById('navHomeIcon');
  var hl = document.getElementById('navHomeLabel');
  var si = document.getElementById('navSavedIcon');
  var sl = document.getElementById('navSavedLabel');
  if (tab === 'home') {
    hi.className = 'nav-icon nav-active'; hl.className = 'nav-label nav-active';
    si.className = 'nav-icon nav-inactive'; sl.className = 'nav-label nav-inactive';
    if (currentPreview) { showResults(); } else { goHome(); }
  } else {
    hi.className = 'nav-icon nav-inactive'; hl.className = 'nav-label nav-inactive';
    si.className = 'nav-icon nav-active'; sl.className = 'nav-label nav-active';
  }
}

function analyze() {
  if (!currentFile) return;
  document.getElementById('scanBtn').style.display = 'none';

  var la = document.getElementById('loadingArea');
  la.style.display = 'block';
  la.innerHTML = '<div class="loading-box"><div class="spinner"></div><div><div style="font-size:14px;font-weight:600">Analiz ediliyor...</div><div style="font-size:11px;color:var(--muted);margin-top:2px">Google Lens + AI parca tespiti</div></div></div>';
  for (var i=0; i<3; i++) {
    la.innerHTML += '<div style="background:var(--card);border-radius:14px;padding:16px;border:1px solid var(--border);margin-bottom:10px;display:flex;gap:12px;align-items:center"><div class="skeleton" style="width:40px;height:40px;border-radius:10px"></div><div style="flex:1;display:flex;flex-direction:column;gap:6px"><div class="skeleton" style="width:60px;height:10px"></div><div class="skeleton" style="width:80%;height:14px"></div></div></div>';
  }

  var fd = new FormData();
  fd.append('file', currentFile);

  fetch('/api/full-analyze', { method: 'POST', body: fd })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      la.style.display = 'none';
      if (!data.success) {
        showError(data.message || 'Analiz basarisiz');
        return;
      }
      renderResults(data);
    })
    .catch(function(err) {
      la.style.display = 'none';
      showError(err.message);
    });
}

function showError(msg) {
  var ea = document.getElementById('errorArea');
  ea.style.display = 'block';
  ea.innerHTML = '<div class="error-box">&#x26A0; ' + msg + '<div class="retry-btn" onclick="analyze()">Tekrar Dene</div></div>';
}

function renderResults(data) {
  document.getElementById('previewImg').style.maxHeight = '200px';
  var pieces = data.pieces || [];
  var lens = data.lens_results || [];
  currentPieces = pieces;

  if (pieces.length > 0) {
    document.getElementById('pieceBadge').style.display = 'block';
    document.getElementById('pieceBadge').textContent = pieces.length + ' parca tespit edildi';
    document.getElementById('saveBtn').style.display = 'block';
    document.getElementById('savePlaceholder').style.display = 'none';
  }

  var ra = document.getElementById('resultsArea');
  ra.style.display = 'block';
  var html = '';

  // Parca sonuclari
  if (pieces.length > 0) {
    html += '<div class="section-title" style="margin-top:8px">Outfit Analizi</div>';
    html += '<div class="section-sub">&#x1F1F9;&#x1F1F7; Turk magazalar oncelikli sonuclar</div>';
    html += '<div style="display:flex;flex-direction:column;gap:14px;margin-top:14px">';

    for (var i=0; i<pieces.length; i++) {
      var p = pieces[i];
      var icon = ICONS[p.category] || '&#x1F3F7;&#xFE0F;';
      var brandHtml = (p.brand && p.brand !== '?') ? '<span class="piece-brand">' + p.brand.toUpperCase() + '</span>' : '';
      var pid = 'piece-' + i;

      html += '<div style="animation:fadeUp .4s ease ' + (i*0.1) + 's both">';
      html += '<div class="piece-header open" id="ph-' + i + '" onclick="togglePiece(' + i + ')">';
      html += '<span class="piece-icon">' + icon + '</span>';
      html += '<div style="flex:1"><div style="display:flex;align-items:center"><span class="piece-cat">' + p.category + '</span>' + brandHtml + '</div>';
      html += '<div class="piece-desc">' + p.description + '</div>';
      html += '<div class="piece-color">' + p.color + '</div></div>';
      html += '<div class="piece-right">';
      if (p.products && p.products.length > 0) {
        html += '<span class="piece-count">' + p.products.length + ' urun</span>';
      }
      html += '<span class="piece-arrow" id="pa-' + i + '" style="transform:rotate(90deg)">&#x203A;</span></div></div>';

      html += '<div class="piece-body" id="pb-' + i + '">';
      if (p.products && p.products.length > 0) {
        for (var j=0; j<p.products.length; j++) {
          html += renderProduct(p.products[j], j);
        }
      } else {
        html += '<div style="font-size:12px;color:var(--dim);text-align:center;padding:16px">Urun bulunamadi</div>';
      }
      html += '</div></div>';
    }
    html += '</div>';
  }

  // Google Lens sonuclari
  if (lens.length > 0) {
    html += '<div class="lens-title">&#x1F50D; Google Lens Sonuclari</div>';
    html += '<div class="lens-sub">Gorsel benzerlik ile bulunan urunler</div>';
    html += '<div style="display:flex;flex-direction:column;gap:8px">';
    for (var k=0; k<lens.length; k++) {
      html += renderProduct(lens[k], k);
    }
    html += '</div>';
  }

  if (pieces.length === 0 && lens.length === 0) {
    html += '<div style="text-align:center;padding:40px 0;color:var(--dim)">Sonuc bulunamadi. Farkli bir fotograf deneyin.</div>';
  }

  ra.innerHTML = html;
}

function renderProduct(p, idx) {
  var thumbHtml = '';
  if (p.thumbnail) {
    thumbHtml = '<img src="' + p.thumbnail + '" onerror="this.hidden=true">';
  } else {
    thumbHtml = '<div class="placeholder">' + (p.is_tr ? '&#x1F1F9;&#x1F1F7;' : '&#x1F6D2;') + '</div>';
  }

  var storeClass = p.is_tr ? 'store-badge store-tr' : 'store-badge store-global';
  var storeHtml = '';
  if (p.brand) storeHtml += '<span class="' + storeClass + '">' + p.brand + '</span> ';
  if (p.source) storeHtml += '<span style="font-size:9px;color:var(--dim)">' + p.source + '</span>';

  return '<a href="' + p.link + '" target="_blank" rel="noopener" class="product" style="animation-delay:' + (idx*0.05) + 's">'
    + thumbHtml
    + '<div style="flex:1;min-width:0">'
    + '<div class="title">' + (p.title || '') + '</div>'
    + '<div style="margin-top:3px">' + storeHtml + '</div>'
    + '<div class="price-row"><span class="price">' + (p.price || '—') + '</span><span class="buy-link">Satin Al &#x2197;</span></div>'
    + '</div></a>';
}

function togglePiece(i) {
  var body = document.getElementById('pb-' + i);
  var header = document.getElementById('ph-' + i);
  var arrow = document.getElementById('pa-' + i);
  if (body.style.display === 'none') {
    body.style.display = 'flex';
    header.className = 'piece-header open';
    arrow.style.transform = 'rotate(90deg)';
  } else {
    body.style.display = 'none';
    header.className = 'piece-header closed';
    arrow.style.transform = 'rotate(0)';
  }
}

function saveOutfit() {
  if (currentPieces && currentPreview) {
    savedOutfits.push({ preview: currentPreview, pieces: currentPieces, date: new Date().toLocaleDateString('tr-TR') });
    alert('Outfit kaydedildi! (' + savedOutfits.length + ' outfit)');
  }
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
