"""목업 크롤링 결과를 anchor와 대조해 자동 라벨을 붙인다.

입력: mock/mock.sqlite, master/anchor_*.csv
출력: mock/mock_videos_labeled.csv, 콘솔 요약 (보고서 작성용)

자동 라벨 (모두 규칙 기반 1차 추정, 사람 검수 전):
  title_songs     제목·태그에서 찾은 anchor 곡
  lyric_songs     자막에서 곡 고유 구절이 잡힌 anchor 곡 (구절 히트 수)
  spectrum_guess  T(전통) / F(퓨전) / U(커버·아마추어) / M(현대·미디어) / ?(미상)
  credit_mention  설명란에 원곡·편곡 등 크레딧 표기가 있는지
"""
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "mock"
MASTER = ROOT / "master"

# 유튜브 통용 표기 → anchor 표기 (anchor_aliases에 아직 없는 것)
EXTRA_ALIASES = {"늴리리야": "닐니리야", "닐리리야": "닐니리야", "도라지타령": "도라지",
                 "쾌지나칭칭": "쾌지나 칭칭 나네", "칭칭나네": "쾌지나 칭칭 나네",
                 "옹혜야": "옹헤야", "닐니리": "닐니리야", "늴리리": "닐니리야"}

SPECTRUM_RULES = [  # 위에서부터 먼저 걸리는 것
    ("M", r"\bOST\b|드라마|광고|아이돌"),
    ("F", r"퓨전|fusion|밴드|band|EDM|remix|리믹스|재즈|jazz|록밴드|rock|힙합|hip-?hop|일렉|크로스오버|조선팝|풍류대장|조선판스타"),
    ("U", r"커버|cover|부르기|불러봤|배우기|따라|연습|초보|레슨|강좌|ASMR"),
    ("T", r"명창|보유자|이수자|전수|국악원|국립|국악단|정기공연|경기민요|서도민요|남도민요|무형|보존회|국악방송"),
]
MEDIA_RULES = [  # 오디오 품질 관점의 영상 형태. 위에서부터 먼저 걸리는 것
    ("topic_audio", lambda r, t: (r.channel or "").endswith(" - Topic") or (r.description or "").startswith("Provided to YouTube")),
    ("official_mv", lambda r, t: re.search(r"\bM/?V\b|뮤직\s?비디오|music video|official (video|audio)|공식\s?(영상|음원)", t, re.I)),
    ("lesson", lambda r, t: re.search(r"배우기|배웁시다|강좌|레슨|강습|따라\s?부르|가르쳐|교실|수업", t)),
    ("broadcast", lambda r, t: re.search(r"KBS|MBC|SBS|JTBC|EBS|MBN|TV조선|국악방송|국악한마당|불후의|풍류대장|조선판스타|가요무대|전국노래자랑", t)),
    ("live_stage", lambda r, t: re.search(r"공연|실황|무대|라이브|live|콘서트|concert|축제|페스티벌|정기연주회|발표회", t, re.I)),
]
CREDIT_RE = re.compile(r"원곡|편곡|작곡|작사|arr\.|arranged|민요|전래|traditional", re.I)


def nfc(s):
    # 맥에서 올린 제목은 한글이 NFD(자모 분해)로 들어오는 경우가 있다
    return unicodedata.normalize("NFC", s or "")


# 자동 자막에서 자주 뒤바뀌는 모음을 하나로 (ㅐ/ㅔ, ㅒ/ㅖ, ㅙ/ㅚ/ㅞ)
_VOWEL_MERGE = str.maketrans({"\u1162": "\u1166", "\u1164": "\u1168", "\u116b": "\u1170", "\u116c": "\u1170"})


def phon(s):
    """발음 근사 키: 공백 제거 + 혼동 모음 통합 (자모 단위로 비교)."""
    return unicodedata.normalize("NFD", key(s)).translate(_VOWEL_MERGE)


FUZZ_THRESHOLD = 85


def key(s):
    return re.sub(r"[\s\W_]+", "", nfc(s))


def main():
    con = sqlite3.connect(MOCK / "mock.sqlite")
    v = pd.read_sql("SELECT * FROM video WHERE status='ok'", con)
    err = pd.read_sql("SELECT * FROM video WHERE status!='ok'", con)
    subs = pd.read_sql("SELECT * FROM subtitle", con).set_index("video_id")["text"].to_dict()
    vq = pd.read_sql("SELECT vq.video_id, q.song, q.axis, vq.rank FROM video_query vq JOIN queries q USING(query)", con)
    n_cand = vq.video_id.nunique()

    songs = pd.read_csv(MASTER / "anchor_songs.csv")
    aliases = pd.read_csv(MASTER / "anchor_aliases.csv")
    phrases = pd.read_csv(MASTER / "anchor_phrases.csv")
    minyo = songs[songs.tier <= 2]

    # 곡명 사전: 검색키 → title_norm
    name_dict = {}
    for _, r in minyo.iterrows():
        if len(r.search_key) >= 3:
            name_dict[r.search_key] = r.title_norm
    al = aliases.merge(minyo[["anchor_id", "title_norm"]])
    for _, r in al[al.alias_type != "english"].iterrows():
        k = key(r.alias)
        if len(k) >= 3:
            name_dict.setdefault(k, r.title_norm)
    for a, t in EXTRA_ALIASES.items():
        name_dict[key(a)] = t

    # 곡 고유 구절 (다른 곡과 공유하지 않는, 6자 이상)
    ph = phrases[(phrases.n_songs == 1) & (phrases.char_len >= 6)].merge(minyo[["anchor_id", "title_norm"]])
    ph_dict = defaultdict(set)
    for _, r in ph.iterrows():
        ph_dict[r.title_norm].add(phon(r.phrase))

    rows = []
    for _, r in v.iterrows():
        tags = " ".join(json.loads(r.tags or "[]"))
        meta_text = f"{r.title} {tags}"
        mk = key(meta_text)
        title_songs = sorted({t for k, t in name_dict.items() if k in mk})
        sub_k = phon(subs.get(r.video_id, "").replace("[음악]", ""))
        # 자동 자막은 가사가 뭉개지므로 부분 문자열 유사도로 매칭
        lyric_hits = ({t: sum(fuzz.partial_ratio(p, sub_k) >= FUZZ_THRESHOLD for p in ps)
                       for t, ps in ph_dict.items()} if len(sub_k) > 20 else {})
        lyric_hits = {t: n for t, n in lyric_hits.items() if n > 0}
        full_text = nfc(f"{r.title} {tags} {(r.description or '')[:1500]}")  # 채널명은 오탐이 많아 제외
        spec = next((c for c, pat in SPECTRUM_RULES if re.search(pat, full_text, re.I)), "?")
        if r.is_short:
            spec += "/short"
        media = "short" if r.is_short else next(
            (m for m, f in MEDIA_RULES if f(r, nfc(f"{r.title} {tags} {r.channel} {(r.description or '')[:600]}"))), "other")
        q = vq[vq.video_id == r.video_id]
        rows.append({
            "video_id": r.video_id, "title": nfc(r.title), "nfd_title": r.title != nfc(r.title), "channel": r.channel,
            "query_song": "|".join(sorted(set(q.song))), "query_axis": "|".join(sorted(set(q.axis))),
            "upload_year": str(r.upload_date)[:4], "duration": r.duration, "views": r.view_count,
            "is_short": r.is_short, "sub_kind": "manual" if r.has_ko_manual else ("auto" if r.has_ko_auto else "none"),
            "sub_saved": r.video_id in subs,
            "title_songs": "|".join(title_songs),
            "lyric_songs": "|".join(f"{t}({n})" for t, n in sorted(lyric_hits.items(), key=lambda x: -x[1])),
            "lyric_top": max(lyric_hits, key=lyric_hits.get) if lyric_hits else "",
            "spectrum_guess": spec,
            "media_form": media,
            "credit_mention": bool(CREDIT_RE.search(r.description or "")),
            "url": f"https://youtu.be/{r.video_id}",
        })
    df = pd.DataFrame(rows)
    df["query_song_in_title"] = [
        any(name_dict.get(key(s), s) in ts.split("|") or key(s) in key(t) for s in qs.split("|"))
        for qs, ts, t in zip(df.query_song, df.title_songs, df.title)
    ]
    df["lyric_confirms_query"] = [
        any(name_dict.get(key(s), s) == lt for s in qs.split("|")) for qs, lt in zip(df.query_song, df.lyric_top)
    ]
    df.to_csv(MOCK / "mock_videos_labeled.csv", index=False, encoding="utf-8-sig")

    # ---- 요약 ----
    raw_bytes = sum(p.stat().st_size for p in (MOCK / "raw").glob("*.gz"))
    sub_bytes = sum(p.stat().st_size for p in (MOCK / "subs").glob("*.vtt"))
    db_bytes = (MOCK / "mock.sqlite").stat().st_size
    p = lambda *a: print(*a)
    p(f"candidates={n_cand} fetched_ok={len(df)} errors={len(err)}")
    if len(err):
        p(err.status.str[:90].value_counts().to_string())
    p(f"storage: raw.gz={raw_bytes/1e6:.2f}MB subs={sub_bytes/1e6:.2f}MB db={db_bytes/1e6:.2f}MB "
      f"per-video raw={raw_bytes/max(len(df),1)/1e3:.1f}KB sub={sub_bytes/max(df.sub_saved.sum(),1)/1e3:.1f}KB")
    p("duration sec:", df.duration.describe()[["mean", "50%", "min", "max"]].round(0).to_dict(),
      "shorts:", int(df.is_short.sum()))
    p("subtitle kind:", df.sub_kind.value_counts().to_dict(), "saved:", int(df.sub_saved.sum()))
    p("query axis:", df.query_axis.value_counts().to_dict())
    p("spectrum_guess:\n", pd.crosstab(df.spectrum_guess, df.query_axis).to_string())
    p("media_form:\n", pd.crosstab(df.media_form, df.query_axis).to_string())
    p("query song found in title/tags:", f"{df.query_song_in_title.mean():.0%}")
    p("lyric phrase hit (any):", f"{(df.lyric_top != '').mean():.0%}",
      "| among saved subs:", f"{(df[df.sub_saved].lyric_top != '').mean():.0%}",
      "| lyric top == query song:", f"{df.lyric_confirms_query.mean():.0%}")
    p("NFD-encoded titles:", int(df.nfd_title.sum()))
    p("credit mention in desc:", f"{df.credit_mention.mean():.0%}")
    p("upload year:", df.upload_year.value_counts().sort_index().to_dict())
    p("views median by axis:", df.groupby("query_axis").views.median().to_dict())
    p("top channels:", df.channel.value_counts().head(10).to_dict())
    p("\nno title match (possible noise):")
    p(df[~df.query_song_in_title][["query_song", "title", "channel"]].head(15).to_string())
    p("\nlyric hit != query song:")
    x = df[(df.lyric_top != "") & ~df.lyric_confirms_query]
    p(x[["query_song", "lyric_songs", "title"]].head(15).to_string())
    p("\nfusion examples:")
    p(df[df.spectrum_guess.str.startswith(("F", "M"))][["query_song", "title", "channel", "views"]].head(20).to_string())


if __name__ == "__main__":
    main()
