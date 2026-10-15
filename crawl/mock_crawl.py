"""목업 크롤링: tier 1 민요 몇 곡 × 전통/퓨전 쿼리 → 영상 약 100개 메타데이터 + 한국어 자막.

미디어는 받지 않는다 (skip_download). 결과는 mock/ 아래에 저장.
  mock/mock.sqlite          queries, video, video_query, subtitle 테이블
  mock/raw/{id}.json.gz     포맷·썸네일 목록을 뺀 info JSON
  mock/subs/{id}.ko.vtt     자막 원본

단계: 검색 → 메타데이터(자막 요청 없음) → 자막(429면 지수 백오프).
자막 요청이 메타데이터 수집을 막지 않도록 분리했다.

사용: .venv/bin/python crawl/mock_crawl.py [--target 100] [--per-query 12]
중단 후 다시 실행하면 이미 받은 video_id는 건너뛴다.
"""
import argparse
import gzip
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "mock"
DB = OUT / "mock.sqlite"

# tier 1 통속민요 중 지역·인지도를 섞어 고름. 유튜브 통용 표기를 쓴다.
SONGS = ["진도아리랑", "밀양아리랑", "뱃노래", "몽금포타령", "쾌지나칭칭나네",
         "옹헤야", "도라지타령", "늴리리야", "한오백년", "창부타령"]
MODIFIERS = {"전통": "{} 명창", "퓨전": "{} 퓨전국악"}

DROP_KEYS = {"formats", "thumbnails", "requested_formats", "automatic_captions", "subtitles",
             "heatmap", "http_headers", "requested_subtitles", "_format_sort_fields"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
  query TEXT PRIMARY KEY, song TEXT, axis TEXT, n_results INTEGER, run_at TEXT);
CREATE TABLE IF NOT EXISTS video_query (
  video_id TEXT, query TEXT, rank INTEGER, PRIMARY KEY (video_id, query));
CREATE TABLE IF NOT EXISTS video (
  video_id TEXT PRIMARY KEY, title TEXT, description TEXT, tags TEXT,
  channel_id TEXT, channel TEXT, channel_follower_count INTEGER,
  upload_date TEXT, duration REAL, view_count INTEGER, like_count INTEGER,
  comment_count INTEGER, categories TEXT, is_short INTEGER, live_status TEXT,
  has_ko_manual INTEGER, has_ko_auto INTEGER, crawled_at TEXT, status TEXT,
  sub_kind TEXT, sub_url TEXT, sub_status TEXT);
CREATE TABLE IF NOT EXISTS subtitle (
  video_id TEXT PRIMARY KEY, kind TEXT, text TEXT);
"""


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def vtt_to_text(path: Path) -> str:
    """VTT → 중복 없는 줄 텍스트 (자동 자막의 롤링 중복 제거)."""
    lines, prev = [], None
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not ln.strip() or "-->" in ln or ln.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if ln and ln != prev:
            lines.append(ln)
            prev = ln
    return "\n".join(dict.fromkeys(lines))


def search_phase(con, per_query):
    opts = {"quiet": True, "extract_flat": "in_playlist", "skip_download": True,
            "sleep_interval_requests": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for song in SONGS:
            for axis, tmpl in MODIFIERS.items():
                q = tmpl.format(song)
                if con.execute("SELECT 1 FROM queries WHERE query=?", (q,)).fetchone():
                    continue
                info = ydl.extract_info(f"ytsearch{per_query}:{q}", download=False)
                entries = [e for e in info.get("entries", []) if e and e.get("id")]
                con.executemany("INSERT OR IGNORE INTO video_query VALUES (?,?,?)",
                                [(e["id"], q, i + 1) for i, e in enumerate(entries)])
                con.execute("INSERT INTO queries VALUES (?,?,?,?,?)",
                            (q, song, axis, len(entries), datetime.now(timezone.utc).isoformat()))
                con.commit()
                log(f"search '{q}': {len(entries)}")
                time.sleep(random.uniform(2, 4))


def pick_targets(con, target):
    """쿼리별 순위를 라운드로빈으로 섞어 중복 없이 target개 고름."""
    rows = con.execute("SELECT video_id, query, rank FROM video_query ORDER BY rank, query").fetchall()
    picked = []
    for vid, _, _ in rows:
        if vid not in picked:
            picked.append(vid)
        if len(picked) >= target:
            break
    return picked


VIDEO_COLS = ("video_id, title, description, tags, channel_id, channel, channel_follower_count, upload_date, "
              "duration, view_count, like_count, comment_count, categories, is_short, live_status, "
              "has_ko_manual, has_ko_auto, crawled_at, status, sub_kind, sub_url, sub_status")


# 메타데이터 요청 차단 대응 (2026-10-13 실측: 약 50분 연속 수집 후 '세션 rate-limited' 발생)
NET_RE = re.compile(r"Failed to resolve|nodename nor servname|Connection (reset|aborted|refused)|timed out|Network is unreachable|Temporary failure", re.I)
RATE_LIMIT_RE = re.compile(r"rate-limited|rate limited|try again later|Sign in to confirm|429", re.I)
BACKOFF_START, BACKOFF_MAX = 900, 3600
REST_EVERY, REST_SECONDS = 300, 900  # 2026-10-13 밤: 1시간 주기 제한이 길어져서 더 느리게


def pick_sub(info):
    """한국어 자막 URL 선택: 수동 자막 > 자동 원어(ko-orig) > 자동(ko)."""
    for kind, pool, keys in (("manual", info.get("subtitles") or {}, ("ko", "ko-KR")),
                             ("auto", info.get("automatic_captions") or {}, ("ko-orig", "ko"))):
        for k in keys:
            for f in pool.get(k, []):
                if f.get("ext") == "vtt":
                    return kind, f["url"]
    return None, None


def meta_phase(con, ids):
    """메타데이터만 (자막 요청 없음). 자막 URL은 기록해 두고 subs_phase에서 받는다."""
    (OUT / "raw").mkdir(exist_ok=True)
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True,
            "sleep_interval_requests": 1}
    done = {r[0] for r in con.execute("SELECT video_id FROM video WHERE status='ok'")}
    todo = [v for v in ids if v not in done]
    log(f"meta: {len(todo)} to fetch ({len(done)} already done)")
    unavailable_streak, backoff = 0, BACKOFF_START
    with yt_dlp.YoutubeDL(opts) as ydl:
        n = 0
        while n < len(todo):
            vid = todo[n]
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                unavailable_streak = unavailable_streak + 1 if "Video unavailable" in msg else 0
                # 차단 신호: 명시적 rate-limit 메시지, 또는 'Video unavailable'이 연속으로 몰려 나올 때
                if RATE_LIMIT_RE.search(msg) or unavailable_streak >= 3:
                    log(f"  [{n + 1}] {vid} THROTTLED ({msg[:80]}), sleeping {backoff}s then retrying")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    if unavailable_streak >= 3:  # 연속 실패한 앞 영상들도 다시 시도
                        n = max(0, n - unavailable_streak + 1)
                    unavailable_streak = 0
                    continue
                if NET_RE.search(msg):  # 네트워크 끊김: 기록하지 않고 기다렸다가 같은 영상 재시도
                    log(f"  [{n + 1}] {vid} network error, sleeping 120s")
                    time.sleep(120)
                    continue
                log(f"  [{n + 1}] {vid} ERROR {msg[:120]}")
                con.execute("INSERT OR REPLACE INTO video (video_id, status, crawled_at) VALUES (?,?,?)",
                            (vid, "error:" + msg[:200], datetime.now(timezone.utc).isoformat()))
                con.commit()
                n += 1
                continue
            unavailable_streak, backoff = 0, BACKOFF_START
            n += 1
            slim = {k: v for k, v in info.items() if k not in DROP_KEYS}
            with gzip.open(OUT / "raw" / f"{vid}.json.gz", "wt", encoding="utf-8") as f:
                json.dump(slim, f, ensure_ascii=False)
            has_manual = any(k.startswith("ko") for k in info.get("subtitles") or {})
            has_auto = any(k.startswith("ko") for k in info.get("automatic_captions") or {})
            sub_kind, sub_url = pick_sub(info)
            dur = info.get("duration") or 0
            con.execute(f"""INSERT OR REPLACE INTO video ({VIDEO_COLS})
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                vid, info.get("title"), info.get("description"), json.dumps(info.get("tags") or [], ensure_ascii=False),
                info.get("channel_id"), info.get("channel"), info.get("channel_follower_count"),
                info.get("upload_date"), dur, info.get("view_count"), info.get("like_count"),
                info.get("comment_count"), json.dumps(info.get("categories") or [], ensure_ascii=False),
                int("/shorts/" in (info.get("webpage_url") or "") or 0 < dur <= 60), info.get("live_status"),
                int(has_manual), int(has_auto), datetime.now(timezone.utc).isoformat(), "ok",
                sub_kind, sub_url, None))
            con.commit()
            log(f"  [{n}/{len(todo)}] {vid} {dur:.0f}s sub={sub_kind or '-'} {info.get('title', '')[:50]}")
            if n % REST_EVERY == 0:
                log(f"  rest {REST_SECONDS}s after {n} videos")
                time.sleep(REST_SECONDS)
            time.sleep(random.uniform(3, 6))


def subs_phase(con, max_consecutive_429=4):
    """기록해 둔 자막 URL로 VTT를 받는다. 429면 대기 시간을 두 배씩 늘린다."""
    (OUT / "subs").mkdir(exist_ok=True)
    todo = con.execute("""SELECT video_id, sub_kind, sub_url FROM video
                          WHERE status='ok' AND sub_url IS NOT NULL
                          AND (sub_status IS NULL OR sub_status LIKE 'err%')""").fetchall()
    log(f"subs: {len(todo)} to fetch")
    backoff, streak = 60, 0
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        for n, (vid, kind, url) in enumerate(todo, 1):
            try:
                data = ydl.urlopen(url).read()
            except Exception as e:  # noqa: BLE001 - 네트워크 오류는 기록만 하고 진행
                msg = str(e)[:150]
                con.execute("UPDATE video SET sub_status=? WHERE video_id=?", ("err:" + msg, vid))
                con.commit()
                if "429" in msg:
                    streak += 1
                    log(f"  [{n}] {vid} 429 (streak {streak}), sleeping {backoff}s")
                    if streak >= max_consecutive_429:
                        log("  too many 429s, stopping subs phase (rerun later to resume)")
                        return
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 600)
                else:
                    log(f"  [{n}] {vid} ERROR {msg}")
                continue
            streak, backoff = 0, 60
            vtt = OUT / "subs" / f"{vid}.ko.vtt"
            vtt.write_bytes(data)
            con.execute("INSERT OR REPLACE INTO subtitle VALUES (?,?,?)", (vid, kind, vtt_to_text(vtt)))
            con.execute("UPDATE video SET sub_status='ok' WHERE video_id=?", (vid,))
            con.commit()
            log(f"  [{n}/{len(todo)}] {vid} sub {kind} {len(data) // 1024}KB")
            time.sleep(random.uniform(8, 12))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--per-query", type=int, default=12)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(video)")}
    if "sub_url" not in cols:  # 첫 버전 DB와 호환
        con.execute("DROP TABLE video")
        con.executescript(SCHEMA)
    t0 = time.time()
    search_phase(con, args.per_query)
    ids = pick_targets(con, args.target)
    log(f"candidates={con.execute('SELECT COUNT(DISTINCT video_id) FROM video_query').fetchone()[0]}, picked={len(ids)}")
    t1 = time.time()
    meta_phase(con, ids)
    log(f"meta phase {time.time() - t1:.0f}s")
    t2 = time.time()
    subs_phase(con)
    log(f"subs phase {time.time() - t2:.0f}s")
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
