"""긴 영상(공연 실황)의 자동 자막에서 민요 구간을 찾아 타임스탬프를 만든다.

방법: 자막 큐를 시간 창(window)으로 묶고, 창마다 anchor 곡 고유 구절을 퍼지 매칭한다.
같은 곡이 연속으로 잡힌 창들을 하나의 구간으로 합친다.

사용: .venv/bin/python process/timestamp_long.py mock/long/ftcCjBbMJHY.ko.vtt [--window 60 --step 15 --threshold 80 --min-hits 2]
출력: 콘솔 표 + 같은 폴더에 {id}.segments.csv
"""
import argparse
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "process"))
from mock_analyze import FUZZ_THRESHOLD, phon  # noqa: E402

TS = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d+)\s+-->")


def parse_vtt(path: Path):
    cues, t, prev = [], None, None
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = TS.match(ln)
        if m:
            h, mi, s, _ = map(int, m.groups())
            t = h * 3600 + mi * 60 + s
            continue
        txt = re.sub(r"<[^>]+>", "", ln).strip()
        if t is None or not txt or txt == prev or txt.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        cues.append((t, unicodedata.normalize("NFC", txt)))
        prev = txt
    return cues


def load_phrases(min_len=6):
    """곡별 매칭 키: 고유 구절(6자 이상) + 후렴 앞머리(4자) + 곡명.

    공연 실황 자동 자막은 노래가 거의 [음악]으로 처리되고 조각만 남는다.
    그래서 짧은 후렴 머리와, 멘트에서 언급되는 곡명도 같이 본다.
    """
    songs = pd.read_csv(ROOT / "master/anchor_songs.csv")
    ph = pd.read_csv(ROOT / "master/anchor_phrases.csv")
    ph = ph[ph.n_songs == 1].merge(songs.loc[songs.tier <= 2, ["anchor_id", "title_norm"]])
    d = defaultdict(set)
    for t, p, k, rep_ in zip(ph.title_norm, ph.phrase, ph.phrase_key, ph.is_refrain):
        if len(k) >= min_len:
            d[t].add(phon(p))
        if rep_ and len(k) >= 4:
            d[t].add(phon(k[:4]))
    for t in songs.loc[songs.tier <= 2, "title_norm"]:
        name = re.sub(r"\s*\d+$", "", t)
        if len(name.replace(" ", "")) >= 3:
            d[t].add(phon(name))
    return d


def fmt(sec):
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vtt")
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--min-hits", type=int, default=2)
    ap.add_argument("--threshold", type=int, default=80)  # 실황 자막은 더 뭉개져서 단곡보다 낮춤
    args = ap.parse_args()

    cues = parse_vtt(Path(args.vtt))
    phrases = load_phrases()
    end = cues[-1][0] if cues else 0
    rows = []
    for w0 in range(0, end + 1, args.step):
        text = re.sub(r"\[[^\]]*\]|&gt;", "", " ".join(c for t, c in cues if w0 <= t < w0 + args.window))
        k = phon(text)
        if len(k) < 15:
            continue
        hits = {s: sum(fuzz.partial_ratio(p, k) >= args.threshold for p in ps) for s, ps in phrases.items()}
        best = max(hits, key=hits.get)
        if hits[best] >= args.min_hits:
            rows.append((w0, w0 + args.window, best, hits[best]))

    # 같은 곡의 겹치거나 이어지는 창을 합침
    segs = []
    for s, e, song, h in rows:
        if segs and segs[-1]["song"] == song and s <= segs[-1]["end"]:
            segs[-1]["end"] = e
            segs[-1]["max_hits"] = max(segs[-1]["max_hits"], h)
            segs[-1]["windows"] += 1
        else:
            segs.append({"start": s, "end": e, "song": song, "max_hits": h, "windows": 1})
    df = pd.DataFrame(segs)
    out = Path(args.vtt).with_suffix("").with_suffix(".segments.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"cues={len(cues)} duration≈{fmt(end)} windows_hit={len(rows)} segments={len(df)} -> {out}")
    for r in segs:
        sample = " / ".join(c for t, c in cues if r["start"] <= t < r["start"] + 20)[:90]
        print(f"  {fmt(r['start'])}–{fmt(r['end'])}  {r['song']:<12} hits={r['max_hits']} win={r['windows']}  | {sample}")


if __name__ == "__main__":
    main()
