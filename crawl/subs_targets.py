"""지정한 영상들의 한국어 자막(수동 > 자동)을 받아 subtitle 테이블에 넣는다.

자막 URL은 몇 시간이면 만료되므로 영상 정보를 다시 받아 새 URL을 얻은 직후 바로 받는다.
대상: CSV의 video_id 열 (기본 review/숨은차용_후보.csv). 이미 받은 영상은 건너뛴다.
요청 제한(봇 확인·429)이 오면 15분부터 두 배씩 쉬고 같은 영상을 다시 시도한다.

사용: .venv/bin/python crawl/subs_targets.py [--csv 경로]
"""
import argparse
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
from mock_crawl import RATE_LIMIT_RE, log, vtt_to_text  # noqa: E402

# 네트워크 끊김 (DNS 실패, 연결 끊김, 시간 초과): 건너뛰지 않고 기다렸다가 같은 영상을 다시 시도
NET_RE = re.compile(r"Failed to resolve|nodename nor servname|Connection (reset|aborted|refused)|timed out|Network is unreachable|Temporary failure", re.I)

DB = ROOT / "db" / "minyo.sqlite"
SUBS = ROOT / "db" / "subs"


def pick(info):
    """(kind, vtt url): 수동 한국어 자막 우선, 없으면 자동 생성 한국어."""
    for kind, src in (("manual", info.get("subtitles") or {}), ("auto", info.get("automatic_captions") or {})):
        for lang in ("ko", "ko-KR", "ko-orig"):
            for f in src.get(lang) or []:
                if f.get("ext") == "vtt":
                    return kind, f["url"]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "review/숨은차용_후보.csv"))
    a = ap.parse_args()
    SUBS.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    done = {r[0] for r in con.execute("SELECT video_id FROM subtitle")}
    ids = [v for v in pd.read_csv(a.csv).video_id.drop_duplicates() if v not in done]
    log(f"subs_targets: {len(ids)} to fetch")
    backoff, n, got, none = 900, 0, 0, 0
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        while n < len(ids):
            vid = ids[n]
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
                kind, url = pick(info)
                data = ydl.urlopen(url).read() if url else None
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:150]
                if RATE_LIMIT_RE.search(msg):
                    log(f"  [{n + 1}] {vid} rate limited, sleeping {backoff}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 3600)
                    continue
                if NET_RE.search(msg):
                    log(f"  [{n + 1}] {vid} network error, sleeping 120s")
                    time.sleep(120)
                    continue
                log(f"  [{n + 1}] {vid} ERROR {msg}")
                n += 1
                continue
            backoff = 900
            n += 1
            if data is None:
                none += 1
                con.execute("INSERT OR REPLACE INTO subtitle VALUES (?,?,?)", (vid, "none", ""))
            else:
                vtt = SUBS / f"{vid}.ko.vtt"
                vtt.write_bytes(data)
                con.execute("INSERT OR REPLACE INTO subtitle VALUES (?,?,?)", (vid, kind, vtt_to_text(vtt)))
                got += 1
            con.commit()
            log(f"  [{n}/{len(ids)}] {vid} {kind or 'none'}")
            time.sleep(random.uniform(15, 25))
    log(f"subs_targets done: got={got} none={none}")


if __name__ == "__main__":
    main()
