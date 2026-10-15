"""total_metadata.csv → 유튜브 수집용 anchor 테이블 생성.

출력 (master/):
  anchor_songs.csv    곡 단위 anchor (정규화 곡명, 검색 키, 음악 속성)
  anchor_aliases.csv  곡명 변형 (괄호 속 별칭, 번호 제거형, 띄어쓰기 제거형)
  anchor_phrases.csv  가사 구절 단위 anchor (후렴 여부, 곡 간 빈도)
  name_collisions.csv 같은 검색 키를 공유하는 서로 다른 곡 (방아타령 등)

라벨러·금액 컬럼은 수집과 무관하므로 가져오지 않는다.
"""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "total_metadata.csv"
OUT = ROOT / "master"

KEEP = {
    "정제데이터 파일명": "source_id",
    "분류_업데이트": "group",
    "장르": "genre",
    "하위장르": "subgenre",
    "세부장르(optional)": "detail_genre",
    "곡명": "title_raw",
    "가창자": "singer",
    "가창자 성별": "singer_sex",
    "최종 서양조성": "key",
    "장단": "jangdan",
    "최종 박자": "meter",
    "최종 템포": "tempo",
    "moods_all": "moods",
    "timbres_all": "timbres",
    "캡션(영어)": "caption_en",
    "가사": "lyrics",
    "JSON_가사_모음": "lyric_segments",
}


def search_key(s: str) -> str:
    """공백·구두점 제거. 검색/매칭용 비교 키."""
    return re.sub(r"[\s\W_]+", "", s)


def normalize_title(raw: str):
    """'쾌지나 칭칭 나네 1' → ('쾌지나 칭칭 나네', [...별칭])"""
    aliases = []
    t = raw.strip()
    for inner in re.findall(r"\(([^)]*)\)", t):
        aliases.append((inner.strip(), "parenthetical"))
    t = re.sub(r"\s*\([^)]*\)\s*", " ", t).strip()
    t2 = re.sub(r"\s*\d+$", "", t).strip()  # 끝 번호 (1, 2) 제거
    if t2 != t:
        aliases.append((t, "numbered"))
    nospace = t2.replace(" ", "")
    if nospace != t2:
        aliases.append((nospace, "nospace"))
    return t2, aliases


def english_name(caption_en: str):
    """영문 캡션 첫 문장의 곡명 추정 ('Kwaejinachinchingnane is ...')."""
    if not isinstance(caption_en, str):
        return None
    m = re.match(r'^(?:The [^"]*?)?"?([A-Z][^"]*?)"?\s+(?:is|belongs|represents)\b', caption_en)
    if not m or re.match(r"(This|It|The)\b", m.group(1)):
        return None
    name = m.group(1).strip()
    # 가곡·시조처럼 설명형 문구가 잡힌 경우는 버림
    return name if len(name.split()) <= 3 and "," not in name else None


# 수집 우선순위. 1 = 통속민요·잘 알려진 향토민요 (핵심), 2 = 잡가·전래·병창·단가, 3 = 정가·궁중 (인접 장르)
TIER1 = {"경기민요", "서도민요", "남도민요", "강원도민요", "경상도민요", "제주민요",
         "경상도향토민요", "전라도 향토민요", "제주도 향토민요"}
TIER2 = {"경기잡가", "서도 잡가", "전래민요", "가야금병창곡", "단가"}
TRANSMISSION = {"경상도향토민요": "토속(유명)", "전라도 향토민요": "토속(유명)",
                "제주도 향토민요": "토속(유명)", "경기잡가": "잡가", "서도 잡가": "잡가",
                "전래민요": "전래", "가야금병창곡": "병창"}


def tier_of(detail_genre: str, subgenre: str) -> int:
    if detail_genre in TIER1:
        return 1
    if detail_genre in TIER2 or subgenre == "판소리":
        return 2
    return 3


def split_phrases(segments: str):
    return [p.strip() for p in re.split(r"\s*/\s*|\n", segments) if p.strip()]


def main():
    df = pd.read_csv(SRC).dropna(subset=["곡명"])
    df = df[list(KEEP)].rename(columns=KEEP).reset_index(drop=True)
    df["anchor_id"] = [f"A{i + 1:04d}" for i in range(len(df))]

    norm = df["title_raw"].map(normalize_title)
    df["title_norm"] = norm.map(lambda x: x[0])
    df["search_key"] = df["title_norm"].map(search_key)
    df["title_en"] = df["caption_en"].map(english_name)
    df["is_minyo"] = df["subgenre"].eq("민요")
    df["tier"] = [tier_of(d, g) for d, g in zip(df["detail_genre"], df["subgenre"])]
    df["transmission"] = [
        TRANSMISSION.get(d, "통속" if t == 1 else "인접") for d, t in zip(df["detail_genre"], df["tier"])
    ]

    alias_rows = []
    for aid, (_, als), title_en in zip(df["anchor_id"], norm, df["title_en"]):
        for a, kind in als:
            alias_rows.append({"anchor_id": aid, "alias": a, "alias_type": kind})
        if title_en:
            alias_rows.append({"anchor_id": aid, "alias": title_en, "alias_type": "english"})
    aliases = pd.DataFrame(alias_rows).drop_duplicates()

    # 가사 구절 anchor
    per_song = {}
    for aid, seg in zip(df["anchor_id"], df["lyric_segments"].fillna("")):
        per_song[aid] = Counter(search_key(p) for p in split_phrases(seg) if len(search_key(p)) >= 4)
    df_count = Counter(k for c in per_song.values() for k in c)  # 구절이 등장하는 곡 수
    text_of = {}
    for seg in df["lyric_segments"].fillna(""):
        for p in split_phrases(seg):
            text_of.setdefault(search_key(p), p)
    phrase_rows = []
    for aid, c in per_song.items():
        for k, n in c.items():
            phrase_rows.append({
                "anchor_id": aid,
                "phrase": text_of[k],
                "phrase_key": k,
                "count_in_song": n,
                "is_refrain": n >= 2,
                "n_songs": df_count[k],
                "char_len": len(k),
            })
    phrases = pd.DataFrame(phrase_rows)

    # 같은 검색 키, 다른 장르/지역 → 동명이곡 후보
    coll = (df.groupby("search_key")
              .agg(n=("anchor_id", "size"),
                   titles=("title_raw", lambda x: " | ".join(x)),
                   detail_genres=("detail_genre", lambda x: " | ".join(sorted(set(map(str, x))))))
              .query("n > 1").reset_index())
    coll["likely_homonym"] = coll["detail_genres"].str.contains(r"\|")

    OUT.mkdir(exist_ok=True)
    cols = ["anchor_id", "source_id", "title_raw", "title_norm", "search_key", "title_en",
            "is_minyo", "tier", "transmission", "genre", "subgenre", "detail_genre", "group", "singer", "singer_sex",
            "jangdan", "meter", "tempo", "key", "moods", "timbres", "lyrics"]
    df[cols].to_csv(OUT / "anchor_songs.csv", index=False, encoding="utf-8-sig")
    aliases.to_csv(OUT / "anchor_aliases.csv", index=False, encoding="utf-8-sig")
    phrases.to_csv(OUT / "anchor_phrases.csv", index=False, encoding="utf-8-sig")
    coll.to_csv(OUT / "name_collisions.csv", index=False, encoding="utf-8-sig")

    print(f"songs={len(df)} (민요 {df['is_minyo'].sum()}), unique keys={df['search_key'].nunique()}",
          f"aliases={len(aliases)}, phrases={len(phrases)} (refrain {phrases['is_refrain'].sum()})",
          f"tier counts={df['tier'].value_counts().sort_index().to_dict()}",
          f"collisions={len(coll)} (homonym {coll['likely_homonym'].sum()})", sep="\n", file=sys.stderr)


if __name__ == "__main__":
    main()
