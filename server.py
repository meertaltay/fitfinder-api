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

# ─── Brand Detection ───
BRAND_MAP = {
    "trendyol.com": "TRENDYOL", "hepsiburada.com": "HEPSIBURADA",
    "boyner.com.tr": "BOYNER", "defacto.com": "DEFACTO",
    "lcwaikiki.com": "LC WAIKIKI", "koton.com": "KOTON",
    "beymen.com": "BEYMEN", "n11.com": "N11", "flo.com.tr": "FLO",
    "mavi.com": "MAVI", "superstep.com.tr": "SUPERSTEP",
    "zara.com": "ZARA", "bershka.com": "BERSHKA",
    "pullandbear.com": "PULL&BEAR", "hm.com": "H&M",
    "mango.com": "MANGO", "asos.com": "ASOS",
    "nike.com": "NIKE", "adidas.com": "ADIDAS", "puma.com": "PUMA",
    "newbalance.com": "NEW BALANCE", "converse.com": "CONVERSE",
}

BLOCKED_DOMAINS = [
    "pinterest.", "instagram.", "facebook.", "twitter.", "x.com",
    "tiktok.", "youtube.", "reddit.", "tumblr.", "blogspot.",
    "wordpress.", "medium.com",
]


def detect_brand(link, source):
    combined = (link + " " + source).lower()
    for domain, brand in BRAND_MAP.items():
        if domain in combined:
            return brand
    return source.upper() if source else ""


def is_tr_store(link, source):
    tr_domains = ["trendyol.", "hepsiburada.", "boyner.", "beymen.", "defacto.",
                   "lcwaikiki.", "koton.", "flo.", "n11.", "mavi.", "superstep.",
                   "sneakscloud.", "vakko.", "ipekyol.", "network."]
    combined = (link + " " + source).lower()
    return any(d in combined for d in tr_domains)


def is_blocked(link):
    return any(d in link.lower() for d in BLOCKED_DOMAINS)


# ─── Image Crop ───
def crop_image(image_bytes, box_2d):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        ymin, xmin, ymax, xmax = box_2d

        left = (xmin / 100) * w
        top = (ymin / 100) * h
        right = (xmax / 100) * w
        bottom = (ymax / 100) * h

        # Biraz padding ekle (daha iyi Lens sonucu icin)
        pad_x = (right - left) * 0.05
        pad_y = (bottom - top) * 0.05
        left = max(0, left - pad_x)
        top = max(0, top - pad_y)
        right = min(w, right + pad_x)
        bottom = min(h, bottom + pad_y)

        cropped = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        print(f"Crop error: {e}")
        return None


# ─── Image Upload (async) ───
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


# ─── Claude Vision - detect pieces with bounding boxes ───
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

CRITICAL RULES:
1. READ ALL visible text, logos, patches, embroidery on each item
2. Provide EXACT bounding box coordinates for each item
3. Be VERY specific about style, material, and details

For EACH piece:
- category: jacket|top|bottom|dress|shoes|bag|hat|sunglasses|watch|accessory|scarf
- description: Very detailed (garment type, material, fit, style details, visible text/logos)
- color: Specific color
- brand: ONLY write brand if you can READ it on the item. Otherwise write "?"
- visible_text: ALL text/words you can read on this item
- box_2d: Bounding box as [ymin, xmin, ymax, xmax] in percentages (0-100). Where ymin=top edge, xmin=left edge, ymax=bottom edge, xmax=right edge of the item in the image
- search_query: Best Turkish search query (3-5 words max)

RESPOND WITH ONLY A JSON ARRAY. NO OTHER TEXT:
[{"category":"...","description":"...","color":"...","brand":"...","visible_text":"...","box_2d":[0,0,0,0],"search_query":"..."}]"""},
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
            print(f"Claude detect error: {type(e).__name__}: {e}")
    return None


# ─── Google Lens search (sync) ───
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

    # Google Lens kendi benzerlik siralmasini kullan, BOZMA!
    return products


# ─── Google Shopping text search (sync, fallback) ───
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


# ─── Process single piece: Crop → Lens → fallback Shopping ───
async def process_piece(piece, image_bytes):
    category = piece.get("category", "")
    box_2d = piece.get("box_2d")
    search_q = piece.get("search_query", "")
    products = []

    # A) ANA YONTEM: Kirp + Google Lens (gorsel arama)
    if box_2d and len(box_2d) == 4:
        print(f"[{category}] Cropping... box: {box_2d}")
        cropped = crop_image(image_bytes, box_2d)
        if cropped:
            cropped_url = await upload_image(cropped)
            if cropped_url:
                print(f"[{category}] Lens searching cropped image...")
                products = await asyncio.to_thread(_search_lens_sync, cropped_url)
                print(f"[{category}] Lens found {len(products)} results")

    # B) FALLBACK: Google Shopping (metin arama)
    if not products:
        if not search_q:
            parts = [p for p in [piece.get("brand", ""), piece.get("color", ""), piece.get("description", "")] if p and p != "?"]
            search_q = " ".join(parts).strip()
        if search_q:
            print(f"[{category}] Text search fallback: '{search_q}'")
            products = await asyncio.to_thread(_search_shopping_sync, search_q)
            print(f"[{category}] Shopping found {len(products)} results")

    return {
        "category": category,
        "description": piece.get("description", ""),
        "color": piece.get("color", ""),
        "brand": piece.get("brand", ""),
        "visible_text": piece.get("visible_text", ""),
        "search_method": "lens" if (box_2d and len(box_2d) == 4 and products) else "text",
        "products": products,
    }


# ─── Main Endpoint ───
@app.post("/api/full-analyze")
async def full_analyze(file: UploadFile = File(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    contents = await file.read()
    image_b64 = base64.b64encode(contents).decode()

    # 1) Claude detect pieces (with bounding boxes)
    print("=== Starting analysis ===")
    pieces = await detect_pieces(image_b64)

    if not pieces:
        # Fallback: sadece full image Lens
        image_url = await upload_image(contents)
        lens_results = []
        if image_url:
            lens_results = await asyncio.to_thread(_search_lens_sync, image_url)
        return {"success": True, "lens_results": lens_results, "pieces": []}

    print(f"Claude detected {len(pieces)} pieces")
    for p in pieces:
        print(f"  {p.get('category')}: {p.get('description', '')[:50]} box={p.get('box_2d')}")

    # 2) Process ALL pieces in PARALLEL (crop + lens each)
    tasks = [process_piece(p, contents) for p in pieces]
    piece_results = await asyncio.gather(*tasks)

    # 3) Full image Lens (bonus results)
    image_url = await upload_image(contents)
    full_lens = []
    if image_url:
        full_lens = await asyncio.to_thread(_search_lens_sync, image_url)
        print(f"Full image Lens: {len(full_lens)} results")

    return {
        "success": True,
        "lens_results": full_lens,
        "pieces": list(piece_results),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "serpapi": bool(SERPAPI_KEY), "anthropic": bool(ANTHROPIC_API_KEY)}


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
:root{--bg:#0c0c0e;--card:#141416;--card2:#1a1a1e;--border:#222226;--text:#f0ece4;--muted:#7a7872;--dim:#4a4843;--accent:#d4a853;--accent-soft:rgba(212,168,83,.08);--accent-border:rgba(212,168,83,.2);--green:#6fcf7c;--red:#e85d5d}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;display:flex;justify-content:center}
::-webkit-scrollbar{display:none}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes pulse{0%,100%{opacity:.6}50%{opacity:1}}
.app{width:100%;max-width:440px;min-height:100vh;position:relative}
.header{position:sticky;top:0;z-index:40;background:rgba(12,12,14,.88);backdrop-filter:blur(20px);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.skeleton{border-radius:6px;background:linear-gradient(90deg,var(--card) 25%,var(--card2) 50%,var(--card) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite}
.product{display:flex;gap:12px;padding:12px;background:var(--card);border-radius:12px;border:1px solid var(--border);text-decoration:none;color:var(--text);animation:fadeUp .3s ease both}
.product img{width:68px;height:68px;border-radius:8px;object-fit:cover;background:var(--card2);flex-shrink:0}
.piece-header{display:flex;align-items:center;gap:12px;padding:14px 16px;background:var(--card);border:1px solid var(--accent-border);cursor:pointer}
.piece-header.open{border-radius:14px 14px 0 0;border-bottom:none}
.piece-header.closed{border-radius:14px}
.piece-body{background:var(--bg);border:1px solid var(--accent-border);border-top:none;border-radius:0 0 14px 14px;padding:12px;display:flex;flex-direction:column;gap:8px;animation:fadeIn .2s ease}
.scan-btn{background:var(--accent);border-radius:14px;padding:16px;display:flex;align-items:center;justify-content:center;gap:10px;cursor:pointer;border:none;width:100%;font-size:16px;font-weight:700;color:var(--bg);font-family:'DM Sans',sans-serif}
.upload-btn{background:var(--accent);border-radius:14px;padding:18px 24px;display:flex;align-items:center;gap:14px;cursor:pointer;margin-bottom:12px}
.bottom-nav{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:440px;background:rgba(12,12,14,.92);backdrop-filter:blur(20px);border-top:1px solid var(--border);display:flex;padding:8px 0 22px;z-index:50}
.nav-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}
.method-badge{font-size:8px;font-weight:700;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.5px}
.method-lens{background:#6fcf7c22;color:var(--green);border:1px solid #6fcf7c44}
.method-text{background:var(--accent-soft);color:var(--accent);border:1px solid var(--accent-border)}
</style>
</head>
<body>
<div class="app" id="app">
  <div id="screen-home">
    <div style="padding:0 20px">
      <div style="padding-top:56px;padding-bottom:40px">
        <p style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:3px;text-transform:uppercase;margin-bottom:12px">FitFinder</p>
        <h1 style="font-size:38px;font-weight:700;font-family:'Cormorant Garamond',serif;line-height:1.1">Komple outfiti<br><em style="color:var(--accent)">parca parca</em> bul.</h1>
        <p style="font-size:14px;color:var(--muted);margin-top:14px;line-height:1.6">Fotograf yukle. AI her parcayi kirpip<br>Google Lens ile birebir urun buluyor.</p>
      </div>
      <div class="upload-btn" onclick="document.getElementById('fi').click()">
        <div style="width:48px;height:48px;border-radius:12px;background:rgba(0,0,0,.15);display:flex;align-items:center;justify-content:center;font-size:24px">&#x1F4F7;</div>
        <div><div style="font-size:16px;font-weight:700;color:var(--bg)">Fotograf Yukle</div><div style="font-size:12px;color:rgba(0,0,0,.5);margin-top:2px">Galeri, kamera veya screenshot</div></div>
      </div>
      <div onclick="document.getElementById('fi').click()" style="border:2px dashed var(--border);border-radius:14px;padding:24px;text-align:center;cursor:pointer;color:var(--dim);font-size:13px">veya surukle &amp; birak</div>
      <input type="file" id="fi" accept="image/*" style="display:none">
      <div style="margin-top:48px;display:flex;flex-direction:column;gap:20px;padding-bottom:100px">
        <div style="display:flex;gap:14px;align-items:flex-start"><div style="font-size:18px;width:40px;height:40px;border-radius:10px;background:var(--accent-soft);border:1px solid var(--accent-border);display:flex;align-items:center;justify-content:center;flex-shrink:0">&#x2702;&#xFE0F;</div><div><div style="font-size:14px;font-weight:600">Kirp + Lens</div><div style="font-size:12px;color:var(--muted);margin-top:2px">Her parca kirpilip ayri ayri Google Lens'e gonderiliyor</div></div></div>
        <div style="display:flex;gap:14px;align-items:flex-start"><div style="font-size:18px;width:40px;height:40px;border-radius:10px;background:var(--accent-soft);border:1px solid var(--accent-border);display:flex;align-items:center;justify-content:center;flex-shrink:0">&#x1F3AF;</div><div><div style="font-size:14px;font-weight:600">Birebir Eslesme</div><div style="font-size:12px;color:var(--muted);margin-top:2px">Metin degil, gorsel benzerlik ile tam urun bulma</div></div></div>
        <div style="display:flex;gap:14px;align-items:flex-start"><div style="font-size:18px;width:40px;height:40px;border-radius:10px;background:var(--accent-soft);border:1px solid var(--accent-border);display:flex;align-items:center;justify-content:center;flex-shrink:0">&#x1F6D2;</div><div><div style="font-size:14px;font-weight:600">Direkt Satin Al</div><div style="font-size:12px;color:var(--muted);margin-top:2px">Zara, Bershka, Trendyol, H&amp;M ve daha fazlasi</div></div></div>
      </div>
    </div>
  </div>

  <div id="screen-results" style="display:none">
    <div class="header">
      <div onclick="goHome()" style="cursor:pointer;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px">&#x2190; Geri</div>
      <div style="font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2.5px;text-transform:uppercase">FitFinder</div>
      <div style="width:48px"></div>
    </div>
    <div style="padding:0 20px 120px">
      <div style="border-radius:16px;overflow:hidden;margin:16px 0;position:relative;background:#111">
        <img id="prev" src="" style="width:100%;display:block;object-fit:cover;max-height:360px;transition:max-height .4s">
        <div style="position:absolute;inset:0;background:linear-gradient(transparent 40%,rgba(12,12,14,.95));pointer-events:none"></div>
        <div id="badge" style="display:none;position:absolute;bottom:14px;left:16px;background:rgba(0,0,0,.7);backdrop-filter:blur(8px);border-radius:8px;padding:5px 12px;font-size:12px;font-weight:600;color:var(--green)"></div>
      </div>
      <button id="scanBtn" class="scan-btn" onclick="analyze()">&#x2702;&#xFE0F; Kirp &amp; Tara &amp; Bul</button>
      <div id="loading" style="display:none"></div>
      <div id="error" style="display:none"></div>
      <div id="results" style="display:none"></div>
    </div>
  </div>

  <div class="bottom-nav">
    <div class="nav-item" onclick="goHome()"><div style="font-size:20px;color:var(--accent)">&#x2B21;</div><div style="font-size:10px;font-weight:600;color:var(--accent);letter-spacing:.5px">Kesfet</div></div>
    <div class="nav-item"><div style="font-size:20px;color:var(--dim)">&#x2661;</div><div style="font-size:10px;font-weight:600;color:var(--dim);letter-spacing:.5px">Kaydedilenler</div></div>
  </div>
</div>

<script>
var ICONS={hat:"&#x1F9E2;",sunglasses:"&#x1F576;&#xFE0F;",top:"&#x1F455;",jacket:"&#x1F9E5;",bag:"&#x1F45C;",accessory:"&#x1F48D;",watch:"&#x231A;",bottom:"&#x1F456;",dress:"&#x1F457;",shoes:"&#x1F45F;",scarf:"&#x1F9E3;"};
var curFile=null,curPrev=null;

document.getElementById('fi').addEventListener('change',function(e){if(e.target.files[0])handleFile(e.target.files[0])});

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
  document.getElementById('prev').src=curPrev;
  document.getElementById('prev').style.maxHeight='360px';
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
  ld.innerHTML='<div style="display:flex;align-items:center;gap:14px;background:var(--card);border-radius:14px;padding:18px 20px;border:1px solid var(--border);margin-bottom:16px"><div style="width:28px;height:28px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite"></div><div><div style="font-size:14px;font-weight:600">Analiz ediliyor...</div><div style="font-size:11px;color:var(--muted);margin-top:2px">Parcalar kirpiliyor + Google Lens ile araniyor</div></div></div>';

  var fd=new FormData();
  fd.append('file',curFile);
  fetch('/api/full-analyze',{method:'POST',body:fd})
    .then(function(r){return r.json()})
    .then(function(d){ld.style.display='none';if(!d.success){showErr(d.message||'Hata');return}render(d)})
    .catch(function(e){ld.style.display='none';showErr(e.message)});
}

function showErr(m){
  var el=document.getElementById('error');
  el.style.display='block';
  el.innerHTML='<div style="background:rgba(232,93,93,.06);border:1px solid rgba(232,93,93,.15);border-radius:12px;padding:12px 16px;font-size:13px;color:var(--red)">&#x26A0; '+m+'<div onclick="analyze()" style="margin-top:10px;padding:8px 16px;background:var(--card);border-radius:8px;display:inline-block;cursor:pointer;font-size:12px;color:var(--accent);font-weight:600;border:1px solid var(--accent-border)">Tekrar Dene</div></div>';
}

function render(d){
  document.getElementById('prev').style.maxHeight='200px';
  var pieces=d.pieces||[],lens=d.lens_results||[];
  if(pieces.length>0){
    document.getElementById('badge').style.display='block';
    document.getElementById('badge').textContent=pieces.length+' parca tespit edildi';
  }
  var ra=document.getElementById('results');
  ra.style.display='block';
  var h='';

  if(pieces.length>0){
    h+='<div style="font-size:22px;font-weight:700;font-family:Cormorant Garamond,serif;margin-top:8px">Outfit Analizi</div>';
    h+='<div style="font-size:12px;color:var(--muted);margin-top:4px">&#x2702;&#xFE0F; Her parca kirpilip ayri ayri arandi</div>';
    h+='<div style="display:flex;flex-direction:column;gap:14px;margin-top:14px">';
    for(var i=0;i<pieces.length;i++){
      var p=pieces[i],icon=ICONS[p.category]||'&#x1F3F7;';
      var brandH=(p.brand&&p.brand!=='?')?'<span style="font-size:9px;font-weight:800;color:var(--bg);background:var(--accent);padding:2px 7px;border-radius:4px;margin-left:8px">'+p.brand.toUpperCase()+'</span>':'';
      var methodH=p.search_method==='lens'?'<span class="method-badge method-lens">&#x1F50D; LENS</span>':'<span class="method-badge method-text">&#x1F4DD; TEXT</span>';

      h+='<div style="animation:fadeUp .4s ease '+(i*.1)+'s both">';
      h+='<div class="piece-header open" id="ph-'+i+'" onclick="tog('+i+')">';
      h+='<span style="font-size:28px">'+icon+'</span><div style="flex:1">';
      h+='<div style="display:flex;align-items:center;gap:6px"><span style="font-size:10px;font-weight:700;color:var(--accent);letter-spacing:1.5px;text-transform:uppercase">'+p.category+'</span>'+brandH+' '+methodH+'</div>';
      h+='<div style="font-size:14px;font-weight:600;margin-top:2px">'+p.description+'</div>';
      var vt=p.visible_text||'';
      if(vt&&vt.toLowerCase()!=='none'&&vt!=='')h+='<div style="font-size:10px;color:var(--accent);margin-top:3px;font-style:italic">&#x1F50E; "'+vt+'"</div>';
      h+='<div style="font-size:11px;color:var(--muted);margin-top:2px">'+p.color+'</div></div>';
      var cnt=(p.products&&p.products.length>0)?'<span style="font-size:11px;color:var(--green);font-weight:600">'+p.products.length+' urun</span>':'';
      h+='<div style="display:flex;align-items:center;gap:8px">'+cnt+'<span style="font-size:16px;color:var(--muted);transform:rotate(90deg);transition:transform .2s" id="pa-'+i+'">&#x203A;</span></div></div>';

      h+='<div class="piece-body" id="pb-'+i+'">';
      if(p.products&&p.products.length>0){for(var j=0;j<p.products.length;j++)h+=prodHTML(p.products[j],j)}
      else{h+='<div style="font-size:12px;color:var(--dim);text-align:center;padding:16px">Urun bulunamadi</div>'}
      h+='</div></div>';
    }
    h+='</div>';
  }

  if(lens.length>0){
    h+='<div style="font-size:18px;font-weight:700;font-family:Cormorant Garamond,serif;margin-top:24px;margin-bottom:4px">&#x1F50D; Tum Fotograf - Lens</div>';
    h+='<div style="font-size:11px;color:var(--dim);margin-bottom:12px">Tam gorselden bulunan benzer urunler</div>';
    h+='<div style="display:flex;flex-direction:column;gap:8px">';
    for(var k=0;k<lens.length;k++)h+=prodHTML(lens[k],k);
    h+='</div>';
  }

  if(!pieces.length&&!lens.length)h+='<div style="text-align:center;padding:40px 0;color:var(--dim)">Sonuc bulunamadi</div>';
  ra.innerHTML=h;
}

function prodHTML(p,i){
  var thumb=p.thumbnail?'<img src="'+p.thumbnail+'" onerror="this.hidden=true">':'<div style="width:68px;height:68px;border-radius:8px;background:var(--card2);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0">'+(p.is_tr?'&#x1F1F9;&#x1F1F7;':'&#x1F6D2;')+'</div>';
  var sc=p.is_tr?'color:var(--bg);background:var(--accent)':'color:var(--text);background:var(--border)';
  var store='';
  if(p.brand)store+='<span style="font-size:9px;font-weight:800;padding:1px 6px;border-radius:3px;'+sc+'">'+p.brand+'</span> ';
  if(p.source)store+='<span style="font-size:9px;color:var(--dim)">'+p.source+'</span>';
  return '<a href="'+p.link+'" target="_blank" rel="noopener" class="product" style="animation-delay:'+(i*.05)+'s">'+thumb+'<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(p.title||'')+'</div><div style="margin-top:3px">'+store+'</div><div style="display:flex;align-items:center;justify-content:space-between;margin-top:6px"><span style="font-size:15px;font-weight:700;color:var(--accent)">'+(p.price||'—')+'</span><span style="font-size:10px;color:var(--accent);font-weight:600">Satin Al &#x2197;</span></div></div></a>';
}

function tog(i){
  var b=document.getElementById('pb-'+i),h=document.getElementById('ph-'+i),a=document.getElementById('pa-'+i);
  if(b.style.display==='none'){b.style.display='flex';h.className='piece-header open';a.style.transform='rotate(90deg)'}
  else{b.style.display='none';h.className='piece-header closed';a.style.transform='rotate(0)'}
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
