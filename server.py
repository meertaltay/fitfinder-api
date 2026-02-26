import os
import json
import requests as req
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from serpapi import GoogleSearch

app = FastAPI(title="FitFinder API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# ─── Marka Haritası (70+ marka) ───
BRAND_MAP = {
    # TR Mağazalar
    "trendyol.com": "TRENDYOL", "hepsiburada.com": "HEPSIBURADA",
    "boyner.com.tr": "BOYNER", "defacto.com": "DEFACTO",
    "lcwaikiki.com": "LC WAIKIKI", "koton.com": "KOTON",
    "network.com.tr": "NETWORK", "vakko.com": "VAKKO",
    "beymen.com": "BEYMEN", "morhipo.com": "MORHIPO",
    "n11.com": "N11", "flo.com.tr": "FLO",
    "sneakscloud.com": "SNEAKS CLOUD", "superstep.com.tr": "SUPERSTEP",
    "tozlu.com": "TOZLU", "modanisa.com": "MODANISA",
    "colins.com": "COLIN'S", "mavi.com": "MAVİ",
    "ipekyol.com": "İPEKYOL", "machka.com.tr": "MACHKA",
    "twist.com.tr": "TWIST", "yargici.com": "YARGICI",
    "derimod.com.tr": "DERİMOD", "kemal-tanca.com": "KEMAL TANCA",
    # Global Fast Fashion
    "zara.com": "ZARA", "bershka.com": "BERSHKA",
    "pullandbear.com": "PULL&BEAR", "stradivarius.com": "STRADIVARIUS",
    "massimodutti.com": "MASSIMO DUTTI", "hm.com": "H&M",
    "mango.com": "MANGO", "cos.com": "COS", "arket.com": "ARKET",
    "uniqlo.com": "UNIQLO", "gap.com": "GAP",
    "asos.com": "ASOS", "shein.com": "SHEIN",
    "aboutyou.com": "ABOUT YOU", "zalando.com": "ZALANDO",
    # Spor
    "nike.com": "NIKE", "adidas.com": "ADIDAS", "adidas.com.tr": "ADIDAS",
    "puma.com": "PUMA", "newbalance.com": "NEW BALANCE",
    "converse.com": "CONVERSE", "vans.com": "VANS",
    "reebok.com": "REEBOK", "underarmour.com": "UNDER ARMOUR",
    "northface.com": "THE NORTH FACE", "columbia.com": "COLUMBIA",
    # Lüks
    "gucci.com": "GUCCI", "louisvuitton.com": "LOUIS VUITTON",
    "prada.com": "PRADA", "burberry.com": "BURBERRY",
    "coach.com": "COACH", "calvinklein.com": "CALVIN KLEIN",
    "tommy.com": "TOMMY HILFIGER", "ralphlauren.com": "RALPH LAUREN",
    "lacoste.com": "LACOSTE", "hugoboss.com": "HUGO BOSS",
    "levi.com": "LEVIS", "diesel.com": "DIESEL",
    "armani.com": "ARMANI", "versace.com": "VERSACE",
    "balenciaga.com": "BALENCIAGA", "saintlaurent.com": "SAINT LAURENT",
}

# ─── TR Mağaza Öncelik Skorları ───
TR_STORE_DOMAINS = {
    "trendyol.com": 100, "hepsiburada.com": 95, "boyner.com": 90,
    "beymen.com": 88, "defacto.com": 85, "lcwaikiki.com": 85,
    "koton.com": 85, "flo.com": 82, "n11.com": 80,
    "morhipo.com": 78, "superstep.com": 78, "mavi.com": 85,
    "colins.com": 80, "ipekyol.com": 82, "network.com": 80,
    "vakko.com": 85, "sneakscloud.com": 78, "tozlu.com": 70,
    "modanisa.com": 75, "yargici.com": 80, "derimod.com": 78,
}

GLOBAL_STORE_PRIORITY = {
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


def get_store_score(link, source):
    """Mağaza öncelik skoru - TR mağazalar daha yüksek"""
    combined = (link + " " + source).lower()
    for domain, score in TR_STORE_DOMAINS.items():
        if domain in combined:
            return score
    for domain, score in GLOBAL_STORE_PRIORITY.items():
        if domain in combined:
            return score
    return 30


def sort_by_tr_priority(products):
    """Ürünleri TR mağaza önceliğine göre sırala"""
    return sorted(products, key=lambda p: get_store_score(p.get("link", ""), p.get("source", "")), reverse=True)


def upload_image(image_bytes):
    """Fotoğrafı geçici hosting'e yükle"""
    # 1) Catbox
    try:
        r = req.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except:
        pass
    # 2) tmpfiles
    try:
        r = req.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        if r.status_code == 200:
            url = r.json().get("data", {}).get("url", "")
            if url:
                return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except:
        pass
    # 3) file.io
    try:
        r = req.post("https://file.io", files={"file": ("image.jpg", image_bytes, "image/jpeg")}, timeout=30)
        if r.status_code == 200:
            return r.json().get("link", "")
    except:
        pass
    return None


def parse_shopping_results(results, limit=8):
    """SerpAPI shopping sonuçlarını parse et + TR önceliklendirme"""
    products = []
    seen = set()

    for item in results.get("shopping_results", []):
        link = item.get("link", "")
        title = item.get("title", "")
        if not link or link in seen or not title:
            continue
        seen.add(link)
        source = item.get("source", "")
        products.append({
            "title": title,
            "brand": detect_brand(link, source),
            "source": source,
            "link": link,
            "price": item.get("price", ""),
            "thumbnail": item.get("thumbnail", ""),
        })
        if len(products) >= limit:
            break

    return sort_by_tr_priority(products)


def parse_lens_results(results, limit=10):
    """Google Lens visual_matches parse et + TR önceliklendirme"""
    products = []
    seen = set()
    for match in results.get("visual_matches", []):
        link = match.get("link", "")
        title = match.get("title", "")
        if not link or link in seen or not title:
            continue
        seen.add(link)
        source = match.get("source", "")
        price_info = match.get("price", {})
        products.append({
            "title": title,
            "brand": detect_brand(link, source),
            "source": source,
            "link": link,
            "price": price_info.get("value", ""),
            "thumbnail": match.get("thumbnail", ""),
        })
        if len(products) >= limit:
            break

    return sort_by_tr_priority(products)


# ─── Endpoint 1: Google Lens (görsel arama) ───
@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    contents = await file.read()
    image_url = upload_image(contents)
    if not image_url:
        raise HTTPException(500, "Image upload failed")

    try:
        search = GoogleSearch({
            "engine": "google_lens",
            "url": image_url,
            "api_key": SERPAPI_KEY,
            "hl": "tr",
            "country": "tr",
        })
        results = search.get_dict()
        if "error" in results:
            raise HTTPException(500, "SerpAPI: " + str(results["error"]))

        products = parse_lens_results(results)
        return {"success": True, "count": len(products), "products": products}
    except Exception as e:
        if "HTTPException" in type(e).__name__:
            raise
        raise HTTPException(500, str(e))


# ─── Endpoint 2: Google Shopping (tek parça arama) ───
@app.post("/api/search-piece")
async def search_piece(query: str = Form(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    try:
        # Önce TR arama
        search = GoogleSearch({
            "engine": "google_shopping",
            "q": query,
            "gl": "tr",
            "hl": "tr",
            "api_key": SERPAPI_KEY,
        })
        results = search.get_dict()
        if "error" in results:
            raise HTTPException(500, "SerpAPI: " + str(results["error"]))

        products = parse_shopping_results(results)

        # TR sonuç az geldiyse global ekle
        tr_count = sum(1 for p in products if get_store_score(p["link"], p["source"]) >= 70)
        if tr_count < 2:
            try:
                search_global = GoogleSearch({
                    "engine": "google_shopping",
                    "q": query,
                    "gl": "us",
                    "hl": "en",
                    "api_key": SERPAPI_KEY,
                })
                global_results = search_global.get_dict()
                global_products = parse_shopping_results(global_results)
                existing_links = {p["link"] for p in products}
                for gp in global_products:
                    if gp["link"] not in existing_links:
                        products.append(gp)
                products = sort_by_tr_priority(products)
            except:
                pass

        return {"success": True, "count": len(products), "products": products[:8]}
    except Exception as e:
        if "HTTPException" in type(e).__name__:
            raise
        raise HTTPException(500, str(e))


# ─── Endpoint 3: Toplu parça arama (outfit) ───
@app.post("/api/search-outfit")
async def search_outfit(pieces: str = Form(...)):
    if not SERPAPI_KEY:
        raise HTTPException(500, "SERPAPI_KEY not set")

    try:
        items = json.loads(pieces)
    except:
        raise HTTPException(400, "Invalid JSON")

    results = []
    for item in items:
        desc = item.get("description", "")
        cat = item.get("category", "")
        color = item.get("color", "")
        brand = item.get("brand", "")

        parts = []
        if brand and brand != "?":
            parts.append(brand)
        parts.append(desc)
        if color:
            parts.append(color)
        query = " ".join(parts).strip()

        if not query:
            continue

        try:
            search = GoogleSearch({
                "engine": "google_shopping",
                "q": query,
                "gl": "tr",
                "hl": "tr",
                "api_key": SERPAPI_KEY,
            })
            data = search.get_dict()
            products = parse_shopping_results(data)

            # TR sonuç az ise global ekle
            tr_count = sum(1 for p in products if get_store_score(p["link"], p["source"]) >= 70)
            if tr_count < 2:
                try:
                    search_g = GoogleSearch({
                        "engine": "google_shopping",
                        "q": query,
                        "gl": "us",
                        "hl": "en",
                        "api_key": SERPAPI_KEY,
                    })
                    gdata = search_g.get_dict()
                    gproducts = parse_shopping_results(gdata)
                    existing = {p["link"] for p in products}
                    for gp in gproducts:
                        if gp["link"] not in existing:
                            products.append(gp)
                    products = sort_by_tr_priority(products)
                except:
                    pass

            results.append({
                "category": cat,
                "description": desc,
                "color": color,
                "brand": brand,
                "query": query,
                "products": products[:6],
            })
        except Exception as e:
            results.append({
                "category": cat,
                "description": desc,
                "color": color,
                "brand": brand,
                "query": query,
                "products": [],
                "error": str(e),
            })

    return {"success": True, "pieces": results}


@app.get("/api/health")
async def health():
    return {"status": "ok", "serpapi": bool(SERPAPI_KEY)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
