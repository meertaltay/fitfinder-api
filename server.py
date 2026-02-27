import os
import re
import io
import json
import base64
import asyncio
import httpx
from PIL import Image
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from serpapi import GoogleSearch

app = FastAPI(title="FitFinder API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BRAND_MAP = {
    "trendyol.com": "Trendyol", "hepsiburada.com": "Hepsiburada",
    "boyner.com.tr": "Boyner", "defacto.com": "DeFacto",
    "lcwaikiki.com": "LC Waikiki", "koton.com": "Koton",
    "beymen.com": "Beymen", "n11.com": "N11", "flo.com.tr": "FLO",
    "mavi.com": "Mavi", "superstep.com.tr": "Superstep",
    "zara.com": "Zara", "bershka.com": "Bershka",
    "pullandbear.com": "Pull&Bear", "hm.com": "H&M",
    "mango.com": "Mango", "asos.com": "ASOS",
    "nike.com": "Nike", "adidas.com": "Adidas", "puma.com": "Puma",
    "newbalance.com": "New Balance", "converse.com": "Converse",
    "stradivarius.com": "Stradivarius", "massimodutti.com": "Massimo Dutti",
}

BLOCKED_DOMAINS = [
    "pinterest.", "instagram.", "facebook.", "twitter.", "x.com",
    "tiktok.", "youtube.", "reddit.", "tumblr.", "blogspot.",
    "wordpress.", "medium.com", "threads.net", "etsy.com",
    "ebay.com", "ebay.", "amazon.com", "aliexpress.",
    "wish.com", "dhgate.", "alibaba.", "komo.", "novosti.",
    "naver.", "daum.net", "flickr.", "weheartit.",
]


def detect_brand(link, source):
    combined = (link + " " + source).lower()
    for domain, brand in BRAND_MAP.items():
        if domain in combined:
            return brand
    return source if source else ""


def is_tr_store(link, source):
    tr = ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.",
          "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep.",
          "sneakscloud.", "vakko.", "ipekyol.", "network."]
    combined = (link + " " + source).lower()
    return any(d in combined for d in tr)


def is_blocked(link):
    return any(d in link.lower() for d in BLOCKED_DOMAINS)


def crop_image(image_bytes, box_2d):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        ymin, xmin, ymax, xmax = box_2d
        left = max(0, (xmin / 100) * w - (xmax - xmin) / 100 * w * 0.05)
        top = max(0, (ymin / 100) * h - (ymax - ymin) / 100 * h * 0.05)
        right = min(w, (xmax / 100) * w + (xmax - xmin) / 100 * w * 0.05)
        bottom = min(h, (ymax / 100) * h + (ymax - ymin) / 100 * h * 0.05)
        cropped = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        print(f"Crop error: {e}")
        return None


def crop_to_base64(image_bytes, box_2d):
    """Crop and return as base64 data URI for frontend preview"""
    cropped = crop_image(image_bytes, box_2d)
    if cropped:
        return "data:image/jpeg;base64," + base64.b64encode(cropped).decode()
    return None


async def upload_image(image_bytes):
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": ("image.jpg", image_bytes, "image/jpeg")},
            )
            if r.status_code == 200 and r.text.startswith("http"):
                return r.text.strip()
        except Exception as e:
            print(f"Catbox error: {e}")
        try:
            r = await client.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": ("image.jpg", image_bytes, "image/jpeg")},
            )
            if r.status_code == 200:
                url = r.json().get("data", {}).get("url", "")
                if url:
                    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
        except Exception as e:
            print(f"tmpfiles error: {e}")
    return None


async def detect_pieces(image_b64):
    if not ANTHROPIC_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                            {"type": "text", "text": """You are a fashion expert. Analyze EVERY clothing item and accessory worn.

RULES:
1. READ ALL visible text, logos, patches on each item
2. Provide PRECISE bounding box for the VISIBLE PORTION only
3. For OVERLAPPING items: crop ONLY the visible part. Two pieces must NEVER have overlapping boxes.

For EACH piece:
- category: jacket|top|bottom|dress|shoes|bag|hat|sunglasses|watch|accessory|scarf
- description: Detailed English description (for search engine, user won't see this)
- short_title_tr: MAX 3-4 words Turkish title for display (e.g. "Kahverengi Deri Ceket", "Gri Oversize Hoodie", "Mavi Baggy Jean")
- color: Specific color in English
- brand: ONLY if you can READ it. Otherwise "?"
- visible_text: ALL readable text on item
- box_2d: [ymin, xmin, ymax, xmax] percentages 0-100. VISIBLE portion only!
- search_query: Turkish search query 3-5 words (e.g. "erkek kahverengi deri bomber ceket")

RESPOND WITH ONLY A JSON ARRAY:
[{"category":"...","description":"...","short_title_tr":"...","color":"...","brand":"...","visible_text":"...","box_2d":[0,0,0,0],"search_query":"..."}]"""},
                        ],
                    }],
                },
            )
            data = r.json()
            if "error" in data:
                print(f"Claude error: {data['error']}")
                return None
            text = data.get("content", [{}])[0].get("text", "")
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as e:
            print(f"Claude error: {type(e).__name__}: {e}")
    return None


def _search_lens_sync(image_url):
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
            if not link or link in seen or not title:
                continue
            if is_blocked(link):
                continue
            seen.add(link)
            source = match.get("source", "")
            price_info = match.get("price", {})
            products.append({
                "title": title, "brand": detect_brand(link, source),
                "source": source, "link": link,
                "price": price_info.get("value", ""),
                "thumbnail": match.get("thumbnail", ""),
                "is_tr": is_tr_store(link, source),
            })
            if len(products) >= 8:
                break
    except Exception as e:
        print(f"Lens error: {type(e).__name__}: {e}")
    return products


def _search_shopping_sync(query, limit=6):
    products = []
    seen = set()
    try:
        search = GoogleSearch({
            "engine": "google_shopping", "q": query,
            "gl": "tr", "hl": "tr", "api_key": SERPAPI_KEY,
        })
        data = search.get_dict()
        if "error" in data:
            print(f"  Shopping ERROR: {data['error']}")
            return []
        for item in data.get("shopping_results", []):
            link = item.get("product_link") or item.get("link") or ""
            title = item.get("title", "")
            if not link or link in seen or not title:
                continue
            if is_blocked(link):
                continue
            seen.add(link)
            source = item.get("source", "")
            products.append({
                "title": title, "brand": detect_brand(link, source),
                "source": source, "link": link,
                "price": item.get("price", item.get("extracted_price", "")),
                "thumbnail": item.get("thumbnail", ""),
                "is_tr": is_tr_store(link, source),
            })
            if len(products) >= limit:
                break
    except Exception as e:
        print(f"  Shopping error: {type(e).__name__}: {e}")
    return products


async def process_piece(piece, image_bytes):
    category = piece.get("category", "")
    box_2d = piece.get("box_2d")
    search_q = piece.get("search_query", "")
    products = []

    # Crop preview for frontend
    crop_preview = None
    if box_2d and len(box_2d) == 4:
        crop_preview = crop_to_base64(image_bytes, box_2d)

    # A) Crop + Google Lens
    if box_2d and len(box_2d) == 4:
        ymin, xmin, ymax, xmax = box_2d
        box_area = (ymax - ymin) * (xmax - xmin)
        if box_area >= 300:
            cropped = crop_image(image_bytes, box_2d)
            if cropped:
                cropped_url = await upload_image(cropped)
                if cropped_url:
                    print(f"[{category}] Lens search...")
                    products = await asyncio.to_thread(_search_lens_sync, cropped_url)
                    print(f"[{category}] Lens: {len(products)} results")

    # B) Supplement with TR results if needed
    if not search_q:
        parts = [p for p in [piece.get("brand", ""), piece.get("color", ""), piece.get("description", "")] if p and p != "?"]
        search_q = " ".join(parts).strip()

    tr_count = sum(1 for p in products if p.get("is_tr"))

    if not products:
        if search_q:
            print(f"[{category}] Text search: '{search_q}'")
            products = await asyncio.to_thread(_search_shopping_sync, search_q)
    elif tr_count < 2:
        if search_q:
            print(f"[{category}] Adding TR results...")
            tr_products = await asyncio.to_thread(_search_shopping_sync, search_q)
            tr_only = [p for p in tr_products if p.get("is_tr")]
            products = tr_only[:3] + products[:5]

    return {
        "category": category,
        "short_title_tr": piece.get("short_title_tr", category.title()),
        "description": piece.get("description", ""),
        "color": piece.get("color", ""),
        "brand": piece.get("brand", ""),
        "visible_text": piece.get("visible_text", ""),
        "crop_preview": crop_preview,
        "products": products,
    }


@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    contents = await file.read()
    image_b64 = base64.b64encode(contents).decode()

    print("=== Analysis started ===")
    pieces = await detect_pieces(image_b64)

    if not pieces:
        image_url = await upload_image(contents)
        lens = []
        if image_url:
            lens = await asyncio.to_thread(_search_lens_sync, image_url)
        return {"success": True, "lens_results": lens, "pieces": []}

    print(f"Detected {len(pieces)} pieces")
    tasks = [process_piece(p, contents) for p in pieces]
    piece_results = await asyncio.gather(*tasks)

    return {"success": True, "pieces": list(piece_results)}


@app.get("/api/health")
async def health():
    return {"status": "ok", "serpapi": bool(SERPAPI_KEY), "anthropic": bool(ANTHROPIC_API_KEY)}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PAGE


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>FitFinder</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0c;--card:#131315;--card2:#1a1a1d;--border:#1f1f23;--text:#f0ece4;--muted:#7a7872;--dim:#4a4843;--accent:#d4a853;--accent2:#c49b45;--green:#6fcf7c;--red:#e85d5d}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;min-height:100dvh;display:flex;justify-content:center}
::-webkit-scrollbar{display:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
.app{width:100%;max-width:440px;min-height:100vh;position:relative;overflow-x:hidden}

/* Header */
.hdr{position:sticky;top:0;z-index:40;background:rgba(10,10,12,.9);backdrop-filter:blur(20px);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}

/* Upload */
.upload-area{margin:12px 0;border:2px dashed var(--border);border-radius:16px;padding:40px 20px;text-align:center;cursor:pointer;transition:border-color .2s}
.upload-area:active{border-color:var(--accent)}

/* Scan button */
.scan-btn{background:var(--accent);border:none;border-radius:14px;padding:16px;width:100%;font-size:16px;font-weight:700;color:var(--bg);font-family:'DM Sans',sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:10px}

/* Piece section */
.piece-section{margin-bottom:32px;animation:fadeUp .4s ease both}
.piece-header-row{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.crop-thumb{width:56px;height:56px;border-radius:12px;object-fit:cover;border:2px solid var(--accent);flex-shrink:0}
.crop-thumb-placeholder{width:56px;height:56px;border-radius:12px;background:var(--card);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0}
.piece-title{font-size:18px;font-weight:700;font-family:'Cormorant Garamond',serif}
.piece-brand{font-size:10px;font-weight:700;color:var(--bg);background:var(--accent);padding:2px 8px;border-radius:4px;display:inline-block;margin-top:3px}

/* Hero product */
.hero-card{border-radius:16px;overflow:hidden;background:var(--card);border:1px solid var(--accent);position:relative;margin-bottom:12px}
.hero-card img{width:100%;height:220px;object-fit:cover;display:block}
.hero-badge{position:absolute;top:12px;left:12px;background:rgba(212,168,83,.95);color:var(--bg);font-size:10px;font-weight:800;padding:4px 10px;border-radius:6px;letter-spacing:.5px}
.hero-info{padding:14px 16px}
.hero-title{font-size:15px;font-weight:700;line-height:1.3}
.hero-row{display:flex;align-items:center;justify-content:space-between;margin-top:10px}
.hero-price{font-size:22px;font-weight:800;color:var(--accent);font-family:'DM Sans',sans-serif}
.hero-btn{background:var(--accent);color:var(--bg);border:none;border-radius:10px;padding:10px 20px;font-size:13px;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif}

/* Alternatives carousel */
.alt-label{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.alt-scroll{display:flex;gap:10px;overflow-x:auto;padding-bottom:8px;scroll-snap-type:x mandatory}
.alt-card{flex-shrink:0;width:140px;scroll-snap-align:start;background:var(--card);border-radius:12px;border:1px solid var(--border);overflow:hidden;text-decoration:none;color:var(--text)}
.alt-card img{width:140px;height:120px;object-fit:cover;display:block}
.alt-card .info{padding:8px 10px}
.alt-card .name{font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alt-card .store{font-size:9px;color:var(--dim);margin-top:2px}
.alt-card .price{font-size:14px;font-weight:700;color:var(--accent);margin-top:4px}
.alt-tr{border-color:var(--accent) !important}

/* Loading */
.skel{border-radius:8px;background:linear-gradient(90deg,var(--card) 25%,var(--card2) 50%,var(--card) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite}

.bottom-nav{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:440px;background:rgba(10,10,12,.93);backdrop-filter:blur(20px);border-top:1px solid var(--border);display:flex;padding:8px 0 22px;z-index:50}
.nav-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}
</style>
</head>
<body>
<div class="app" id="app">
  <!-- HOME -->
  <div id="screen-home">
    <div style="padding:0 20px">
      <div style="padding-top:56px;padding-bottom:32px">
        <p style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:12px">FitFinder</p>
        <h1 style="font-size:36px;font-weight:700;font-family:'Cormorant Garamond',serif;line-height:1.1">Gorseldeki outfiti<br><em style="color:var(--accent)">birebir</em> bul.</h1>
        <p style="font-size:14px;color:var(--muted);margin-top:14px;line-height:1.6">Fotograf yukle. AI her parcayi kirpip<br>Google Lens ile tam urununu buluyor.</p>
      </div>
      <div class="upload-area" id="uploadArea" onclick="document.getElementById('fi').click()">
        <div style="font-size:40px;margin-bottom:12px">&#x1F4F7;</div>
        <div style="font-size:16px;font-weight:700;color:var(--text)">Fotograf Yukle</div>
        <div style="font-size:13px;color:var(--dim);margin-top:6px">veya surukle birak</div>
      </div>
      <input type="file" id="fi" accept="image/*" style="display:none">
      <div style="margin-top:40px;display:flex;flex-direction:column;gap:20px;padding-bottom:100px">
        <div style="display:flex;gap:14px;align-items:center"><div style="font-size:24px">&#x2702;&#xFE0F;</div><div><div style="font-size:14px;font-weight:600">Kirp + Google Lens</div><div style="font-size:12px;color:var(--muted)">Her parca ayri ayri gorsel aranir</div></div></div>
        <div style="display:flex;gap:14px;align-items:center"><div style="font-size:24px">&#x1F3AF;</div><div><div style="font-size:14px;font-weight:600">Tam Eslesme + Muadiller</div><div style="font-size:12px;color:var(--muted)">Birebir urun + butce dostu alternatifler</div></div></div>
        <div style="display:flex;gap:14px;align-items:center"><div style="font-size:24px">&#x1F1F9;&#x1F1F7;</div><div><div style="font-size:14px;font-weight:600">Turk Magazalar</div><div style="font-size:12px;color:var(--muted)">Trendyol, Hepsiburada, Boyner, Zara TR...</div></div></div>
      </div>
    </div>
  </div>

  <!-- RESULTS -->
  <div id="screen-results" style="display:none">
    <div class="hdr">
      <div onclick="goHome()" style="cursor:pointer;color:var(--muted);font-size:13px">&#x2190; Geri</div>
      <div style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2.5px">FITFINDER</div>
      <div style="width:40px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <div style="border-radius:16px;overflow:hidden;margin:16px 0;position:relative;background:#111">
        <img id="prev" src="" style="width:100%;display:block;object-fit:cover;max-height:280px;transition:max-height .4s">
        <div style="position:absolute;inset:0;background:linear-gradient(transparent 50%,rgba(10,10,12,.95));pointer-events:none"></div>
        <div id="badge" style="display:none;position:absolute;bottom:14px;left:16px;background:rgba(0,0,0,.7);backdrop-filter:blur(8px);border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;color:var(--green)"></div>
      </div>
      <button id="scanBtn" class="scan-btn" onclick="analyze()">&#x2702;&#xFE0F; Kirp &amp; Tara &amp; Bul</button>
      <div id="loading" style="display:none"></div>
      <div id="error" style="display:none"></div>
      <div id="results" style="display:none"></div>
    </div>
  </div>

  <div class="bottom-nav">
    <div class="nav-item" onclick="goHome()"><div style="font-size:20px;color:var(--accent)">&#x2B21;</div><div style="font-size:10px;font-weight:600;color:var(--accent)">Kesfet</div></div>
    <div class="nav-item"><div style="font-size:20px;color:var(--dim)">&#x2661;</div><div style="font-size:10px;font-weight:600;color:var(--dim)">Favoriler</div></div>
  </div>
</div>

<script>
var curFile=null,curPrev=null;
document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])handleFile(e.target.files[0])});
var ua=document.getElementById('uploadArea');
ua.addEventListener('dragover',function(e){e.preventDefault();ua.style.borderColor='var(--accent)'});
ua.addEventListener('dragleave',function(){ua.style.borderColor='var(--border)'});
ua.addEventListener('drop',function(e){e.preventDefault();ua.style.borderColor='var(--border)';if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0])});

function handleFile(f){
  if(!f.type.startsWith('image/'))return;
  curFile=f;
  var r=new FileReader();
  r.onload=function(e){curPrev=e.target.result;showResults()};
  r.readAsDataURL(f);
}
function showResults(){
  document.getElementById('screen-home').style.display='none';
  document.getElementById('screen-results').style.display='block';
  var p=document.getElementById('prev');p.src=curPrev;p.style.maxHeight='280px';
  document.getElementById('scanBtn').style.display='flex';
  document.getElementById('loading').style.display='none';
  document.getElementById('error').style.display='none';
  document.getElementById('results').style.display='none';
  document.getElementById('badge').style.display='none';
}
function goHome(){
  document.getElementById('screen-home').style.display='block';
  document.getElementById('screen-results').style.display='none';
  curFile=null;curPrev=null;
}
function analyze(){
  if(!curFile)return;
  document.getElementById('scanBtn').style.display='none';
  var ld=document.getElementById('loading');
  ld.style.display='block';
  ld.innerHTML='<div style="display:flex;align-items:center;gap:14px;background:var(--card);border-radius:14px;padding:18px 20px;border:1px solid var(--border);margin:16px 0"><div style="width:28px;height:28px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div><div style="font-size:14px;font-weight:600">Analiz ediliyor...</div><div style="font-size:11px;color:var(--muted);margin-top:2px">Parcalar kirpiliyor, Lens ile araniyor</div></div></div>';
  var fd=new FormData();fd.append('file',curFile);
  fetch('/api/full-analyze',{method:'POST',body:fd})
    .then(function(r){return r.json()})
    .then(function(d){ld.style.display='none';if(!d.success){showErr(d.message||'Hata');return}render(d)})
    .catch(function(e){ld.style.display='none';showErr(e.message)});
}
function showErr(m){
  var el=document.getElementById('error');el.style.display='block';
  el.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px 16px;margin:12px 0;font-size:13px;color:var(--red)">'+m+'<div onclick="analyze()" style="margin-top:10px;padding:8px 16px;background:var(--card);border-radius:8px;display:inline-block;cursor:pointer;font-size:12px;color:var(--accent);font-weight:600;border:1px solid rgba(212,168,83,.2)">Tekrar Dene</div></div>';
}

function render(d){
  document.getElementById('prev').style.maxHeight='180px';
  var pieces=d.pieces||[];
  if(pieces.length>0){
    document.getElementById('badge').style.display='block';
    document.getElementById('badge').textContent=pieces.length+' parca bulundu';
  }
  var ra=document.getElementById('results');ra.style.display='block';
  var h='';

  for(var i=0;i<pieces.length;i++){
    var p=pieces[i];
    var prods=p.products||[];
    var hero=prods[0]||null;
    var alts=prods.slice(1);

    h+='<div class="piece-section" style="animation-delay:'+(i*.12)+'s">';

    // Header: crop preview + title
    h+='<div class="piece-header-row">';
    if(p.crop_preview){
      h+='<img class="crop-thumb" src="'+p.crop_preview+'">';
    } else {
      h+='<div class="crop-thumb-placeholder">&#x1F455;</div>';
    }
    h+='<div>';
    h+='<div class="piece-title">'+(p.short_title_tr||p.category)+'</div>';
    if(p.brand&&p.brand!=='?')h+='<span class="piece-brand">'+p.brand+'</span>';
    var vt=p.visible_text||'';
    if(vt&&vt.toLowerCase()!=='none'&&vt!=='')h+='<div style="font-size:10px;color:var(--accent);margin-top:2px;font-style:italic">"'+vt+'"</div>';
    h+='</div></div>';

    if(!hero){
      h+='<div style="background:var(--card);border-radius:12px;padding:20px;text-align:center;color:var(--dim);font-size:13px">Urun bulunamadi</div>';
      h+='</div>';
      continue;
    }

    // Hero card
    h+='<a href="'+hero.link+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text)">';
    h+='<div class="hero-card">';
    if(hero.thumbnail)h+='<img src="'+hero.thumbnail+'" onerror="this.hidden=true">';
    h+='<div class="hero-badge">&#x2728; Tam Eslesme</div>';
    h+='<div class="hero-info">';
    h+='<div class="hero-title">'+hero.title+'</div>';
    h+='<div style="margin-top:4px">';
    if(hero.brand)h+='<span style="font-size:11px;font-weight:700;color:'+(hero.is_tr?'var(--accent)':'var(--muted)')+'">'+hero.brand+'</span>';
    if(hero.source&&hero.source!==hero.brand)h+='<span style="font-size:10px;color:var(--dim);margin-left:6px">'+hero.source+'</span>';
    h+='</div>';
    h+='<div class="hero-row">';
    h+='<span class="hero-price">'+(hero.price||'Fiyat icin tikla')+'</span>';
    h+='<button class="hero-btn">Magazaya Git &#x2197;</button>';
    h+='</div></div></div></a>';

    // Alternatives
    if(alts.length>0){
      h+='<div class="alt-label">&#x1F4B8; Alternatifler &mdash; yana kaydir &#x1F449;</div>';
      h+='<div class="alt-scroll">';
      for(var j=0;j<alts.length;j++){
        var a=alts[j];
        h+='<a href="'+a.link+'" target="_blank" rel="noopener" class="alt-card'+(a.is_tr?' alt-tr':'')+'">';
        if(a.thumbnail)h+='<img src="'+a.thumbnail+'" onerror="this.hidden=true">';
        h+='<div class="info">';
        h+='<div class="name">'+a.title+'</div>';
        h+='<div class="store">'+(a.brand||a.source||'')+'</div>';
        h+='<div class="price">'+(a.price||'—')+'</div>';
        h+='</div></a>';
      }
      h+='</div>';
    }

    h+='</div>';
  }

  if(!pieces.length)h+='<div style="text-align:center;padding:40px 0;color:var(--dim)">Sonuc bulunamadi</div>';
  ra.innerHTML=h;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
