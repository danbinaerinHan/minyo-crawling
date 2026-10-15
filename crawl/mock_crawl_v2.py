"""목업 v2: 채널·시리즈 순회 → 제목 파서 + anchor 매칭 → 매칭된 영상만 메타데이터.

1차(mock_crawl.py)의 결론 "키워드 검색보다 채널 순회가 낫다"를 숫자로 확인하는 파일럿.
자막은 받지 않는다.

출력 (mock_v2/):
  mock_v2.sqlite   channel_list(채널 목록 + 제목 파싱 결과), video(상세 메타)
  raw/{id}.json.gz

사용: .venv/bin/python crawl/mock_crawl_v2.py [--max-detail 200]
"""
import argparse
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
sys.path.insert(0, str(ROOT / "process"))
import mock_crawl  # noqa: E402
from mock_analyze import EXTRA_ALIASES, key, nfc  # noqa: E402

OUT = ROOT / "mock_v2"
CH = "https://www.youtube.com/channel/"

# (seed 이름, 유형, URL). 큰 방송 채널은 채널 내 검색으로 범위를 좁힌다.
SEEDS = [
    ("국악방송 라디오", "broadcast", CH + "UChGH9Y7DVCJXFlBtNXTf98A/videos"),
    ("국악방송 TV", "broadcast", CH + "UCaBwNyKKjDaYIzS8dhyaXBA/videos"),
    ("KBS전주 국악한마당", "broadcast", CH + "UCpzc0K6Y6AxQq9m3dPtQWXw/search?query=국악한마당"),
    ("국립국악원", "institution", CH + "UCmN4xFSlmWw-_d2riM-ep0A/videos"),
    ("국립남도국악원", "institution", CH + "UCu8ouZak6oQcgDHlcQ0kjrw/videos"),
    ("이로", "fusion_team", CH + "UCpvlNeMjHREq3b7Vg9bFEMA/videos"),
    ("퓨전국악그룹 퀸", "fusion_team", CH + "UCZF5RffGu0pScFDWqcuAJPg/videos"),
    ("QUEEN TV", "fusion_team", CH + "UCjpgLP33Rn04-69KWOWUldg/videos"),
    ("퓨전국악 소리애", "fusion_team", CH + "UCjrIFFidPhRIh7W3x1U2wew/videos"),
    ("퓨전국악소리애 - Topic", "topic", CH + "UC2qhXBvSNeX1vYLKpaSy82g/playlists"),  # Topic 채널은 releases 탭이 없음
    ("두번째달", "fusion_team", CH + "UCdn1csOZblnPmyQrPbxl6aA/videos"),
    ("프로젝트 락", "fusion_team", CH + "UChnTLSdnd5v4pZYehtH2pxA/videos"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_list (
  seed TEXT, seed_type TEXT, video_id TEXT, title TEXT, duration REAL, views INTEGER,
  pattern TEXT, artist TEXT, new_title TEXT, orig_text TEXT, songs TEXT, match_source TEXT,
  PRIMARY KEY (seed, video_id));
CREATE TABLE IF NOT EXISTS seed_log (seed TEXT PRIMARY KEY, n INTEGER, seconds REAL, run_at TEXT);
"""

# 제목 패턴. 위에서부터 먼저 맞는 것
FM_LIVE = re.compile(r"\[FM\s?LIVE\]\s*(?P<artist>.+?)\s*[-–]\s*(?P<song>.+?)\s*(?:\((?:바투의 상사디야|[^()]*라디오[^()]*|[^()]*라이브)\))?\s*$", re.I)
ORIG_CREDIT = re.compile(r"\(\s*원곡\s*[:：]\s*(?P<orig>[^)]+)\)")
PAREN = re.compile(r"(?P<new>[^()\[\]|]+?)\s*\((?P<orig>[^()]+)\)")
ARTIST_SONG = re.compile(r"^(?P<artist>[^\-–|\[\]]+?)\s*[-–]\s*(?P<song>[^|\[\]]+)")
NOISE = {"Official Audio", "Official MV", "Live Clip", "Full", "Full-Kor", "Feat", "MV", "LIVE", "Live", "4K", "Lyrics"}


def build_name_dict():
    songs = pd.read_csv(ROOT / "master/anchor_songs.csv")
    aliases = pd.read_csv(ROOT / "master/anchor_aliases.csv").merge(songs[["anchor_id", "title_norm", "tier"]])
    songs = songs[songs.tier <= 2]
    d = {}
    for k, t in zip(songs.search_key, songs.title_norm):
        if len(k) >= 3:
            d[k] = re.sub(r"\s*\d+$", "", t)
    for a, t, ty, tier in zip(aliases.alias, aliases.title_norm, aliases.alias_type, aliases.tier):
        if ty != "english" and tier <= 2 and len(key(a)) >= 3:
            d.setdefault(key(a), re.sub(r"\s*\d+$", "", t))
    for a, t in EXTRA_ALIASES.items():
        d[key(a)] = t
    extra = ROOT / "master/folk_song_extra.csv"
    if extra.exists():  # 메타데이터에 없는 유명 통속곡 + 표기 변형
        for a, t in pd.read_csv(extra)[["name", "maps_to"]].itertuples(index=False):
            d[key(a)] = t
    d["아리랑"] = "아리랑"
    # 같은 곡의 표기 변형(「쾌지나칭칭나네」/「쾌지나 칭칭 나네」)을 한 이름으로 통일.
    # 통일하지 않으면 find_songs의 부분 이름 제거 단계에서 서로를 지워 곡이 사라진다.
    canon = {}
    for t in sorted(set(d.values()), key=lambda t: (-t.count(" "), t)):
        canon.setdefault(key(t), t)
    return {k: canon[key(t)] for k, t in d.items()}


# 곡명 뒤에 붙으면 곡이 아니라 행사·기관 이름 (「밀양아리랑대축제」, 「정선아리랑제」, 「아리랑TV」)
EVENT_SUFFIX = re.compile(r"^(대축제|축제|문화제|가요제|예술단|합창단|센터|재단|tv|티비|페스티벌|festival|제(?!주))", re.I)
# 곡명이 들어 있지만 민요가 아닌 대중가요·창작곡 (앞말 또는 뒷말로 판별)
NOT_MINYO_BEFORE = ("홀로", "영암")
NOT_MINYO_AFTER = ("목동",)


def find_songs(text, names):
    """곡명이 단어 시작 위치에서 시작할 때만 인정 (「국가무형유산가야금」 속 '유산가' 같은 오탐 방지).
    단어 끝은 따지지 않는다 (「창부타령을」처럼 조사가 붙는 경우)."""
    tokens = [key(t) for t in re.split(r"[\s\W_]+", nfc(text)) if key(t)]
    k, starts, pos = "".join(tokens), set(), 0
    for t in tokens:
        starts.add(pos)
        pos += len(t)
    spans = {}  # 곡 -> 잡힌 위치들 [(시작, 끝)]
    for n, t in names.items():
        i = k.find(n)
        while i != -1:
            rest, before = k[i + len(n):], k[:i]
            if (i in starts and not EVENT_SUFFIX.match(rest)
                    and not before.endswith(NOT_MINYO_BEFORE) and not rest.startswith(NOT_MINYO_AFTER)):
                spans.setdefault(t, []).append((i, i + len(n)))
            i = k.find(n, i + 1)
    # 짧은 이름이 긴 이름 안에서만 잡혔으면 뺀다 (「진도아리랑」 속 '아리랑').
    # 따로 한 번이라도 나오면 별개 곡으로 남긴다 (「뱃노래+자진뱃노래」의 '뱃노래').
    def inside(a, b, t):
        return any(o != t and s0 <= a and b <= e0 and (e0 - s0) > (b - a) for o, sp in spans.items() for s0, e0 in sp)
    hits = {t for t, sp in spans.items() if any(not inside(a, b, t) for a, b in sp)}
    # 지역 아리랑이 있으면 단독 '아리랑'은 주제어로 본다 (「아리랑 모음집 - 진도아리랑, …」)
    if "아리랑" in hits and any(h != "아리랑" and re.search("아리랑|아라리", h) for h in hits):
        hits.discard("아리랑")
    return sorted(hits)


# 민요에서 나온 대중가요·트로트·창작 아리랑·국악가요 (민요 곡으로는 세지 않고 따로 표시)
POP_GENRE = re.compile(r"국악\s?가요|국악\s?트로트|트로트")


def build_pop_dict():
    f = ROOT / "master/pop_derived.csv"
    return {key(a): t for a, t in pd.read_csv(f)[["name", "maps_to"]].itertuples(index=False)} if f.exists() else {}


def find_pop(text, pop):
    """→ (대중가요 곡명 목록, 장르 단어). 「홀로아리랑」, 「영암아리랑」 등."""
    tokens = [key(t) for t in re.split(r"[\s\W_]+", nfc(text)) if key(t)]
    k, starts, pos = "".join(tokens), set(), 0
    for t in tokens:
        starts.add(pos)
        pos += len(t)
    hits = sorted({t for n, t in pop.items() if any(m.start() in starts for m in re.finditer(re.escape(n), k))})
    g = POP_GENRE.search(nfc(text))
    return hits, (re.sub(r"\s", "", g.group(0)) if g else "")


# 곡명 사전에 없어도 민요로 보이는 제목 → "기타 민요" (후보 곡명을 orig_text에 남김)
GENRE_WORD = re.compile(r"(경기|서도|남도|동부|제주|강원|경상|전라|황해|평안|함경|토속|향토|신)\s?민요|민요|잡가|선소리|들노래|두레소리|메나리")
GENRE_TOKEN = re.compile(r"[가-힣]{1,8}(?:타령|아라리|아리랑|잡가)|(?:상여|달구|모심는|모심기|논매는|논매기|지경|베틀|뱃|배치기|노동|김매는|보리타작|도리깨|그물|노젓는)\s?(?:소리|노래)")
GENRE_NOISE = ("홀로아리랑", "퀸즈아리랑", "영암아리랑", "아리랑목동")


def genre_hint(title):
    """→ 후보 곡명 문자열 (없으면 None). 판소리 대목·대중가요는 거른다."""
    t = nfc(title)
    toks = [m.group(0).replace(" ", "") for m in GENRE_TOKEN.finditer(t)]
    toks = [x for x in dict.fromkeys(toks) if x not in GENRE_NOISE and not x.endswith(("판소리",))]
    if toks:
        return "|".join(toks)
    if GENRE_WORD.search(t):
        return "(곡명 미상)"
    return None


def parse_title(title, names):
    """→ (pattern, artist, new_title, orig_text, songs, match_source)"""
    m = FM_LIVE.search(title)
    if m:
        song = m.group("song")
        pm = PAREN.search(song)
        if pm and find_songs(pm.group("orig"), names):
            return "fm_live+paren", m.group("artist"), pm.group("new").strip(), pm.group("orig"), find_songs(pm.group("orig"), names), "paren"
        songs = find_songs(song, names)
        return "fm_live", m.group("artist"), song, None, songs, ("title" if songs else None)
    m = ORIG_CREDIT.search(title)
    if m and find_songs(m.group("orig"), names):
        return "orig_credit", None, None, m.group("orig"), find_songs(m.group("orig"), names), "paren"
    for pm in PAREN.finditer(title):
        orig = pm.group("orig")
        if orig.strip() not in NOISE and find_songs(orig, names):
            return "paren", None, pm.group("new").strip(), orig, find_songs(orig, names), "paren"
    m = ARTIST_SONG.search(title)
    songs = find_songs(title, names)
    if m and songs:
        return "artist-song", m.group("artist").strip(), m.group("song").strip(), None, songs, "title"
    if not songs:
        hint = genre_hint(title)
        if hint:
            return "genre", None, None, hint, [], "genre"
    return ("none" if not songs else "keyword"), None, None, None, songs, ("title" if songs else None)


def list_phase(con, names):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "sleep_interval_requests": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for seed, stype, url in SEEDS:
            if con.execute("SELECT 1 FROM seed_log WHERE seed=?", (seed,)).fetchone():
                continue
            t0 = time.time()
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                mock_crawl.log(f"list {seed}: ERROR {str(e)[:120]}")
                continue
            entries = []
            for e in info.get("entries") or []:  # releases 탭은 앨범 플레이리스트가 한 단계 더 있음
                if e and (e.get("_type") == "playlist" or "list=" in (e.get("url") or "")):
                    sub = ydl.extract_info(e["url"], download=False)
                    entries += [x for x in sub.get("entries") or [] if x]
                elif e and e.get("id"):
                    entries.append(e)
            rows = []
            for e in entries:
                title = nfc(e.get("title"))
                p = parse_title(title, names)
                rows.append((seed, stype, e["id"], title, e.get("duration"), e.get("view_count"),
                             p[0], p[1], p[2], p[3], "|".join(p[4]), p[5]))
            con.executemany("INSERT OR REPLACE INTO channel_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.execute("INSERT INTO seed_log VALUES (?,?,?,?)",
                        (seed, len(rows), time.time() - t0, datetime.now(timezone.utc).isoformat()))
            con.commit()
            n_match = sum(1 for r in rows if r[10])
            mock_crawl.log(f"list {seed}: {len(rows)} videos, {n_match} matched, {time.time() - t0:.0f}s")
            time.sleep(random.uniform(2, 4))


def pick(con, max_detail):
    """상세 수집 대상: 괄호 확정 > 퓨전팀·Topic > 기관 > 방송. 유형별로 고르게."""
    df = pd.read_sql("SELECT * FROM channel_list WHERE songs != ''", con).drop_duplicates("video_id")
    df["prio"] = df.match_source.map({"paren": 0, "title": 1}).fillna(2)
    df = df.sample(frac=1, random_state=0).sort_values("prio", kind="stable")
    quota = {"fusion_team": 0.35, "topic": 0.05, "broadcast": 0.4, "institution": 0.2}
    picked = []
    for st, q in quota.items():
        picked += df[df.seed_type == st].video_id.head(int(max_detail * q)).tolist()
    rest = df[~df.video_id.isin(picked)].video_id.tolist()
    return (picked + rest)[:max_detail]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-detail", type=int, default=200)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    mock_crawl.OUT = OUT  # meta_phase가 raw JSON을 저장할 위치
    con = sqlite3.connect(OUT / "mock_v2.sqlite")
    con.executescript(SCHEMA)
    con.executescript(mock_crawl.SCHEMA)
    t0 = time.time()
    list_phase(con, build_name_dict())
    mock_crawl.log(f"list phase {time.time() - t0:.0f}s")
    ids = pick(con, args.max_detail)
    t1 = time.time()
    mock_crawl.meta_phase(con, ids)
    mock_crawl.log(f"meta phase {time.time() - t1:.0f}s, total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
