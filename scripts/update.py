"""Pobiera dane Cmentarza w Siedliszczu z cmentarz.app i buduje stronę w folderze _site.

Uruchamiany automatycznie co 7 dni przez GitHub Actions (.github/workflows/update.yml).
Dane publikowane za pisemną zgodą zarządcy cmentarza.
"""
import colorsys
import io
import json
import math
import os
import shutil
import sys
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

CEMETERY = "ocUSG"
API = "https://api.cmentarz.app/api/public"
S3 = "https://s3.cmentarz.app/seak-graveyards/"
LAYER = "cemetery:6039d1e1-1459-4fd2-b644-b72d1226bf37"
WMS = "https://geoserver.cmentarz.app/geoserver/wms"
BBOX = (23.1508, 51.1976, 23.1546, 51.2004)  # lon_min, lat_min, lon_max, lat_max zdjęcia lotniczego
IMG_W, IMG_H = 2048, 2381

# układ współrzędnych sceny (metry): X na wschód, Z na południe, (0,0) = lewy górny róg zdjęcia
LAT0, LNG0 = 51.1996, 23.1527
KX = math.cos(math.radians(LAT0)) * 111320
KY = 110574
SX = (BBOX[2] - BBOX[0]) * KX
SZ = (BBOX[3] - BBOX[1]) * KY
OX = (LNG0 - BBOX[0]) * KX
OZ = (BBOX[3] - LAT0) * KY
AWAY = (-0.304, 0.953)  # kierunek "od ulicy Szpitalnej w głąb" w układzie (X, Z)

ENTRANCES = [
    ["Brama główna (wjazd wschodni)", 247.1, 99.5],
    ["Drugie schody", 189.5, 77.3],
    ["Schody przy przejściu dla pieszych", 147.4, 56.5],
    ["Brama zachodnia", 110.4, 43.0],
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_SRC = os.path.join(ROOT, "site")
OUT = os.path.join(ROOT, "_site")
PHOTOS = os.path.join(ROOT, ".cache", "zdjecia")  # zachowywane między uruchomieniami (actions/cache)

S = requests.Session()
S.headers["User-Agent"] = "cmentarz-siedliszcze-bot (parish website, with cemetery manager consent)"


def get_json(url, tries=4):
    for t in range(tries):
        try:
            r = S.get(url, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception:
            if t == tries - 1:
                raise
            time.sleep(2 + t * 3)


def paged(url):
    out, page = [], 1
    while True:
        sep = "&" if "?" in url else "?"
        r = get_json(f"{url}{sep}page={page}&per_page=100")
        out += r["data"]
        if not r["data"] or len(out) >= r["total"]:
            return out
        page += 1


def date_str(d):
    if not d:
        return ""
    if isinstance(d, str):
        return d[:10]
    return "-".join(str(d[k]) for k in ("year", "month", "day") if d.get(k) is not None)


def title_case(s):
    return " ".join("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")) for w in (s or "").split())


def to_scene(lng, lat):
    e = (lng - LNG0) * KX
    n = (lat - LAT0) * KY
    return e + OX, OZ - n


def grave_geometry(poly):
    pts = [to_scene(p[0], p[1]) for p in poly[:-1]]
    cx = sum(p[0] for p in pts) / len(pts)
    cz = sum(p[1] for p in pts) / len(pts)
    edges = []
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        edges.append((math.hypot(b[0] - a[0], b[1] - a[1]), b[0] - a[0], b[1] - a[1]))
    longest = max(edges)
    shortest = min(e[0] for e in edges)
    yaw = math.atan2(longest[1], longest[2])  # kierunek długiej osi (sin=X, cos=Z)
    if math.sin(yaw) * AWAY[0] + math.cos(yaw) * AWAY[1] < 0:
        yaw += math.pi
    return cx, cz, yaw, longest[0], shortest


def orient(G):
    """Grób zwrócony przodem do wolnego przejścia, tyłem do sąsiada; niejasne – jak sąsiedzi w rzędzie."""
    cell = defaultdict(list)
    for i, g in enumerate(G):
        cell[(int(g[2] // 5), int(g[3] // 5))].append(i)

    def around(i, r):
        g = G[i]
        cx, cz = int(g[2] // 5), int(g[3] // 5)
        for a in range(-r, r + 1):
            for b in range(-r, r + 1):
                for j in cell[(cx + a, cz + b)]:
                    if j != i:
                        yield j

    def gap(i, sgn):
        g = G[i]
        dx, dz = math.sin(g[4]) * sgn, math.cos(g[4]) * sgn
        best = 9.0
        for j in around(i, 2):
            h = G[j]
            rx, rz = h[2] - g[2], h[3] - g[3]
            pr = rx * dx + rz * dz
            lat = abs(-rx * dz + rz * dx)
            if pr > 0.3 and lat < max(g[6], h[6]) * 0.75:
                best = min(best, pr - g[5] / 2 - h[5] / 2)
        return best

    def flip(g):
        g[4] = round(g[4] + math.pi if g[4] < 0 else g[4] - math.pi, 3)

    amb = set()
    for i, g in enumerate(G):
        f, b = gap(i, 1), gap(i, -1)
        if abs(f - b) < 0.35:
            amb.add(i)
        elif f > b:
            flip(g)
    for _ in range(4):
        for i in list(amb):
            g = G[i]
            dx, dz = math.sin(g[4]), math.cos(g[4])
            votes = 0
            for j in around(i, 1):
                if j in amb:
                    continue
                h = G[j]
                rx, rz = h[2] - g[2], h[3] - g[3]
                if abs(rx * dx + rz * dz) < 0.8 and abs(-rx * dz + rz * dx) < 3.5:
                    votes += 1 if math.sin(h[4]) * dx + math.cos(h[4]) * dz > 0 else -1
            if votes < 0:
                flip(g)
                amb.discard(i)
            elif votes > 0:
                amb.discard(i)


def hull(points):
    p = sorted(set(points))
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for q in p:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(p):
        while len(up) >= 2 and cross(up[-2], up[-1], q) <= 0:
            up.pop()
        up.append(q)
    return lo[:-1] + up[:-1]


def download_orto(path):
    q = urllib.parse.urlencode({
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": LAYER, "STYLES": "",
        "SRS": "EPSG:4326", "BBOX": ",".join(map(str, BBOX)), "WIDTH": IMG_W, "HEIGHT": IMG_H, "FORMAT": "image/jpeg"})
    r = S.get(f"{WMS}?{q}", timeout=120)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    im.save(path, quality=80, optimize=True, progressive=True)
    return im


def trees_from_photo(im, G):
    px = im.load()
    IW, IH = im.size
    grid = defaultdict(list)
    for g in G:
        grid[(int(g[2] // 4), int(g[3] // 4))].append(g)

    def near_grave(x, z, d):
        cx, cz = int(x // 4), int(z // 4)
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for g in grid[(cx + a, cz + b)]:
                    if math.hypot(g[2] - x, g[3] - z) < d:
                        return True
        return False

    found, step, z = [], 2.2, 0.0
    while z < SZ:
        x = 0.0
        while x < SX:
            i, j = int(x / SX * IW), int(z / SZ * IH)
            if 6 < i < IW - 6 and 6 < j < IH - 6:
                cnt = 0
                for di in (-6, 0, 6):
                    for dj in (-6, 0, 6):
                        r, g, b = px[i + di, j + dj]
                        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                        if 0.18 < h < 0.42 and s > 0.38 and v < 0.36 and g > r:
                            cnt += 1
                if cnt >= 8 and not near_grave(x, z, 2.6):
                    found.append([round(x, 1), round(z, 1)])
            x += step
        z += step
    kept = []
    for t in found:
        if all(math.hypot(t[0] - k[0], t[1] - k[1]) > 4.8 for k in kept[-400:]):
            kept.append(t)
    foot = []
    for j in range(0, IH, 8):
        for i in range(0, IW, 8):
            r, g, b = px[i, j]
            if not (r > 245 and g > 245 and b > 245):
                foot.append((i / IW * SX, j / IH * SZ))
    return kept, [[round(a, 1), round(b, 1)] for a, b in hull(foot)]


def stone_color(path):
    """Kolor kamienia z środka zdjęcia grobu (bez trawy i nieba)."""
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        im = im.crop((int(w * .25), int(h * .35), int(w * .75), int(h * .85))).resize((40, 30))
        px = []
        for r, g, b in im.getdata():
            hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if ss > .28 and .17 < hh < .72:  # zieleń, niebo
                continue
            px.append((r, g, b))
        if len(px) < 120:
            return ""
        px.sort(key=lambda c: sum(c))
        mid = px[len(px) // 4: len(px) * 3 // 4]
        r, g, b = (sum(c[i] for c in mid) // len(mid) for i in range(3))
        return "%02x%02x%02x" % (r, g, b)
    except Exception:
        return ""


def fetch_photo(item):
    uid, ok = _fetch_photo(item)
    return uid, ok, stone_color(os.path.join(PHOTOS, uid + ".jpg")) if ok else ""


def _fetch_photo(item):
    uid, path = item
    dst = os.path.join(PHOTOS, uid + ".jpg")
    stamp = dst + ".src"
    if os.path.exists(dst) and os.path.exists(stamp) and open(stamp).read() == path:
        return uid, True
    try:
        r = S.get(S3 + urllib.parse.quote(path), timeout=60)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((900, 900))
        im.save(dst, quality=78, optimize=True, progressive=True)
        open(stamp, "w").write(path)
        return uid, True
    except Exception as e:
        print("  zdjęcie nieudane:", uid, e, file=sys.stderr)
        return uid, os.path.exists(dst)


NMT = "https://services.gugik.gov.pl/nmt/"
TERRAIN_STEP = 2.5
TERRAIN_CACHE = os.path.join(ROOT, ".cache", "teren.json")


def _floats(text):
    import re
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", text)]


def terrain():
    """Wysokości terenu z numerycznego modelu terenu GUGiK (Geoportal), siatka co 2,5 m."""
    if os.path.exists(TERRAIN_CACHE):
        with open(TERRAIN_CACHE) as f:
            print("  teren z pamięci podręcznej")
            return json.load(f)
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
    nx = int(SX // TERRAIN_STEP) + 1
    nz = int(SZ // TERRAIN_STEP) + 1
    pts = []
    for j in range(nz):
        for i in range(nx):
            lon = BBOX[0] + i * TERRAIN_STEP / KX
            lat = BBOX[3] - j * TERRAIN_STEP / KY
            e, n = tr.transform(lon, lat)
            pts.append((e, n))

    def one(a, b):
        r = S.get(NMT, params={"request": "GetHbyXY", "x": f"{a:.2f}", "y": f"{b:.2f}"}, timeout=30)
        v = _floats(r.text)
        return v[-1] if v else None

    # GUGiK: x = północ, y = wschód; sprawdzamy na pierwszym punkcie, która kolejność daje sensowną wysokość
    e0, n0 = pts[len(pts) // 2]
    order = None
    for cand in ((n0, e0, "NE"), (e0, n0, "EN")):
        try:
            h = one(cand[0], cand[1])
            print("  próba", cand[2], "->", h)
            if h is not None and 50 < h < 400:
                order = cand[2]
                break
        except Exception as ex:
            print("  próba nieudana", cand[2], ex)
    if not order:
        print("  Brak danych wysokości – teren płaski", file=sys.stderr)
        return None
    xy = [(n, e) if order == "NE" else (e, n) for e, n in pts]

    def batch(chunk):
        lst = ",".join(f"{a:.2f} {b:.2f}" for a, b in chunk)
        for t in range(4):
            try:
                r = S.get(NMT, params={"request": "GetHByPointList", "list": lst}, timeout=60)
                v = _floats(r.text)
                if len(v) == 3 * len(chunk):
                    return v[2::3]
                if len(v) == len(chunk):
                    return v
                raise ValueError(f"nieoczekiwana odpowiedź ({len(v)} liczb): {r.text[:120]}")
            except Exception as ex:
                if t == 3:
                    print("  partia nieudana:", ex, file=sys.stderr)
                    return [one(a, b) for a, b in chunk]
                time.sleep(2 + t * 2)

    chunks = [xy[k:k + 150] for k in range(0, len(xy), 150)]
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(batch, chunks))
    hs = [h for part in res for h in part]
    good = [h for h in hs if h is not None and 50 < h < 400]
    if len(good) < 0.8 * len(hs):
        print("  Za mało danych wysokości – teren płaski", file=sys.stderr)
        return None
    mean = sum(good) / len(good)
    hs = [h if (h is not None and 50 < h < 400) else mean for h in hs]
    base = min(hs)
    out = {"nx": nx, "nz": nz, "step": TERRAIN_STEP, "base": round(base, 2), "cm": [int(round((h - base) * 100)) for h in hs]}
    os.makedirs(os.path.dirname(TERRAIN_CACHE), exist_ok=True)
    with open(TERRAIN_CACHE, "w") as f:
        json.dump(out, f)
    print(f"  teren: {nx}x{nz} punktów, wysokość {base:.1f}–{max(hs):.1f} m n.p.m.")
    return out


def shadow_orient(G):
    """Ustala, z której strony grobu stoi pomnik, po cieniu na zdjęciu z drona (wysoka rozdzielczość)."""
    import numpy as np
    W2, H2 = IMG_W * 2, IMG_H * 2
    q = urllib.parse.urlencode({
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": LAYER, "STYLES": "",
        "SRS": "EPSG:4326", "BBOX": ",".join(map(str, BBOX)), "WIDTH": W2, "HEIGHT": H2, "FORMAT": "image/jpeg"})
    r = S.get(f"{WMS}?{q}", timeout=240)
    r.raise_for_status()
    lum = np.asarray(Image.open(io.BytesIO(r.content)).convert("L"), dtype=np.float32)
    sx, sz = W2 / SX, H2 / SZ

    def prof(g):
        dx, dz = math.sin(g[4]), math.cos(g[4])
        L, W = g[5], g[6]
        ts = np.arange(-L / 2 - 0.9, L / 2 + 0.9, 0.08)
        ls = np.linspace(-W * 0.3, W * 0.3, 5)
        vals = []
        for t in ts:
            xs = g[2] + dx * t + dz * ls
            zs = g[3] + dz * t - dx * ls
            i = np.clip((xs * sx).astype(int), 0, W2 - 1)
            j = np.clip((zs * sz).astype(int), 0, H2 - 1)
            vals.append(lum[j, i].mean())
        v = np.convolve(np.array(vals), np.ones(5) / 5, mode="same")
        k = int(np.argmin(v[3:-3])) + 3
        contrast = float(np.median(v) - v[k])
        t = float(ts[k])
        if contrast < 14:
            return None
        if t >= L / 2:
            return "+out"
        if t > L / 2 - 0.8:
            return "+in"
        if t <= -L / 2:
            return "-out"
        if t < -L / 2 + 0.8:
            return "-in"
        return None

    zones = [prof(g) for g in G]
    cnt = {z: zones.count(z) for z in ("+out", "+in", "-out", "-in")}
    a = cnt["+out"] + cnt["-in"]
    b = cnt["-out"] + cnt["+in"]
    print("  cienie:", cnt)
    if max(a, b) < 200 or max(a, b) < 1.3 * min(a, b):
        print("  cienie niejednoznaczne – zostaje dotychczasowe ustawienie")
        return
    head_plus = {"+out", "+in"} if a >= b else set()
    # wzór A (a>=b): cień za pomnikiem po stronie "+" => "+out" = pomnik z przodu (+), "-in" = pomnik z tyłu (-)
    use = ("+out", "-in") if a >= b else ("-out", "+in")
    flipped = kept = 0
    for g, z in zip(G, zones):
        if z not in use:
            continue
        plus = z in ("+out", "+in")
        if not plus:
            g[4] = round(g[4] + math.pi if g[4] < 0 else g[4] - math.pi, 3)
            flipped += 1
        else:
            kept += 1
    print(f"  pomnik ustalony z cienia: {kept + flipped} grobów (obróconych: {flipped})")


def main():
    os.makedirs(PHOTOS, exist_ok=True)
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(SITE_SRC, OUT)

    print("Lista grobów…")
    graves = paged(f"{API}/cemeteries/{CEMETERY}/grave-cards")
    print("  grobów:", len(graves))
    print("Lista pochowanych…")
    flt = urllib.parse.quote(json.dumps([{"field": "uniqueReadableIdentifier", "type": "eq", "value": CEMETERY}]))
    buried = paged(f"{API}/buried-cards?filters={flt}")
    print("  pochowanych:", len(buried))

    print("Szczegóły grobów (obrysy, zdjęcia)…")
    def detail(g):
        try:
            return g["uniqueReadableIdentifier"], get_json(f"{API}/cemeteries/{CEMETERY}/grave-cards/{g['uniqueReadableIdentifier']}")
        except Exception as e:
            print("  szczegóły nieudane:", g.get("name"), e, file=sys.stderr)
            return g["uniqueReadableIdentifier"], None
    with ThreadPoolExecutor(8) as ex:
        details = dict(ex.map(detail, graves))

    by_grave = defaultdict(list)
    for b in buried:
        by_grave[b.get("graveCardUniqueIdentifier")].append(
            [title_case(b.get("firstName")), title_case(b.get("lastName")), date_str(b.get("birthDate")), date_str(b.get("deathDate"))])

    G, photo_jobs = [], []
    for g in graves:
        uid = g["uniqueReadableIdentifier"]
        d = details.get(uid) or {}
        poly = d.get("boundaries")
        if not poly or len(poly) < 4:
            continue
        x, z, yaw, L, W = grave_geometry(poly)
        photo = (d.get("photo") or {}).get("path")
        if photo:
            photo_jobs.append((uid, photo))
        G.append([g["name"], g.get("sector") or "", round(x, 2), round(z, 2), round(yaw, 3), round(L, 2), round(W, 2),
                  by_grave.get(uid, []), uid if photo else ""])
    orient(G)
    print("Kierunek pomników z cieni na zdjęciu…")
    try:
        shadow_orient(G)
    except Exception as ex:
        print("  analiza cieni nieudana:", ex, file=sys.stderr)

    print("Zdjęcia grobów:", len(photo_jobs))
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(fetch_photo, photo_jobs))
    ok = {u: good for u, good, _ in res}
    col = {u: c for u, _, c in res}
    for g in G:
        g.append(col.get(g[8], "") if g[8] else "")
        if g[8] and not ok.get(g[8]):
            g[8] = ""
    keep = {uid + ".jpg" for uid, good in ok.items() if good}
    os.makedirs(os.path.join(OUT, "zdjecia"), exist_ok=True)
    for name in os.listdir(PHOTOS):
        if name in keep:
            shutil.copy2(os.path.join(PHOTOS, name), os.path.join(OUT, "zdjecia", name))

    print("Zdjęcie lotnicze…")
    im = download_orto(os.path.join(OUT, "orto.jpg"))
    trees, foot = trees_from_photo(im, G)
    ents = [e for e in ENTRANCES]
    trees = [t for t in trees if not any(math.hypot(e[1] - t[0], e[2] - t[1]) < 6 for e in ents)]

    print("Wysokości terenu (Geoportal)…")
    try:
        hgt = terrain()
    except Exception as ex:
        print("  teren nieudany:", ex, file=sys.stderr)
        hgt = None

    data = {"g": G, "hgt": hgt, "foot": foot, "trees": trees, "ent": ents, "img": {"w": round(SX, 2), "h": round(SZ, 2)},
            "updated": time.strftime("%Y-%m-%d")}
    with open(os.path.join(OUT, "groby.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Gotowe: {len(G)} grobów, {sum(len(g[7]) for g in G)} osób, {len(keep)} zdjęć.")


if __name__ == "__main__":
    main()
