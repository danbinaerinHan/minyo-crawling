"""자막으로 숨은 차용 찾기 (YouTube 요청 없음).

subtitle 테이블의 자막(자동·수동)을 곡별 고유 구절과 비교한다.
자동 자막은 가사가 뭉개지므로 발음 정규화(phon) 후 부분 문자열 유사도로 본다.
짧은 구절(「여름이라」, 「가세가세」)은 여러 곡에 흔하므로, 8자 이상 긴 구절이
2개 이상 잡히거나, 긴 구절 1개 + 전체 3개 이상일 때만 그 곡으로 인정한다.

출력: subtitle_songs(video_id, song, hits, phrases) 테이블, review/자막_차용후보.csv
사용: .venv/bin/python process/subs_borrow.py
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "process"))
from mock_analyze import FUZZ_THRESHOLD, phon  # noqa: E402
from timestamp_long import load_phrases  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()
    con = sqlite3.connect(DB)
    phrases = {t: [p for p in ps if len(p) >= 4] for t, ps in load_phrases().items()}
    subs = pd.read_sql("""SELECT s.video_id, s.text, v.title, v.channel, l.title_songs
                          FROM subtitle s JOIN video v USING(video_id)
                          LEFT JOIN video_text_labels l USING(video_id)
                          WHERE s.kind != 'none' AND s.text != ''""", con)
    out = []
    for r in subs.itertuples():
        sk = phon(r.text.replace("[음악]", ""))
        if len(sk) < 20:
            continue
        for song, ps in phrases.items():
            hit = [p for p in ps if fuzz.partial_ratio(p, sk) >= FUZZ_THRESHOLD]
            long_ = [p for p in hit if len(p) >= 18]  # 자모 기준 약 8음절
            if len(long_) >= 2 or (len(long_) >= 1 and len(hit) >= 3):
                out.append((r.video_id, song, len(hit), "|".join(sorted(hit, key=len, reverse=True)[:6])))
    con.execute("DROP TABLE IF EXISTS subtitle_songs")
    con.execute("CREATE TABLE subtitle_songs (video_id TEXT, song TEXT, hits INTEGER, phrases TEXT)")
    con.executemany("INSERT INTO subtitle_songs VALUES (?,?,?,?)", out)
    con.commit()
    df = pd.DataFrame(out, columns=["video_id", "song", "hits", "phrases"]).merge(
        subs[["video_id", "title", "channel", "title_songs"]])
    df["new"] = [s.split(" ")[0] not in (ts or "") for s, ts in zip(df.song, df.title_songs)]
    df.sort_values(["new", "hits"], ascending=False).to_csv(ROOT / "review/자막_차용후보.csv", index=False, encoding="utf-8-sig")
    print(f"subs={len(subs)} videos_with_song={df.video_id.nunique()} pairs={len(df)} "
          f"new_vs_title={int(df.new.sum())} (videos {df[df.new].video_id.nunique()})")


if __name__ == "__main__":
    main()
