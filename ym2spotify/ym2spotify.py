#!/usr/bin/env python3
"""Перенос лайкнутых треков из Яндекс Музыки в Spotify (раздел Liked Songs).

Как работает:
1. Забирает «Мне нравится» из Яндекс Музыки (неофициальная библиотека yandex-music).
2. Для каждого трека ищет пару в Spotify: сначала по ISRC, потом по названию и исполнителю.
3. Каждый кандидат получает оценку 0..1 (название, исполнитель, длительность).
4. Всё, что выше порога, добавляется в Liked Songs. Остальное попадает в CSV-отчёт для ручной проверки.

Результаты поиска кэшируются в .ym2spotify_state.json, поэтому после обрыва или лимита
Spotify можно просто запустить скрипт ещё раз.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from spotipy.exceptions import SpotifyException

STATE_FILE = Path(".ym2spotify_state.json")
REPORT_FILE = Path("ym2spotify_report.csv")
SCOPE = "user-library-read user-library-modify"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"

YM_CHUNK = 100            # сколько треков просить у Яндекса за раз
SP_CHUNK = 40             # сколько треков отправлять в Spotify за раз (с запасом)
SEARCH_LIMIT = 10         # дефолт spotipy; максимум для dev-приложений я не проверял
DEFAULT_MIN_SCORE = 0.75  # порог принятия для поиска по тексту
ISRC_MIN_SCORE = 0.5      # ISRC уже почти гарантия, порог мягче
MAX_RETRY_WAIT = 900      # дольше 15 минут ждать лимит не будем, сохраним прогресс и выйдем


@dataclass
class YmTrack:
    id: str
    title: str
    artists: list[str]
    version: str = ""
    duration_ms: Optional[int] = None
    isrc: Optional[str] = None
    liked_at: str = ""

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.title}"


@dataclass
class Candidate:
    uri: str
    score: float
    via: str  # isrc | fielded | text
    name: str = ""
    artists: list[str] = field(default_factory=list)

    def accepted(self, min_score: float) -> bool:
        threshold = ISRC_MIN_SCORE if self.via == "isrc" else min_score
        return self.score >= threshold


# ---------------------------------------------------------------- сопоставление

_FEAT = re.compile(r"[(\[]\s*(?:featuring|feat|ft|при участии|при уч)\b[^)\]]*[)\]]")


def norm(text: str) -> str:
    """Приводит строку к виду, удобному для сравнения."""
    text = text.casefold().replace("ё", "е")
    text = _FEAT.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_candidate(ym: YmTrack, item: dict) -> float:
    """Оценка совпадения трека Яндекса и результата поиска Spotify, от 0 до 1."""
    cand_title = norm(item.get("name", ""))
    titles = {norm(ym.title)}
    if ym.version:
        titles.add(norm(f"{ym.title} {ym.version}"))
    title_score = max(difflib.SequenceMatcher(None, t, cand_title).ratio() for t in titles)

    cand_artists = [a for a in (norm(a.get("name", "")) for a in item.get("artists", [])) if a]
    ym_artists = [a for a in (norm(a) for a in ym.artists) if a]
    if ym_artists:
        hits = sum(any(y == c or y in c or c in y for c in cand_artists) for y in ym_artists)
        artist_score = hits / len(ym_artists)
    else:
        artist_score = 0.5

    cand_ms = item.get("duration_ms")
    if ym.duration_ms and cand_ms:
        diff = abs(ym.duration_ms - cand_ms) / 1000
        duration_score = 1.0 if diff <= 3 else 0.5 if diff <= 10 else 0.0
    else:
        duration_score = 0.5

    return 0.5 * title_score + 0.3 * artist_score + 0.2 * duration_score


def build_queries(ym: YmTrack) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if ym.isrc:
        queries.append(("isrc", f"isrc:{ym.isrc}"))
    title = ym.title.replace('"', " ")
    if ym.artists:
        first = ym.artists[0].replace('"', " ")
        queries.append(("fielded", f'track:"{title}" artist:"{first}"'))
    queries.append(("text", f"{ym.title} {' '.join(ym.artists)}".strip()))
    return queries


def find_match(search: Callable[[str], list[dict]], ym: YmTrack, min_score: float) -> Optional[Candidate]:
    """Возвращает лучшего кандидата. Останавливается на первом запросе, где кандидат прошёл порог.

    Если никто не прошёл, возвращает лучшего из отвергнутых (он попадёт в отчёт как low_confidence).
    Если Spotify вообще ничего не нашёл, возвращает None.
    """
    best: Optional[Candidate] = None
    for via, query in build_queries(ym):
        scored = [(score_candidate(ym, item), item) for item in search(query)]
        if not scored:
            continue
        score, item = max(scored, key=lambda pair: pair[0])
        cand = Candidate(
            uri=item["uri"],
            score=round(score, 3),
            via=via,
            name=item.get("name", ""),
            artists=[a.get("name", "") for a in item.get("artists", [])],
        )
        if cand.accepted(min_score):
            return cand
        if best is None or cand.score > best.score:
            best = cand
    return best


# ---------------------------------------------------------------- Яндекс Музыка

def fetch_yandex_likes(token: str) -> list[YmTrack]:
    from yandex_music import Client

    client = Client(token).init()
    likes = client.users_likes_tracks()
    if not likes:
        return []

    short = list(likes.tracks)
    liked_at = {str(s.id): (s.timestamp or "") for s in short}
    ids = [s.track_id for s in short]

    result: list[YmTrack] = []
    for chunk in chunked(ids, YM_CHUNK):
        for t in client.tracks(chunk) or []:
            if t is None:
                continue
            result.append(
                YmTrack(
                    id=str(t.id),
                    title=t.title or "",
                    version=getattr(t, "version", None) or "",
                    artists=[a.name for a in (t.artists or []) if a and a.name],
                    duration_ms=t.duration_ms,
                    isrc=getattr(t, "isrc", None),
                    liked_at=liked_at.get(str(t.id), ""),
                )
            )
    return result


# ---------------------------------------------------------------- Spotify

def build_spotify():
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    auth = SpotifyOAuth(
        scope=SCOPE,
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        cache_path=".cache-ym2spotify",
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=20, retries=3)


def with_backoff(fn: Callable, *args, on_give_up: Callable[[], None] = lambda: None, **kwargs):
    """Вызывает fn, а при 429 ждёт Retry-After. Если ждать слишком долго, сохраняет прогресс и выходит."""
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except SpotifyException as e:
            if e.http_status != 429:
                raise
            wait = int((e.headers or {}).get("Retry-After", 30)) + 1
            if wait > MAX_RETRY_WAIT or attempt == 5:
                on_give_up()
                sys.exit(
                    f"\nSpotify просит подождать {wait} c. Прогресс сохранён, запусти скрипт позже, он продолжит с того же места."
                )
            print(f"  лимит Spotify, жду {wait} c...", flush=True)
            time.sleep(wait)


# ---------------------------------------------------------------- утилиты

def chunked(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def candidate_from_state(raw: Optional[dict]) -> Optional[Candidate]:
    return Candidate(**raw) if raw else None


def write_report(rows: Iterable[dict]) -> None:
    fields = ["status", "yandex", "spotify", "score", "via", "uri"]
    with REPORT_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Не задана переменная окружения {name}. См. README.md")
    return value


# ---------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Яндекс Музыка (лайки) -> Spotify (Liked Songs)")
    p.add_argument("--dry-run", action="store_true", help="только сопоставить и записать отчёт, ничего не добавлять")
    p.add_argument("--limit", type=int, help="взять только N последних лайков (для пробного прогона)")
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help=f"порог совпадения, по умолчанию {DEFAULT_MIN_SCORE}")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение перед добавлением")
    args = p.parse_args(argv)

    ym_token = require_env("YANDEX_MUSIC_TOKEN")
    require_env("SPOTIPY_CLIENT_ID")
    require_env("SPOTIPY_CLIENT_SECRET")

    print("Забираю лайки из Яндекс Музыки...")
    tracks = fetch_yandex_likes(ym_token)
    tracks.sort(key=lambda t: t.liked_at)  # от старых к новым, чтобы новые оказались сверху в Spotify
    if args.limit:
        tracks = tracks[-args.limit :]
    print(f"Треков: {len(tracks)}")
    if not tracks:
        return 0

    sp = build_spotify()
    state = load_state()

    def search(query: str) -> list[dict]:
        res = with_backoff(sp.search, q=query, type="track", limit=SEARCH_LIMIT, on_give_up=lambda: save_state(state))
        return res["tracks"]["items"]

    rows: list[dict] = []
    to_add: list[str] = []
    for i, ym in enumerate(tracks, 1):
        if ym.id in state:
            cand = candidate_from_state(state[ym.id])
        else:
            cand = find_match(search, ym, args.min_score)
            state[ym.id] = cand.__dict__ if cand else None
            if i % 50 == 0:
                save_state(state)
        if i % 25 == 0 or i == len(tracks):
            print(f"  сопоставлено {i}/{len(tracks)}", flush=True)

        if cand is None:
            status = "not_found"
        elif cand.accepted(args.min_score):
            status = "matched"
            to_add.append(cand.uri)
        else:
            status = "low_confidence"
        rows.append(
            {
                "status": status,
                "yandex": ym.label,
                "spotify": f"{', '.join(cand.artists)} - {cand.name}" if cand else "",
                "score": cand.score if cand else "",
                "via": cand.via if cand else "",
                "uri": cand.uri if cand else "",
            }
        )
    save_state(state)
    write_report(rows)

    stats = {s: sum(r["status"] == s for r in rows) for s in ("matched", "low_confidence", "not_found")}
    print(f"\nНайдено: {stats['matched']} | сомнительно: {stats['low_confidence']} | не найдено: {stats['not_found']}")
    print(f"Отчёт для ручной проверки: {REPORT_FILE}")

    if args.dry_run:
        print("--dry-run: в Spotify ничего не добавлено.")
        return 0
    to_add = list(dict.fromkeys(to_add))  # дубли убираем, порядок сохраняем
    if not to_add:
        return 0
    if not args.yes and input(f"Добавить {len(to_add)} треков в Liked Songs? [y/N] ").strip().lower() != "y":
        print("Отменено.")
        return 0

    new: list[str] = []
    for chunk in chunked(to_add, SP_CHUNK):
        saved = with_backoff(sp.current_user_saved_tracks_contains, tracks=chunk, on_give_up=lambda: save_state(state))
        new += [uri for uri, already in zip(chunk, saved) if not already]
    for n, chunk in enumerate(chunked(new, SP_CHUNK), 1):
        with_backoff(sp.current_user_saved_tracks_add, tracks=chunk, on_give_up=lambda: save_state(state))
        print(f"  добавлено {min(n * SP_CHUNK, len(new))}/{len(new)}", flush=True)
    print(f"Готово. Добавлено новых: {len(new)}, уже были в библиотеке: {len(to_add) - len(new)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
