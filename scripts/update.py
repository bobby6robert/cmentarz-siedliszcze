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
    ["Brama główna (wjazd)", 110.4, 43.0],
    ["Schody przy przejściu dla pieszych", 147.4, 56.5],
    ["Drugie schody", 189.5, 77.3],
    ["Wjazd wschodni", 247.1, 99.5],
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
                        if 0.18 < h < 0.42 and s > 0.35 and v < 0.42 and g > r:
                            cnt += 1
                if cnt >= 7 and not near_grave(x, z, 2.2):
                    found.append([round(x, 1), round(z, 1)])
            x += step
        z += step
    kept = []
    for t in found:
        if all(math.hypot(t[0] - k[0], t[1] - k[1]) > 3.6 for k in kept[-400:]):
            kept.append(t)
    foot = []
    for j in range(0, IH, 8):
        for i in range(0, IW, 8):
            r, g, b = px[i, j]
            if not (r > 245 and g > 245 and b > 245):
                foot.append((i / IW * SX, j / IH * SZ))
    return kept, [[round(a, 1), round(b, 1)] for a, b in hull(foot)]


def fetch_photo(item):
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

    print("Zdjęcia grobów:", len(photo_jobs))
    with ThreadPoolExecutor(8) as ex:
        ok = dict(ex.map(fetch_photo, photo_jobs))
    for g in G:
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

    data = {"g": G, "foot": foot, "trees": trees, "ent": ents, "img": {"w": round(SX, 2), "h": round(SZ, 2)},
            "updated": time.strftime("%Y-%m-%d")}
    with open(os.path.join(OUT, "groby.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Gotowe: {len(G)} grobów, {sum(len(g[7]) for g in G)} osób, {len(keep)} zdjęć.")


if __name__ == "__main__":
    main()
