#!/usr/bin/env python3
"""
나의 여행 기록 — 어드민 서버 (내 컴퓨터에서만 돌아간다, 파이썬 표준 라이브러리만 쓴다)

이 폴더는 두 쪽으로 나뉜다.
  어드민  admin/     내 컴퓨터에서만 여는 관리 화면. 장소를 찾고 기록하고 고친다
  사이트  docs/      GitHub Pages 가 웹에 올리는 쪽. trips.json 하나만 읽어서 지도와 목록을 그린다

데이터가 흐르는 길
  어드민에서 기록·수정  →  data/trips.json (내 기록 원본)
  어드민의 [게시] 버튼  →  docs/trips.json (사이트가 읽는 파일)
  git push            →  웹에 반영

[게시]를 따로 둔 이유. 어드민에서 고치는 중인 것이 바로 웹에 나가지 않게 하려고.
고치고, 로컬 미리보기로 확인하고, 게시하고, push 하는 네 걸음이다.

구글 키는 두 개이고 둘 다 .env 에 둔다.
  GOOGLE_MAPS_API_KEY      어드민의 장소 검색용. 이 서버만 쓰고 브라우저로 내려보내지 않는다
  GOOGLE_MAPS_BROWSER_KEY  사이트 지도용. 지도는 브라우저가 그리기 때문에 [게시]할 때 docs/trips.json 에 들어가
                           공개된다. 그래서 구글 콘솔에서 "내 사이트 주소에서만 쓰기"로 묶어 둔 키를 쓴다
.env 는 .gitignore 에 들어 있어서 GitHub 에 올라가지 않는다.

구글 이용 조건 때문에 검색 결과의 좌표·주소는 저장하지 않는다. 저장하는 것은 장소 ID(placeId)와
내가 적은 칸(장소 이름, 국가, 날짜, 메모)뿐이고, 사이트가 열릴 때 장소 ID로 핀 위치를 구글에 다시 묻는다.

실행
    python3 admin/server.py          # http://127.0.0.1:8800 어드민, /site/ 는 사이트 미리보기
    python3 admin/server.py 9000     # 포트 지정
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 여행 기록 폴더 맨 바깥
ADMIN = ROOT / "admin"
DOCS = ROOT / "docs"                               # GitHub Pages 가 여는 폴더
TRIPS_PATH = ROOT / "data" / "trips.json"          # 내 기록 원본
SITE_PATH = DOCS / "trips.json"                    # 사이트가 읽는 파일
ENV_PATH = ROOT / ".env"
DEFAULT_PORT = 8800                                # 책장(8799)과 동시에 켤 수 있게 다른 번호
DEFAULT_TITLE = "나의 여행 기록"


# ───────────────────────────── ① 설정과 저장 ─────────────────────────────
def read_env():
    """ .env 의 KEY=값 줄을 읽는다. 환경변수에 같은 이름이 있으면 그쪽이 먼저다. """
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_BROWSER_KEY", "GOOGLE_MAPS_MAP_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def env_key(name):
    """ 키 값. 비었거나 .env.example 의 안내 문구 그대로면 없는 것으로 본다. """
    v = read_env().get(name, "").strip()
    return "" if not v or v.startswith("여기에") else v


def load_db():
    if not TRIPS_PATH.exists():
        return {"site": {"title": DEFAULT_TITLE, "owner": ""}, "trips": []}
    return json.loads(TRIPS_PATH.read_text(encoding="utf-8"))


def save_db(db):
    # 임시 파일에 먼저 쓰고 바꿔치기한다. 쓰다가 꺼져도 원본이 반쯤 지워지지 않게.
    TRIPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRIPS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRIPS_PATH)


# ───────────────────────────── ② 구글 장소 검색 ─────────────────────────────
# Places API (New) 의 Text Search. 내가 다녀온 곳을 찾을 때만 부른다.
# 받아오는 칸을 FieldMask 로 좁혀 둔다. 칸이 늘면 요금 등급이 올라간다.
_last_call = [0.0]
_lock = threading.Lock()
FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.addressComponents"


def google_search(q):
    key = env_key("GOOGLE_MAPS_API_KEY")
    if not key:
        raise RuntimeError(".env 파일에 GOOGLE_MAPS_API_KEY 가 없어요. README 의 '구글 키 넣기'를 따라 해 주세요")
    with _lock:  # 버튼을 여러 번 눌러도 호출 사이를 0.2초 띄운다
        wait = 0.2 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    body = json.dumps({"textQuery": q, "languageCode": "ko", "pageSize": 10}).encode("utf-8")
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchText", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        if e.code in (400, 401, 403):
            raise RuntimeError("구글이 요청을 받아 주지 않아요. .env 의 키, 그리고 구글 콘솔에서 "
                               "'Places API (New)'를 켰는지 확인해 주세요. (" + (msg or str(e.code)) + ")")
        raise RuntimeError(f"구글 응답 오류 {e.code} {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"구글에 연결하지 못했어요 ({e.reason}). 이 문장을 Claude 에게 보여 주세요")
    return [to_item(p) for p in data.get("places") or []]


def to_item(p):
    """ 검색 결과에서 화면에 보여 줄 값만 남긴다. 주소는 고를 때 보여 주기만 하고 저장하지 않는다. """
    country, code = "", ""
    for c in p.get("addressComponents") or []:
        if "country" in (c.get("types") or []):
            country, code = c.get("longText") or "", (c.get("shortText") or "").upper()
    return {
        "placeId": p.get("id") or "",
        "name": (p.get("displayName") or {}).get("text") or "",
        "address": p.get("formattedAddress") or "",
        "country": country,
        "countryCode": code,
    }


def search(q):
    q = re.sub(r"\s+", " ", (q or "")).strip()[:100]
    return google_search(q) if q else []


# ───────────────────────────── ③ 기록 다루기 ─────────────────────────────
# 기록 한 건에 들어가는 칸. 어드민 입력 화면과 사이트가 이 이름을 같이 쓴다.
#   place        장소 이름 (내가 붙인 이름. 검색 결과 이름이 처음에 채워진다)
#   country      국가          countryCode  국가 코드 두 글자 (국기 표시용, 없어도 된다)
#   date         날짜 YYYY-MM-DD
#   memo         메모
#   placeId      구글 장소 ID. 있으면 사이트 지도에 핀이 꽂힌다
FIELDS = ["place", "country", "countryCode", "date", "memo", "placeId"]
PUBLIC = ["id"] + FIELDS   # 사이트로 내보내는 칸


def clean_trip(data, old=None):
    trip = dict(old or {})
    for k in FIELDS:
        if k in data:
            trip[k] = data[k]
    for k in FIELDS:
        trip[k] = str(trip.get(k) or "").strip()
    if not trip["country"]:
        raise ValueError("국가가 비어 있어요")
    trip["countryCode"] = trip["countryCode"].upper() if re.fullmatch(r"[A-Za-z]{2}", trip["countryCode"]) else ""
    if not trip["date"]:
        trip["date"] = date.today().isoformat()
    try:
        date.fromisoformat(trip["date"])
    except ValueError:
        raise ValueError("날짜는 2026-11-22 처럼 적어 주세요")
    return trip


def new_id(db):
    return max([t.get("id", 0) for t in db["trips"]] + [0]) + 1


def public_trips(db):
    trips = sorted(db["trips"], key=lambda t: (t.get("date") or "", t.get("id")), reverse=True)
    return [{k: t.get(k) for k in PUBLIC} for t in trips]


def site_map():
    """ 사이트 지도 설정. 브라우저용 키만 내보낸다. 검색용 키는 절대 넣지 않는다. """
    return {"key": env_key("GOOGLE_MAPS_BROWSER_KEY"), "mapId": env_key("GOOGLE_MAPS_MAP_ID") or "DEMO_MAP_ID"}


def publish(db):
    """ 사이트가 읽는 docs/trips.json 을 새로 쓴다. 사이트에 필요한 칸만 내보낸다. """
    trips = public_trips(db)
    out = {"site": db.get("site", {}), "map": site_map(), "updated": date.today().isoformat(), "trips": trips}
    DOCS.mkdir(exist_ok=True)
    SITE_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(trips)


def published_differs(db):
    """ 어드민에서 고친 뒤 아직 게시하지 않은 게 있는지. 어드민 위쪽 알림에 쓴다. """
    if not SITE_PATH.exists():
        return True
    try:
        pub = json.loads(SITE_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return True
    return (public_trips(db) != pub.get("trips") or pub.get("site") != db.get("site")
            or pub.get("map") != site_map())


# ───────────────────────────── ④ 서버 ─────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 터미널을 조용하게
        pass

    def send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def send_file(self, path):
        if not path.is_file():
            self.send_error(404)
            return
        types = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def find(self, db, path):
        m = re.fullmatch(r"/api/trips/(\d+)", urllib.parse.urlparse(path).path)
        if not m:
            return None, None
        tid = int(m.group(1))
        return tid, next((i for i, t in enumerate(db["trips"]) if t["id"] == tid), None)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/":
                return self.send_file(ADMIN / "admin.html")
            if u.path == "/site":
                self.send_response(301)
                self.send_header("Location", "/site/")
                self.end_headers()
                return
            if u.path.startswith("/site/"):
                # 사이트 미리보기. docs/ 를 그대로 보여 준다. 웹에 올라가면 이 화면이 그대로 열린다
                rel = urllib.parse.unquote(u.path[len("/site/"):]) or "index.html"
                target = (DOCS / rel).resolve()
                if DOCS.resolve() not in target.parents and target != DOCS.resolve():
                    return self.send_error(403)
                return self.send_file(target)
            if u.path == "/api/trips":
                db = load_db()
                return self.send_json({**db, "unpublished": published_differs(db),
                                       "hasKey": bool(env_key("GOOGLE_MAPS_API_KEY")),
                                       "hasBrowserKey": bool(env_key("GOOGLE_MAPS_BROWSER_KEY"))})
            if u.path == "/api/search":
                q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
                return self.send_json({"items": search(q)})
            self.send_error(404)
        except Exception as e:  # 화면에 원인을 그대로 보여 준다. 막히면 그 문장을 Claude 에게 보여 주면 된다
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            db = load_db()
            if u.path == "/api/trips":
                trip = clean_trip(self.read_body())
                trip["id"] = new_id(db)
                trip["addedAt"] = date.today().isoformat()
                db["trips"].append(trip)
                save_db(db)
                return self.send_json(trip)
            if u.path == "/api/site":
                data = self.read_body()
                db["site"] = {"title": (data.get("title") or DEFAULT_TITLE).strip(), "owner": (data.get("owner") or "").strip()}
                save_db(db)
                return self.send_json(db["site"])
            if u.path == "/api/publish":
                return self.send_json({"count": publish(db)})
            if u.path == "/api/clear-samples":
                db["trips"] = [t for t in db["trips"] if not t.get("sample")]
                save_db(db)
                return self.send_json({"count": len(db["trips"])})
            self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_PUT(self):
        try:
            db = load_db()
            tid, i = self.find(db, self.path)
            if tid is None:
                return self.send_error(404)
            if i is None:
                return self.send_json({"error": "없는 기록이에요"}, 404)
            db["trips"][i] = clean_trip(self.read_body(), db["trips"][i])
            db["trips"][i].pop("sample", None)  # 손댄 샘플은 내 기록으로 본다
            save_db(db)
            self.send_json(db["trips"][i])
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)

    def do_DELETE(self):
        db = load_db()
        tid, _ = self.find(db, self.path)
        if tid is None:
            return self.send_error(404)
        db["trips"] = [t for t in db["trips"] if t["id"] != tid]
        save_db(db)
        self.send_json({"ok": True})


def main():
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else DEFAULT_PORT
    # 127.0.0.1 에만 연다. 같은 와이파이의 다른 사람이 내 어드민에 들어올 수 없게
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{port}/"
        print(f"\n  {port}번 포트를 이미 쓰고 있어요. 어드민이 벌써 켜져 있을 수 있어요: {url}\n")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  여행 기록 어드민   {url}\n  사이트 미리보기    {url}site/\n\n  끄려면 이 창에서 Ctrl+C\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  어드민을 껐어요")


if __name__ == "__main__":
    main()
