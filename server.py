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

CROP_ZONES = {
    "hat": (0, 15, 20, 85), "sunglasses": (8, 20, 25, 80),
    "scarf": (15, 15, 40, 85), "jacket": (15, 5, 60, 95),
    "top": (20, 10, 55, 90), "dress": (15, 5, 80, 95),
    "bag": (30, 0, 70, 40), "watch": (35, 0, 55, 35),
    "accessory": (25, 10, 50, 90), "bottom": (45, 10, 85, 90),
    "shoes": (75, 10, 100, 90),
}

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
    "ayakkabi", "bot", "sneaker", "boot", "shoe",
    "canta", "bag", "sapka", "bere", "hat", "cap",
    "gozluk", "saat", "watch", "sunglasses",
    "erkek", "kadin", "giyim", "fashion", "wear", "clothing",
]


def brand(link, src):
    c = (link + " " + src).lower()
    for d, b in BRAND_MAP.items():
        if d in c: return b
    return src if src else ""

def istr(link, src):
    tr = ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.", "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep."]
    return any(d in (link+" "+src).lower() for d in tr)

def blocked(link):
    return any(d in link.lower() for d in BLOCKED)

def fashion(link, title, src):
    c = (link+" "+src).lower()
    if any(d in c for d in FASHION_DOMAINS): return True
    t = (title+" "+src).lower()
    return any(k in t for k in FASHION_KW)


def zone_crop(img_bytes, cat):
    z = CROP_ZONES.get(cat)
    if not z: return None
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        y1,x1,y2,x2 = z
        cr = img.crop(((x1/100)*w, (y1/100)*h, (x2/100)*w, (y2/100)*h))
        buf = io.BytesIO()
        cr.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except: return None

def thumb_b64(img_bytes, cat):
    cr = zone_crop(img_bytes, cat)
    return ("data:image/jpeg;base64," + base64.b64encode(cr).decode()) if cr else None


async def upload_img(img_bytes):
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post("https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype":"fileupload","time":"1h"},
                files={"fileToUpload":("i.jpg",img_bytes,"image/jpeg")})
            if r.status_code==200 and r.text.startswith("http"): return r.text.strip()
        except Exception as e: print(f"Upload err: {e}")
        try:
            r = await c.post("https://tmpfiles.org/api/v1/upload",
                files={"file":("i.jpg",img_bytes,"image/jpeg")})
            if r.status_code==200:
                u=r.json().get("data",{}).get("url","")
                if u: return u.replace("tmpfiles.org/","tmpfiles.org/dl/")
        except Exception as e: print(f"Upload2 err: {e}")
    return None


async def claude_detect(img_b64):
    if not ANTHROPIC_API_KEY: return None
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":1500,
                    "messages":[{"role":"user","content":[
                        {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}},
                        {"type":"text","text":"""Analyze every clothing item and accessory this person is wearing.

CRITICAL RULES:
1. ONLY list items that are CLEARLY VISIBLE as SEPARATE garments
2. A collar, lining, or edge peeking under a jacket is NOT a separate piece
3. If you can see less than 30% of an item, do NOT list it
4. Do NOT guess hidden items - only what you can actually see
5. Read ALL visible text, logos, brand names, patches on each item

For each CLEARLY VISIBLE item:
- category: hat|sunglasses|scarf|jacket|top|bottom|dress|shoes|bag|watch|accessory
- short_title_tr: 2-4 word Turkish name. Be EXACT:
  * bere ≠ şapka, bomber ≠ blazer, jogger ≠ kumaş pantolon, bot ≠ sneaker
  * Include style: "Bordo Nakışlı Şapka", "Yeşil Varsity Ceket"
- color_tr: Turkish color
- brand: ONLY if you can READ it on the item, else "?"
- visible_text: ALL readable text/logos/patches (e.g. "Timeless", "Rebel", "R")
- search_query_tr: 4-6 word ULTRA SPECIFIC Turkish query
  * MUST match exact item type from short_title_tr
  * Include brand if readable, include ALL visible text/patches
  * Examples:
    "bershka yeşil timeless rebel varsity ceket"
    "bordo R harfli nakışlı beyzbol şapkası"
    "gri wide leg kumaş pantolon erkek"

Return ONLY valid JSON array, no markdown no backticks:
[{"category":"","short_title_tr":"","color_tr":"","brand":"","visible_text":"","search_query_tr":""}]"""}
                    ]}]})
            d = r.json()
            if "error" in d: return None
            t = d.get("content",[{}])[0].get("text","").strip()
            if t.startswith("```"): t = re.sub(r'^```\w*\n?','',t); t = re.sub(r'\n?```$','',t)
            m = re.search(r'\[.*\]', t, re.DOTALL)
            if m:
                try: return json.loads(m.group())
                except: pass
        except Exception as e: print(f"Claude err: {e}")
    return None


def _lens(url):
    res = []; seen = set()
    try:
        d = GoogleSearch({"engine":"google_lens","url":url,"api_key":SERPAPI_KEY,"hl":"tr","country":"tr"}).get_dict()
        all_matches = d.get("visual_matches",[])
        print(f"  Lens raw: {len(all_matches)} visual_matches")
        for m in all_matches:
            lnk,ttl,src = m.get("link",""),m.get("title",""),m.get("source","")
            if not lnk or not ttl or lnk in seen: continue
            if blocked(lnk): continue
            seen.add(lnk)
            pr = m.get("price",{}); pr = pr.get("value","") if isinstance(pr,dict) else str(pr)
            res.append({"title":ttl,"brand":brand(lnk,src),"source":src,"link":lnk,"price":pr,"thumbnail":m.get("thumbnail",""),"is_tr":istr(lnk,src)})
            if len(res)>=20: break
    except Exception as e: print(f"Lens err: {e}")
    print(f"  Lens after filter: {len(res)} results")
    return res


# ─── Match Lens results to piece categories ───
PIECE_KEYWORDS = {
    "hat": ["sapka","şapka","cap","hat","bere","beanie","kasket","fes","kepi","baseball","snapback","bucket","fedora","berretto"],
    "sunglasses": ["gozluk","gözlük","sunglasses","güneş","eyewear"],
    "scarf": ["atki","atkı","sal","şal","fular","scarf","bandana"],
    "jacket": ["ceket","mont","kaban","blazer","bomber","jacket","coat","varsity","parka","trench","palto","kase","kaşe","denim jacket","overcoat","cardigan","hırka","yelek","vest","windbreaker","anorak","puffer","embroidered cloth"],
    "top": ["tisort","tişört","gomlek","gömlek","sweatshirt","hoodie","kazak","bluz","top","shirt","polo","triko","t-shirt","tee","crop","tank","henley"],
    "bottom": ["pantolon","jean","denim","jogger","chino","pants","trousers","sort","şort","etek","skirt","cargo","wide leg","slim fit","straight","baggy","tapered"],
    "dress": ["elbise","dress","tulum","jumpsuit","romper"],
    "shoes": ["ayakkabi","ayakkabı","sneaker","bot","boot","shoe","terlik","loafer","sandalet","spor ayak","trainer","runner","chelsea","oxford","moccasin","slip-on"],
    "bag": ["canta","çanta","bag","clutch","sirt","sırt","backpack","tote","crossbody","messenger"],
    "watch": ["saat","watch","kol saati","timepiece"],
    "accessory": ["kolye","bileklik","yuzuk","yüzük","kupe","küpe","aksesuar","kemer","belt","necklace","bracelet","ring","earring","chain"],
}

def match_lens_to_pieces(lens_results, pieces):
    """Distribute Lens results to matching piece categories"""
    piece_lens = {i: [] for i in range(len(pieces))}
    unmatched = []

    for lr in lens_results:
        title_lower = lr["title"].lower()
        matched = False
        for i, p in enumerate(pieces):
            cat = p.get("category", "")
            keywords = PIECE_KEYWORDS.get(cat, [])
            if any(kw in title_lower for kw in keywords):
                piece_lens[i].append(lr)
                matched = True
                break
        if not matched:
            unmatched.append(lr)

    return piece_lens, unmatched


def _shop(q, limit=6):
    res = []; seen = set()
    try:
        d = GoogleSearch({"engine":"google_shopping","q":q,"gl":"tr","hl":"tr","api_key":SERPAPI_KEY}).get_dict()
        for i in d.get("shopping_results",[]):
            lnk = i.get("product_link") or i.get("link","")
            ttl = i.get("title",""); src = i.get("source","")
            if not lnk or not ttl or lnk in seen or blocked(lnk): continue
            seen.add(lnk)
            res.append({"title":ttl,"brand":brand(lnk,src),"source":src,"link":lnk,
                "price":i.get("price",str(i.get("extracted_price",""))),"thumbnail":i.get("thumbnail",""),"is_tr":istr(lnk,src)})
            if len(res)>=limit: break
    except Exception as e: print(f"Shop err: {e}")
    return res


async def process_piece(p, img_bytes):
    cat = p.get("category","")
    q = p.get("search_query_tr","") or p.get("short_title_tr","")
    print(f"[{cat}] Shopping query: '{q}'")

    # Text search only — zone crop/Lens unreliable for auto mode
    products = []
    if q:
        products = await asyncio.to_thread(_shop, q)
        print(f"[{cat}] Found: {len(products)}")

    return {
        "category": cat,
        "short_title_tr": p.get("short_title_tr", cat.title()),
        "color_tr": p.get("color_tr",""),
        "brand": p.get("brand",""),
        "visible_text": p.get("visible_text",""),
        "crop_preview": None,
        "products": products[:6],
        "lens_count": 0,
    }


# ─── AUTO ANALYZE ───
@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...)):
    if not SERPAPI_KEY: raise HTTPException(500,"No API key")
    contents = await file.read()

    # Claude icin gorseli kucult (maliyet + hiz)
    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        b64 = base64.b64encode(contents).decode()

    print("\n=== AUTO ANALYZE ===")

    # Claude detect + Upload in PARALLEL
    detect_task = claude_detect(b64)
    upload_task = upload_img(contents)
    pieces, url = await asyncio.gather(detect_task, upload_task)

    # Full image Lens (gorsel eslesme - birebir urun bulur)
    lens_results = []
    if url:
        lens_results = await asyncio.to_thread(_lens, url)
        print(f"Full image Lens: {len(lens_results)}")

    if not pieces:
        return {"success":True,"pieces":[],"lens_results":lens_results}

    print(f"Claude: {len(pieces)} pieces")

    # Match Lens results to pieces
    piece_lens, unmatched = match_lens_to_pieces(lens_results, pieces)
    for i, p in enumerate(pieces):
        cat = p.get("category","")
        print(f"  [{cat}] {len(piece_lens[i])} Lens matches")
    print(f"  Unmatched Lens: {len(unmatched)}")

    # Process all pieces in parallel (Shopping) then merge with Lens
    tasks = [process_piece(p, contents) for p in pieces]
    results = list(await asyncio.gather(*tasks))

    # Merge: Lens first, then Shopping, deduplicated
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

    return {"success":True,"pieces":results,"lens_unmatched":unmatched[:5]}


# ─── MANUAL SEARCH (cropped by user) ───
@app.post("/api/manual-search")
async def manual_search(file: UploadFile = File(...), query: str = Form("")):
    if not SERPAPI_KEY: raise HTTPException(500,"No API key")
    contents = await file.read()
    print(f"\n=== MANUAL SEARCH === query='{query}'")

    # Lens with user-cropped image
    url = await upload_img(contents)
    lens_res = []
    if url:
        lens_res = await asyncio.to_thread(_lens, url)
        print(f"Manual Lens: {len(lens_res)}")

    # Shopping if query provided
    shop_res = []
    if query:
        shop_res = await asyncio.to_thread(_shop, query)
        print(f"Manual Shop: {len(shop_res)}")

    seen = set(); combined = []
    for x in lens_res + shop_res:
        if x["link"] not in seen: seen.add(x["link"]); combined.append(x)

    return {"success":True,"products":combined[:10],"lens_count":len(lens_res)}


@app.get("/api/health")
async def health():
    return {"status":"ok","serpapi":bool(SERPAPI_KEY),"anthropic":bool(ANTHROPIC_API_KEY)}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>FitFinder</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.css" rel="stylesheet">
<style>
:root{--bg:#0a0a0c;--card:#131315;--card2:#1c1c1f;--border:#222;--text:#f0ece4;--muted:#8a8880;--dim:#555;--accent:#d4a853;--green:#6fcf7c;--red:#e85d5d}
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

/* Cropper */
.crop-container{position:relative;width:100%;max-height:400px;margin:12px 0;border-radius:14px;overflow:hidden;background:#111}
.crop-container img{display:block;max-width:100%}

/* Hero */
.hero{border-radius:14px;overflow:hidden;background:var(--card);border:1px solid var(--green);margin-bottom:10px;position:relative}
.hero img{width:100%;height:200px;object-fit:cover;display:block}
.hero .badge{position:absolute;top:10px;left:10px;background:var(--green);color:var(--bg);font-size:10px;font-weight:800;padding:4px 10px;border-radius:6px}
.hero .info{padding:12px 14px}
.hero .t{font-size:14px;font-weight:700}
.hero .s{font-size:11px;color:var(--muted);margin-top:2px}
.hero .row{display:flex;align-items:center;justify-content:space-between;margin-top:8px}
.hero .price{font-size:20px;font-weight:800;color:var(--accent)}
.hero .btn{background:var(--green);color:var(--bg);border:none;border-radius:8px;padding:8px 16px;font:700 12px 'DM Sans',sans-serif;cursor:pointer}

/* Piece */
.piece{margin-bottom:28px;animation:fadeUp .4s ease both}
.p-hdr{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.p-thumb{width:52px;height:52px;border-radius:10px;object-fit:cover;border:2px solid var(--accent)}
.p-title{font-size:16px;font-weight:700}
.p-brand{font-size:9px;font-weight:700;color:var(--bg);background:var(--accent);padding:2px 7px;border-radius:4px;margin-left:6px}

/* Cards */
.scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}
.card{flex-shrink:0;width:135px;background:var(--card);border-radius:10px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text)}
.card.tr{border-color:var(--accent)}
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
  <!-- HOME -->
  <div id="home" style="padding:0 20px">
    <div style="padding-top:56px;padding-bottom:28px">
      <p style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:10px">FitFinder</p>
      <h1 style="font-size:32px;font-weight:700;line-height:1.15">Gorseldeki outfiti<br><span style="color:var(--accent)">birebir</span> bul.</h1>
      <p style="font-size:14px;color:var(--muted);margin-top:12px;line-height:1.5">Fotograf yukle, AI parcalari tespit etsin<br>veya kendin sec, Google Lens bulsun.</p>
    </div>
    <div onclick="document.getElementById('fi').click()" style="background:var(--accent);border-radius:14px;padding:18px 24px;display:flex;align-items:center;gap:14px;cursor:pointer;margin-bottom:16px">
      <div style="font-size:24px">&#x1F4F7;</div>
      <div><div style="font-size:16px;font-weight:700;color:var(--bg)">Fotograf Yukle</div><div style="font-size:12px;color:rgba(0,0,0,.45)">Galeri veya screenshot</div></div>
    </div>
    <input type="file" id="fi" accept="image/*" style="display:none">
    <div style="margin-top:32px;display:flex;flex-direction:column;gap:14px;padding-bottom:100px">
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px">&#x1F916;</span><div><div style="font-size:13px;font-weight:600">Otomatik Tara</div><div style="font-size:11px;color:var(--muted)">AI tum parcalari tespit edip arar</div></div></div>
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px">&#x2702;&#xFE0F;</span><div><div style="font-size:13px;font-weight:600">Kendim Seceyim</div><div style="font-size:11px;color:var(--muted)">Parmaginla parcayi sec, birebir bul</div></div></div>
      <div style="display:flex;gap:12px;align-items:center"><span style="font-size:20px">&#x1F1F9;&#x1F1F7;</span><div><div style="font-size:13px;font-weight:600">Turk Magazalar</div><div style="font-size:11px;color:var(--muted)">Trendyol, Zara TR, Bershka TR, H&amp;M TR</div></div></div>
    </div>
  </div>

  <!-- RESULT SCREEN -->
  <div id="rScreen" style="display:none">
    <div style="position:sticky;top:0;z-index:40;background:rgba(10,10,12,.9);backdrop-filter:blur(20px);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
      <div onclick="goHome()" style="cursor:pointer;color:var(--muted);font-size:13px">&#x2190; Geri</div>
      <div style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2.5px">FITFINDER</div>
      <div style="width:40px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <!-- Preview image -->
      <div style="border-radius:14px;overflow:hidden;margin:14px 0;position:relative;background:#111">
        <img id="prev" src="" style="width:100%;display:block;object-fit:cover;max-height:260px">
        <div style="position:absolute;inset:0;background:linear-gradient(transparent 50%,var(--bg));pointer-events:none"></div>
      </div>

      <!-- Two action buttons -->
      <div id="actionBtns" style="display:flex;flex-direction:column;gap:10px">
        <button class="btn-main btn-gold" onclick="autoScan()">&#x1F916; Otomatik Tara</button>
        <button class="btn-main btn-outline" onclick="startManual()">&#x2702;&#xFE0F; Kendim Seceyim</button>
      </div>

      <!-- Manual crop mode -->
      <div id="cropMode" style="display:none">
        <p style="font-size:13px;color:var(--accent);font-weight:600;margin-bottom:8px;text-align:center">&#x1F447; Aramak istedigin parcayi cercevele</p>
        <div class="crop-container"><img id="cropImg" src=""></div>
        <input id="manualQ" placeholder="Opsiyonel: ne aradigini yaz (ornek: siyah deri ceket)" style="width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--text);font:14px 'DM Sans',sans-serif;margin:10px 0">
        <button class="btn-main btn-green" onclick="cropAndSearch()">&#x1F50D; Bu Parcayi Bul</button>
        <button class="btn-main btn-outline" onclick="cancelManual()" style="margin-top:8px;font-size:13px">&#x2190; Vazgec</button>
      </div>

      <div id="ld" style="display:none"></div>
      <div id="err" style="display:none"></div>
      <div id="res" style="display:none"></div>
    </div>
  </div>

  <div class="bnav">
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer" onclick="goHome()"><div style="font-size:20px;color:var(--accent)">&#x2B21;</div><div style="font-size:10px;font-weight:600;color:var(--accent)">Kesfet</div></div>
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer"><div style="font-size:20px;color:var(--dim)">&#x2661;</div><div style="font-size:10px;font-weight:600;color:var(--dim)">Favoriler</div></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/cropper.min.js"></script>
<script>
var IC={hat:"\u{1F9E2}",sunglasses:"\u{1F576}",top:"\u{1F455}",jacket:"\u{1F9E5}",bag:"\u{1F45C}",accessory:"\u{1F48D}",watch:"\u{231A}",bottom:"\u{1F456}",dress:"\u{1F457}",shoes:"\u{1F45F}",scarf:"\u{1F9E3}"};
var cF=null, cPrev=null, cropper=null;

document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])loadF(e.target.files[0])});

function loadF(f){
  if(!f.type.startsWith('image/'))return;
  cF=f;
  var r=new FileReader();
  r.onload=function(e){cPrev=e.target.result;showScreen()};
  r.readAsDataURL(f);
}

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

function goHome(){
  document.getElementById('home').style.display='block';
  document.getElementById('rScreen').style.display='none';
  if(cropper){cropper.destroy();cropper=null}
  cF=null;cPrev=null;
}

// ─── AUTO MODE ───
function autoScan(){
  document.getElementById('actionBtns').style.display='none';
  showLoading('Parcalar tespit ediliyor...');
  var fd=new FormData();fd.append('file',cF);
  fetch('/api/full-analyze',{method:'POST',body:fd})
    .then(function(r){return r.json()})
    .then(function(d){hideLoading();if(!d.success)return showErr(d.message||'Hata');renderAuto(d)})
    .catch(function(e){hideLoading();showErr(e.message)});
}

// ─── MANUAL MODE ───
function startManual(){
  document.getElementById('actionBtns').style.display='none';
  document.getElementById('prev').style.display='none';
  document.getElementById('cropMode').style.display='block';
  document.getElementById('cropImg').src=cPrev;
  document.getElementById('manualQ').value='';

  setTimeout(function(){
    var img=document.getElementById('cropImg');
    if(cropper)cropper.destroy();
    cropper=new Cropper(img,{
      viewMode:1,
      dragMode:'move',
      autoCropArea:0.5,
      responsive:true,
      background:false,
      guides:true,
      highlight:true,
      cropBoxMovable:true,
      cropBoxResizable:true,
    });
  },100);
}

function cancelManual(){
  if(cropper){cropper.destroy();cropper=null}
  document.getElementById('cropMode').style.display='none';
  document.getElementById('prev').style.display='block';
  document.getElementById('actionBtns').style.display='flex';
}

function cropAndSearch(){
  if(!cropper)return;
  var canvas=cropper.getCroppedCanvas({maxWidth:800,maxHeight:800});
  if(!canvas)return;

  document.getElementById('cropMode').style.display='none';
  document.getElementById('prev').style.display='block';
  showLoading('Sectigin parca araniyor...');

  canvas.toBlob(function(blob){
    var q=document.getElementById('manualQ').value.trim();
    var fd=new FormData();
    fd.append('file',blob,'crop.jpg');
    fd.append('query',q);
    fetch('/api/manual-search',{method:'POST',body:fd})
      .then(function(r){return r.json()})
      .then(function(d){hideLoading();if(!d.success)return showErr('Hata');renderManual(d,canvas.toDataURL('image/jpeg',0.7))})
      .catch(function(e){hideLoading();showErr(e.message)});
  },'image/jpeg',0.85);

  if(cropper){cropper.destroy();cropper=null}
}

// ─── LOADING / ERROR ───
function showLoading(t){
  var l=document.getElementById('ld');l.style.display='block';
  l.innerHTML='<div style="display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:16px;border:1px solid var(--border);margin:14px 0"><div style="width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div style="font-size:13px;font-weight:600">'+t+'</div></div>';
}
function hideLoading(){document.getElementById('ld').style.display='none'}
function showErr(m){var e=document.getElementById('err');e.style.display='block';e.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px;margin:12px 0;font-size:13px;color:var(--red)">'+m+'</div>'}

// ─── RENDER AUTO ───
function renderAuto(d){
  document.getElementById('prev').style.maxHeight='160px';
  var pieces=d.pieces||[],unm=d.lens_unmatched||[];
  var ra=document.getElementById('res');ra.style.display='block';
  var h='';

  // ─── Unmatched Lens at top ───
  if(unm.length>0){
    h+='<div style="margin-bottom:20px">';
    h+='<div style="font-size:13px;font-weight:700;color:var(--green);margin-bottom:10px">&#x1F3AF; Gorsel Eslesme</div>';
    h+=heroHTML(unm[0],true);
    if(unm.length>1)h+=altsHTML(unm.slice(1));
    h+='</div>';
  }

  // ─── PIECES ───
  if(pieces.length>0){
    h+='<div style="font-size:11px;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:14px">&#x1F455; Parca Bazli Sonuclar</div>';
  }

  for(var i=0;i<pieces.length;i++){
    var p=pieces[i],pr=p.products||[],lc=p.lens_count||0;
    var hero=pr[0],alts=pr.slice(1);
    h+='<div class="piece" style="animation-delay:'+(i*.1)+'s">';

    h+='<div class="p-hdr">';
    h+='<div style="width:52px;height:52px;border-radius:10px;background:var(--card);display:flex;align-items:center;justify-content:center;font-size:22px;border:2px solid '+(lc>0?'var(--green)':'var(--border)')+'">'+(IC[p.category]||'')+'</div>';
    h+='<div><span class="p-title">'+(p.short_title_tr||p.category)+'</span>';
    if(p.brand&&p.brand!=='?')h+='<span class="p-brand">'+p.brand+'</span>';
    var vt=p.visible_text||'';
    if(vt&&vt.toLowerCase()!=='none')h+='<div style="font-size:10px;color:var(--accent);font-style:italic;margin-top:2px">"'+vt+'"</div>';
    if(lc>0)h+='<div style="font-size:9px;color:var(--green);margin-top:1px">&#x1F3AF; '+lc+' Lens eslesmesi</div>';
    h+='</div></div>';

    if(!hero){h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">Urun bulunamadi</div></div>';continue}

    h+=heroHTML(hero,lc>0);
    if(alts.length>0)h+=altsHTML(alts);
    h+='</div>';
  }

  if(!pieces.length&&!unm.length){
    h='<div style="text-align:center;padding:40px;color:var(--dim)">Sonuc bulunamadi. "Kendim Seceyim" ile dene!</div>';
  }
  ra.innerHTML=h;
  ra.innerHTML+='<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">&#x2702;&#xFE0F; Kendim Seceyim ile Tekrar Dene</button>';
}

// ─── RENDER MANUAL ───
function renderManual(d,cropSrc){
  document.getElementById('prev').style.maxHeight='160px';
  var pr=d.products||[];
  var ra=document.getElementById('res');ra.style.display='block';
  var h='';

  h+='<div class="p-hdr" style="margin-bottom:14px"><img class="p-thumb" src="'+cropSrc+'"><div><span class="p-title">Sectigin Parca</span>';
  if(d.lens_count>0)h+='<div style="font-size:10px;color:var(--green);margin-top:2px">'+d.lens_count+' Lens eslesmesi</div>';
  h+='</div></div>';

  if(pr.length>0){
    h+=heroHTML(pr[0],d.lens_count>0);
    if(pr.length>1)h+=altsHTML(pr.slice(1));
  } else {
    h+='<div style="background:var(--card);border-radius:10px;padding:16px;text-align:center;color:var(--dim);font-size:12px">Urun bulunamadi</div>';
  }
  h+='<button class="btn-main btn-outline" onclick="showScreen()" style="margin-top:20px">&#x2702;&#xFE0F; Baska Parca Sec</button>';
  ra.innerHTML=h;
}

// ─── SHARED HTML BUILDERS ───
function heroHTML(p,isLens){
  var h='<a href="'+p.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)"><div class="hero">';
  if(p.thumbnail)h+='<img src="'+p.thumbnail+'" onerror="this.hidden=true">';
  h+='<div class="badge">'+(isLens?'&#x1F3AF; Lens Eslesmesi':'&#x2728; Onerilen')+'</div>';
  h+='<div class="info"><div class="t">'+p.title+'</div><div class="s">'+(p.brand||p.source||'')+'</div>';
  h+='<div class="row"><span class="price">'+(p.price||'Fiyat icin tikla')+'</span>';
  h+='<button class="btn">Magazaya Git &#x2197;</button></div></div></div></a>';
  return h;
}

function altsHTML(list){
  var h='<div style="font-size:11px;color:var(--dim);margin:6px 0">&#x1F4B8; Alternatifler &#x1F449;</div><div class="scroll">';
  for(var i=0;i<list.length;i++){
    var a=list[i];
    h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="card'+(a.is_tr?' tr':'')+'">';
    if(a.thumbnail)h+='<img src="'+a.thumbnail+'" onerror="this.hidden=true">';
    h+='<div class="ci"><div class="cn">'+a.title+'</div><div class="cs">'+(a.brand||a.source)+'</div><div class="cp">'+(a.price||'\u2014')+'</div></div></a>';
  }
  h+='</div>';return h;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
