#!/usr/bin/env python3

"""
Matches this repo's DAT files to IGDB and Hasheous,
and derives metadata for RetroArch's `.rdb` databases from them.

- `index` joins all three sources into one SQLite database.
- `generate` writes one DAT file per playlist to `lookatalldat/`,
  holding whatever the index can add to the entries that RetroArch's databases already have.
  libretro-super's `libretro-build-database.sh` compiles it after every other DAT file.

DAT entries are only ever matched by the identifiers that RetroArch itself uses,
i.e. by CRC32 or by serial; never by name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import tomllib

from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import aiofiles
import pe
import pycountry

from aiomultiprocess.types import ProxyException

from pydantic import AliasChoices, BaseModel, DirectoryPath, Field, FilePath
from pydantic_settings import BaseSettings, CliSubCommand, SettingsConfigDict, CliApp
from sqlalchemy import CheckConstraint, ForeignKey, MetaData, Column, String, Index, column, select, text, true
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql.functions import coalesce, count

from dats import DAT_OBJECT_TYPES, CompiledEntry, DatPair, DatTopLevelRecord, compile_dats_async, encode_dat, get_dat_match, index_dats, write_dat_key_index, Game as DatGame, Rom as DatRom
from igdb import ANALOG_KEYWORD_IDS, KEYWORD_OVERRIDES, RUMBLE_KEYWORD_IDS, Playlist, PlaylistConfig, IGDB_OBJECT_TYPES, index_igdb
from hasheous import HASHEOUS_OBJECT_TYPES, index_hasheous
from utils import CliTuple, DEFAULT_DAT_CONCURRENCY, DEFAULT_HASHEOUS_CONCURRENCY, DEFAULT_IGDB_CONCURRENCY, IndexArgs, PlaylistArgs, PoolArgs, RowId, Sha256, VerboseArgs, create_db, create_deferred_indexes, DatabaseModel, Crc, Md5, Sha1, db_transaction

class AllRoms(DatabaseModel, frozen=True):
    __tablename__ = "AllRoms"
    __tableargs__ = (
        CheckConstraint("dat_rom IS NOT NULL OR hasheous_rom IS NOT NULL", name="dat_or_hasheous_rom_not_null"),
        Index("ix_AllRoms", "dat_rom", "hasheous_rom", "crc", "serial", "md5", "sha1", unique=True),
        Index("ix_AllRoms_dat_rom_where_not_null", "dat_rom", unique=True, sqlite_where=column("dat_rom").is_not(None)),
        # Not unique: several DAT ROMs may share a serial and so match the same Hasheous ROM.
        Index("ix_AllRoms_hasheous_rom_where_not_null", "hasheous_rom", sqlite_where=column("hasheous_rom").is_not(None)),
        Index("ix_AllRoms_dat_rom_crc_where_not_null", "dat_rom", "crc", unique=True, sqlite_where=column("crc").is_not(None) & column("dat_rom").is_not(None)),
        Index("ix_AllRoms_hasheous_crc_where_not_null", "hasheous_rom", "crc", unique=True, sqlite_where=column("crc").is_not(None) & column("hasheous_rom").is_not(None)),
        Index("ix_AllRoms_serial_where_not_null", "serial", sqlite_where=column("serial").is_not(None)),
        Index("ix_AllRoms_dat_rom_md5_where_not_null", "dat_rom", "md5", unique=True, sqlite_where=column("md5").is_not(None) & column("dat_rom").is_not(None)),
        Index("ix_AllRoms_hasheous_rom_md5_where_not_null", "hasheous_rom", "md5", unique=True, sqlite_where=column("md5").is_not(None) & column("hasheous_rom").is_not(None)),
        Index("ix_AllRoms_dat_rom_sha1_where_not_null", "dat_rom", "sha1", unique=True, sqlite_where=column("sha1").is_not(None) & column("dat_rom").is_not(None)),
        Index("ix_AllRoms_hasheous_rom_sha1_where_not_null", "hasheous_rom", "sha1", unique=True, sqlite_where=column("sha1").is_not(None) & column("hasheous_rom").is_not(None)),
        # None of the DAT files have SHA256 hashes, so we don't need a column or index for that,
        # but having the SHA256 from a Hasheous object can help us with matching
        Index("ix_AllRoms_hasheous_rom_sha256_where_not_null", "hasheous_rom", "sha256", unique=True, sqlite_where=column("sha256").is_not(None) & column("hasheous_rom").is_not(None)),
    )

    # These columns are deliberately not unique on their own.
    # Hasheous lists the same hash under several ROM entries
    # (see RomItem's class docstring), and some of those hashes are junk
    # (over a thousand rows carry the SHA-256 of an empty file),
    # so the uniqueness that this table actually relies on
    # is declared as the partial composite indexes in __tableargs__ above.
    dat_rom: Annotated[RowId | None, Column(ForeignKey("DatRom.rowid"), nullable=True, index=True)] = None
    hasheous_rom: Annotated[int | None, Column(ForeignKey("HasheousRomItem.id"), nullable=True, index=True)] = None
    crc: Annotated[Crc | None, Column(String, nullable=True, index=True)] = None
    serial: Annotated[str | None, Column(String, nullable=True, index=True)] = None
    md5: Annotated[Md5 | None, Column(String, nullable=True, index=True)] = None
    sha1: Annotated[Sha1 | None, Column(String, nullable=True, index=True)] = None
    sha256: Annotated[Sha256 | None, Column(String, nullable=True, index=True)] = None

PARENT_DIR = Path(__file__).parent.parent
class CommonArgs:
    igdb_path: DirectoryPath = Field(
        default=PARENT_DIR / 'tmp' / 'igdb',
        description="Path to the directory containing IGDB JSON files fetched with `igdb.py fetch`.",
        validation_alias=AliasChoices('i', 'igdb'),
        validate_default=True,
    )

    hasheous_path: DirectoryPath = Field(
        default=PARENT_DIR / 'tmp' / 'hasheous',
        description="Path to the directory containing Hasheous ZIP dumps fetched with `hasheous.py fetch`.",
        validation_alias=AliasChoices('s', 'hasheous'),
        validate_default=True,
    )

    dat_dirs: CliTuple[DirectoryPath] = Field(
        default=(PARENT_DIR / 'dat', PARENT_DIR / 'metadat',),
        description="Paths to the directories containing existing DAT files to scan for games to process.",
        validation_alias=AliasChoices('d', 'dat'),
        validate_default=True,
    )

OUTDIR_NAME = "lookatalldat"

BUILD_DAT_DIRS: tuple[str, ...] = (
    "metadat",
    "metadat/goodtools",
    "metadat/analog",
    "metadat/barcode",
    "metadat/bbfc",
    "metadat/developer",
    "metadat/elspa",
    "metadat/esrb",
    "metadat/franchise",
    "metadat/magazine/famitsu",
    "metadat/magazine/edge",
    "metadat/magazine/edge_review",
    "metadat/maxusers",
    "metadat/origin",
    "metadat/publisher",
    "metadat/releasemonth",
    "metadat/releaseyear",
    "metadat/genre",
    "metadat/rumble",
    "metadat/serial",
    "metadat/enhancement_hw",
    "metadat/tgdb",
    "metadat/headered",
    "metadat/hacks",
    "metadat/homebrew",
    "metadat/mame-nonmerged",
    "metadat/mame-split",
    "metadat/mame-member",
    "metadat/mame",
    "metadat/fbneo-merged",
    "metadat/fbneo-split",
    "metadat/fbneo-member",
    "metadat/tosec",
    "metadat/libretro-dats",
    "metadat/redump",
    "metadat/no-intro",
    "dat",
)
"""
The directories that libretro-super's `libretro-build-database.sh` looks in for `<rdb name>.dat`,
relative to the root of this repo.
See https://github.com/libretro/libretro-super/blob/master/libretro-build-database.sh

Listed in the order the script passes them to `c_converter`,
which is also their precedence from lowest to highest:
when two files describe the same entry, the later one wins.
The script reads `lookatalldat` last of all, so it isn't listed here.
"""


def rdb_source_dats(root: Path, title: str) -> tuple[Path, ...]:
    """
    Returns the DAT files that `libretro-build-database.sh` compiles into `<title>.rdb`
    (not counting the one that `generate` writes), in the order it compiles them.
    """
    return tuple(
        path
        for directory in BUILD_DAT_DIRS
        if (path := root / directory / f"{title}.dat").is_file()
    )


def match_key(playlist: Playlist) -> str:
    """
    The field that `c_converter` identifies a playlist's `.rdb` entries by.

    This has to agree with the match key that `libretro-build-database.sh` gives the playlist,
    as it does for every playlist in `playlists.toml` that the script builds.
    """
    return f"rom.{playlist.id_type}"


RDB_FIELDS: tuple[str, ...] = (
    "developer",
    "publisher",
    "genre",
    "franchise",
    "perspective",
    "releaseyear",
    "releasemonth",
    "users",
    "coop",
    "rumble",
    "analog",
    "origin",
    "esrb_rating",
    "pegi_rating",
    "cero_rating",
    "platform_exclusive",
    "console_exclusive",
    "achievements",
    "tags",
    "language",
    "region",
)
"""
The fields that generated DAT files may contain besides each entry's name and key,
in the order they're written.

Each one is compiled by `c_converter` (see `rdb_mappings` in `libretro-db/c_converter.c`)
and read back by RetroArch (see `database_info.c` and `menu/menu_explore.c`).
Only `tags` needs a `c_converter` newer than upstream's, which ignores it.

Deliberately left out:

- `bbfc_rating`: RetroArch reads it, but `c_converter` never writes it.
- `elspa_rating`: ELSPA stopped rating games in 2003, and IGDB doesn't track it.
- `description`, `score`, `category`, `media`, `controls`, `artstyle`,
  `gameplay`, `narrative`, `pacing`, `setting`, `visual`, `vehicular`:
  none of our sources has a field that means the same thing.
"""

MULTI_VALUE_SEPARATOR = " / "
"""
Joins multiple values in one field.

Matches the convention of the existing DAT files (e.g. `developer "Capcom / Arika"`),
and `menu_explore.c` splits on it.
"""

TAG_SEPARATORS = re.compile(r"[/,|]")
"""
The characters that `menu_explore.c` splits multi-valued fields on.

A few IGDB keywords contain them (e.g. "day/night cycle"),
which would turn into tags that don't exist.
"""

IGDB_REGIONS_BY_DAT_REGION: Mapping[str, tuple[int, ...]] = {
    # IGDB release regions: 1 europe, 2 north_america, 3 australia, 4 new_zealand, 5 japan,
    # 6 china, 7 asia, 8 worldwide, 9 korea, 10 brazil
    "USA": (2,),
    "Canada": (2,),
    "Europe": (1,),
    "UK": (1,), "United Kingdom": (1,), "Germany": (1,), "France": (1,), "Spain": (1,),
    "Italy": (1,), "Netherlands": (1,), "Sweden": (1,), "Scandinavia": (1,), "Denmark": (1,),
    "Norway": (1,), "Finland": (1,), "Poland": (1,), "Portugal": (1,), "Greece": (1,),
    "Austria": (1,), "Switzerland": (1,), "Belgium": (1,), "Russia": (1,),
    "Japan": (5,),
    "Australia": (3,),
    "New Zealand": (4,),
    "China": (6,), "Hong Kong": (6, 7), "Taiwan": (7,),
    "Asia": (7,),
    "Korea": (9,),
    "Brazil": (10,),
    "World": (8,),
}
"""Maps the regions that DAT files name (mostly No-Intro's) to IGDB's release regions."""

DAT_REGIONS: frozenset[str] = frozenset((
    *IGDB_REGIONS_BY_DAT_REGION,
    "Argentina", "Belarus", "Bosnia and Herzegovina", "Chile", "Croatia", "Czech Republic",
    "Estonia", "Hungary", "Iceland", "India", "Ireland", "Israel", "Latin America", "Latvia",
    "Lithuania", "Mexico", "Peru", "Romania", "Serbia", "Singapore", "Slovakia", "Slovenia",
    "South Africa", "Turkey", "Ukraine", "United Arab Emirates", "Yugoslavia",
))
"""Every region that a No-Intro or Redump name may list."""

REGION_ALIASES: Mapping[str, str] = {
    "UK": "United Kingdom",
    "United States": "USA",
    "Worldwide": "World",
    "South Korea": "Korea",
}
"""
Region names that the DAT files' own `region` fields spell differently.
Most of the DAT files spell the UK "United Kingdom", even though No-Intro names use "(UK)".
"""

REGIONS_BY_COUNTRY_CODE: Mapping[str, str] = {
    "US": "USA",
    "EU": "Europe",
    "World": "World",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "KR": "Korea",
    "AS": "Asia",
    "Asia": "Asia",
}
"""
Hasheous's country codes for the regions whose names it spells differently than the DAT files.
The names of all other countries already match.
"""

REGION_LANGUAGES: Mapping[str, frozenset[str]] = {
    "USA": frozenset(("English",)),
    "United Kingdom": frozenset(("English",)),
    "Ireland": frozenset(("English",)),
    "Australia": frozenset(("English",)),
    "New Zealand": frozenset(("English",)),
    "Canada": frozenset(("English", "French")),
    "Japan": frozenset(("Japanese",)),
    "Korea": frozenset(("Korean",)),
    "China": frozenset(("Chinese",)),
    "Taiwan": frozenset(("Chinese",)),
    "Hong Kong": frozenset(("Chinese", "English")),
    "Germany": frozenset(("German",)),
    "Austria": frozenset(("German",)),
    "France": frozenset(("French",)),
    "Spain": frozenset(("Spanish",)),
    "Mexico": frozenset(("Spanish",)),
    "Argentina": frozenset(("Spanish",)),
    "Italy": frozenset(("Italian",)),
    "Netherlands": frozenset(("Dutch",)),
    "Portugal": frozenset(("Portuguese",)),
    "Brazil": frozenset(("Portuguese",)),
    "Sweden": frozenset(("Swedish",)),
    "Denmark": frozenset(("Danish",)),
    "Norway": frozenset(("Norwegian",)),
    "Finland": frozenset(("Finnish",)),
    "Poland": frozenset(("Polish",)),
    "Russia": frozenset(("Russian",)),
    "Greece": frozenset(("Greek",)),
    "Czech Republic": frozenset(("Czech",)),
    "Hungary": frozenset(("Hungarian",)),
    "Turkey": frozenset(("Turkish",)),
}
"""
The languages that a region's releases are normally in,
for the regions that have only one or two.

What IGDB or Hasheous say about a whole game's languages
is only trusted for a dump that's from one of these regions
if it agrees with them; IGDB often lists only the language of a game's original release.
"""

TRANSLATION_TAG = re.compile(r"[(\[]T[-+]")
"""
Marks a fan translation in No-Intro (e.g. "(T-En by ...)") or GoodTools (e.g. "[T+Eng]") names.

A translation isn't in the languages of the game it translates.
"""

IGDB_RELEASE_REGIONS: Mapping[int, str] = {
    1: "Europe",
    2: "USA",
    3: "Australia",
    4: "New Zealand",
    5: "Japan",
    6: "China",
    7: "Asia",
    8: "World",
    9: "Korea",
    10: "Brazil",
}
"""IGDB's release regions, spelled the way the DAT files spell them."""

WORLDWIDE = 8

PRECISE_MONTH_FORMATS = frozenset((0, 1))
"""IGDB date formats `YYYYMMDD` and `YYYYMM`; the others don't pin down a month."""

SINGLE_PLAYER_MODE = 1
COOP_MODE = 3

CONSOLE_PLATFORM_TYPES = frozenset((1, 5))
"""IGDB's platform types for consoles and portable consoles."""

PLATFORM_TYPE_OVERRIDES: Mapping[int, int] = {
    47: 1,  # Virtual Console, which IGDB calls a "platform"
    133: 1,  # Odyssey 2 / Videopac G7000, which IGDB calls a computer
    150: 1,  # Turbografx-16/PC Engine CD, which IGDB calls a computer
    377: 1,  # Plug & Play, which IGDB calls a "platform"
}
"""IGDB platforms that are consoles, but that IGDB doesn't say are."""

class RatingBoard(NamedTuple):
    field: str
    founded: int
    """The year the board started rating games; a release before then was never rated by it."""

    regions: frozenset[int]
    """The IGDB release regions that the board rates games for."""


RATING_BOARDS: Mapping[str, RatingBoard] = {
    "ESRB": RatingBoard("esrb_rating", 1994, frozenset((2,))),
    "PEGI": RatingBoard("pegi_rating", 2003, frozenset((1,))),
    "CERO": RatingBoard("cero_rating", 2002, frozenset((5,))),
}
"""
IGDB records age ratings per game rather than per release,
so a rating may well belong to a re-release on some later platform
(e.g. the SNES's Chrono Trigger carries the E10+ of its DS port).
A rating is only kept for an entry released in the board's region
after the board existed, on the playlist's platform.
"""

IGNORED_AGE_RATINGS = frozenset(("RP",))
"""ESRB's "Rating Pending" isn't a rating."""

ORIGIN_OVERRIDES: Mapping[str, str] = {
    "826": "UK",
    "840": "US",
}
"""
Country names that the existing `metadat/origin` DATs spell differently than ISO 3166 does.
Keyed by ISO 3166-1 numeric code.
"""


def playlist_igdb_platforms(playlist: Playlist) -> frozenset[int] | None:
    """
    Returns the IGDB platforms that a playlist's query selects games by,
    or None if it selects them some other way (e.g. by engine or ID).
    """
    where = playlist.igdb_query.where or ""
    match = re.search(r"\bplatforms\s*=\s*\(([\d,\s]+)\)", where)
    if not match:
        return None

    return frozenset(int(p) for p in match[1].split(",") if p.strip())


def language_name(name: str | None) -> str | None:
    """
    Returns a language's name without its regional variant (e.g. IGDB's "Spanish (Mexico)"),
    or None if it's a code or a combination rather than a name (e.g. Hasheous's "Pt-BR" or "Multi-5").
    """
    if not name:
        return None

    name = re.sub(r"\s*\([^()]*\)$", "", name)
    return name if len(name) > 2 and re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)*", name) else None


def country_region(code: str | None, name: str | None) -> str | None:
    """
    Spells one of Hasheous's countries the way the DAT files spell regions,
    or returns None if it isn't a country at all (e.g. "Unset", or a bare code like "ss").
    """
    if code and (region := REGIONS_BY_COUNTRY_CODE.get(code)):
        return region

    if name and name != "Unset" and re.fullmatch(r"[A-Z][A-Za-z]*(?: [A-Za-z]+)*", name):
        return REGION_ALIASES.get(name, name)

    return None


def hasheous_game_region(value: str) -> str | None:
    """Parses one of the countries that Hasheous lists for a whole game, e.g. "Japan (JP)"."""
    if match := re.fullmatch(r"(.*?)\s*\(([^()]*)\)", value):
        return country_region(match[2], match[1])

    return country_region(None, value)


@dataclass(frozen=True, slots=True)
class ReleaseDate:
    platform: int
    region: int
    year: int
    month: int | None
    format: int


@dataclass(frozen=True, slots=True)
class MultiplayerMode:
    platform: int | None
    max_players: int | None
    coop: bool


@dataclass(slots=True)
class IgdbInfo:
    name: str
    franchise: str | None = None
    franchises: list[str] = field(default_factory=list)
    developers: list[str] = field(default_factory=list)
    developer_countries: set[str | None] = field(default_factory=set)
    publishers: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    perspectives: list[str] = field(default_factory=list)
    platforms: set[int] = field(default_factory=set)
    game_modes: set[int] = field(default_factory=set)
    keywords: set[int] = field(default_factory=set)
    languages: list[str] = field(default_factory=list)
    releases: list[ReleaseDate] = field(default_factory=list)
    multiplayer: list[MultiplayerMode] = field(default_factory=list)
    age_ratings: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


class HasheousLink(NamedTuple):
    """One Hasheous game that lists a ROM, as recorded in the `AllRoms` table."""

    game: int
    igdb: int | None
    retroachievements: int | None
    dump: str

    countries: tuple[str, ...]
    """The regions that Hasheous says this very ROM was released in, most prominent first."""

    languages: tuple[str, ...]
    """The languages that Hasheous says this very ROM is in."""


@dataclass
class Catalog:
    """Everything that the generated DAT files are derived from, loaded into memory."""

    igdb: dict[int, IgdbInfo]
    igdb_playlists: dict[str, frozenset[int]]
    links_by_crc: dict[str, list[HasheousLink]]
    links_by_serial: dict[str, list[HasheousLink]]

    tags: dict[int, str]
    """The tag that each IGDB keyword becomes, if any."""

    platform_types: dict[int, int | None]
    """Each IGDB platform's type (see `CONSOLE_PLATFORM_TYPES`)."""

    hasheous_countries: dict[int, frozenset[str]]
    """The regions that Hasheous lists for each of its games, across all of the game's ROMs."""

    hasheous_languages: dict[int, frozenset[str]]
    """The languages that Hasheous lists for each of its games, across all of the game's ROMs."""

    @classmethod
    async def load(cls, connection: AsyncConnection) -> Catalog:
        async def rows(query: str) -> Iterable[Any]:
            return (await connection.execute(text(query))).all()

        igdb: dict[int, IgdbInfo] = {}
        franchise_names = {id: name for id, name in await rows("SELECT id, name FROM IgdbFranchise")}

        for id, name, franchise in await rows("SELECT id, name, franchise FROM IgdbGame"):
            igdb[id] = IgdbInfo(name=name, franchise=franchise_names.get(franchise))

        for game, franchise in await rows("SELECT IgdbGame_franchises_id, IgdbFranchise_id FROM IgdbGame_franchises ORDER BY IgdbFranchise_id"):
            if (info := igdb.get(game)) and franchise in franchise_names:
                info.franchises.append(franchise_names[franchise])

        for game, name in await rows(
            "SELECT r.IgdbGame_genres_id, g.name FROM IgdbGame_genres r "
            "JOIN IgdbGenre g ON g.id = r.IgdbGenre_id ORDER BY g.name"
        ):
            if info := igdb.get(game):
                info.genres.append(name)

        for game, name in await rows(
            "SELECT r.IgdbGame_player_perspectives_id, p.name FROM IgdbGame_player_perspectives r "
            "JOIN IgdbPlayerPerspective p ON p.id = r.IgdbPlayerPerspective_id ORDER BY p.id"
        ):
            if info := igdb.get(game):
                info.perspectives.append(name)

        for game, platform in await rows("SELECT IgdbGame_platforms_id, IgdbPlatform_id FROM IgdbGame_platforms"):
            if info := igdb.get(game):
                info.platforms.add(platform)

        for game, mode in await rows("SELECT IgdbGame_game_modes_id, IgdbGameMode_id FROM IgdbGame_game_modes"):
            if info := igdb.get(game):
                info.game_modes.add(mode)

        for game, keyword in await rows("SELECT IgdbGame_keywords_id, IgdbKeyword_id FROM IgdbGame_keywords"):
            if info := igdb.get(game):
                info.keywords.add(keyword)

        for game, name in await rows(
            "SELECT r.IgdbGame_language_supports_id, l.name FROM IgdbGame_language_supports r "
            "JOIN IgdbLanguageSupport s ON s.id = r.IgdbLanguageSupport_id "
            "JOIN IgdbLanguage l ON l.id = s.language ORDER BY l.id"
        ):
            # Whether a language is supported in audio, subtitles, or the interface makes no difference here
            if (info := igdb.get(game)) and (name := language_name(name)) and name not in info.languages:
                info.languages.append(name)

        for game, name, country, developer, publisher in await rows(
            "SELECT ic.game, c.name, c.country, ic.developer, ic.publisher FROM IgdbInvolvedCompany ic "
            "JOIN IgdbCompany c ON c.id = ic.company ORDER BY ic.id"
        ):
            if not (info := igdb.get(game)):
                continue
            if developer and name not in info.developers:
                info.developers.append(name)
                info.developer_countries.add(country)
            if publisher and name not in info.publishers:
                info.publishers.append(name)

        for game, platform, region, year, month, format in await rows(
            "SELECT game, platform, release_region, y, m, date_format FROM IgdbReleaseDate WHERE y IS NOT NULL"
        ):
            if info := igdb.get(game):
                info.releases.append(ReleaseDate(platform, region, year, month, format))

        for game, platform, offlinemax, offlinecoopmax, offlinecoop in await rows(
            "SELECT game, platform, offlinemax, offlinecoopmax, offlinecoop FROM IgdbMultiplayerMode"
        ):
            if info := igdb.get(game):
                max_players = max((n for n in (offlinemax, offlinecoopmax) if n), default=None)
                info.multiplayer.append(MultiplayerMode(platform, max_players, bool(offlinecoop)))

        for game, organization, rating in await rows(
            "SELECT r.IgdbGame_age_ratings_id, o.name, c.rating FROM IgdbGame_age_ratings r "
            "JOIN IgdbAgeRating a ON a.id = r.IgdbAgeRating_id "
            "JOIN IgdbAgeRatingOrganization o ON o.id = a.organization "
            "JOIN IgdbAgeRatingCategory c ON c.id = a.rating_category"
        ):
            if info := igdb.get(game):
                info.age_ratings[organization].add(rating)

        igdb_playlists: dict[str, set[int]] = defaultdict(set)
        for title, game in await rows("SELECT title, game FROM IgdbPlaylistMapping"):
            igdb_playlists[title].add(game)

        keyword_names = {id: name for id, name in await rows("SELECT id, name FROM IgdbKeyword")}
        tags: dict[int, str] = {}
        for id, name in keyword_names.items():
            # Some keywords are synonyms of others, which would make for duplicate tags
            name = keyword_names.get(KEYWORD_OVERRIDES.get(id, id), name)
            if not TAG_SEPARATORS.search(name):
                tags[id] = name

        platform_types = {id: type for id, type in await rows("SELECT id, platform_type FROM IgdbPlatform")}

        # Most ROMs share their countries and languages with many others,
        # so each distinct JSON value is only parsed once
        @cache
        def rom_countries(value: str | None) -> tuple[str, ...]:
            countries = json.loads(value) if value else None
            if not isinstance(countries, dict):
                return ()
            return tuple(dict.fromkeys(r for code, name in countries.items() if (r := country_region(code, name))))

        @cache
        def rom_languages(value: str | None) -> tuple[str, ...]:
            languages = json.loads(value) if value else None
            if not isinstance(languages, dict):
                return ()
            return tuple(dict.fromkeys(l for name in languages.values() if (l := language_name(name))))

        by_crc: dict[str, list[HasheousLink]] = defaultdict(list)
        by_serial: dict[str, list[HasheousLink]] = defaultdict(list)

        for crc, serial, game, igdb_id, ra, dump, countries, languages in await rows(
            "SELECT a.crc, a.serial, h.id, h.igdb_id, h.retroachievements_id, m.dump, i.country, i.language FROM AllRoms a "
            "JOIN HasheousRomItem i ON i.id = a.hasheous_rom "
            "JOIN HasheousGameDataObject_roms r ON r.HasheousRomItem_id = a.hasheous_rom "
            "JOIN HasheousGameDataObject h ON h.id = r.HasheousGameDataObject_roms_id "
            "JOIN HasheousGameDumpMapping m ON m.game = h.id "
            "WHERE a.hasheous_rom IS NOT NULL"
        ):
            link = HasheousLink(game, igdb_id, ra, dump, rom_countries(countries), rom_languages(languages))
            if crc:
                by_crc[crc.lower()].append(link)
            if serial:
                by_serial[serial.upper()].append(link)

        hasheous_countries: dict[int, set[str]] = defaultdict(set)
        for game, value in await rows(
            "SELECT HasheousGameDataObject_country_id, HasheousGameDataObject_country FROM HasheousGameDataObject_country"
        ):
            if region := hasheous_game_region(value):
                hasheous_countries[game].add(region)

        hasheous_languages: dict[int, set[str]] = defaultdict(set)
        for game, value in await rows(
            "SELECT HasheousGameDataObject_language_id, HasheousGameDataObject_language FROM HasheousGameDataObject_language"
        ):
            if language := language_name(value):
                hasheous_languages[game].add(language)

        return cls(
            igdb=igdb,
            igdb_playlists={k: frozenset(v) for k, v in igdb_playlists.items()},
            links_by_crc=dict(by_crc),
            links_by_serial=dict(by_serial),
            tags=tags,
            platform_types=platform_types,
            hasheous_countries={k: frozenset(v) for k, v in hasheous_countries.items()},
            hasheous_languages={k: frozenset(v) for k, v in hasheous_languages.items()},
        )


class Derivation(NamedTuple):
    fields: dict[str, str]
    """
    Every field our sources could fill in for an entry, whether or not the entry already has it,
    in the order of `RDB_FIELDS`.
    """

    igdb: int | None
    """The IGDB game the entry was matched to, if any."""

    hasheous_games: frozenset[int]
    """The Hasheous games whose ROMs the entry was matched to."""

    ambiguous: bool
    """Whether the entry's ROMs pointed to several IGDB games equally, so none was chosen."""


def name_regions(entry: CompiledEntry, known: Collection[str]) -> tuple[str, ...]:
    """
    Returns the regions in `known` that the first parenthesized tag of an entry's name to list any lists,
    like No-Intro's "(USA, Europe)", MAME's "(Japan, set 2)", or the second tag of "Tiny Troops (CD) (Europe)".
    """
    name = entry.game.get("name")
    if not isinstance(name, str):
        return ()

    for tag in re.findall(r"\(([^()]*)\)", name):
        if regions := tuple(r for part in tag.split(",") if (r := part.strip()) in known):
            return regions

    return ()


def entry_regions(entry: CompiledEntry) -> tuple[str, ...]:
    """
    Returns the regions an entry was released in,
    from its `region` field or else from its name,
    as far as they can be mapped to IGDB's release regions.
    """
    region = entry.game.get("region")
    if isinstance(region, str) and region:
        return tuple(r.strip() for r in re.split(r"[,/|]", region) if r.strip())

    return name_regions(entry, IGDB_REGIONS_BY_DAT_REGION)


def agreed_languages(claims: Iterable[Sequence[str]]) -> Sequence[str] | None:
    """
    Returns the languages that a source gives for an entry, or None if its records disagree.

    :param claims: The languages that each of the source's records gives for the entry.
      A source that only says the entry is in one of several languages
      (e.g. a game whose regional releases are in different languages)
      should give each language as a claim of its own.
    """
    claims = [c for c in claims if c]
    return claims[0] if claims and len({frozenset(c) for c in claims}) == 1 else None


def agreed_region(claims: Iterable[Sequence[str]], named: Sequence[str]) -> str | None:
    """
    Returns the region that a source says an entry is primarily from, or None if its records disagree.

    :param claims: The regions that each of the source's records gives for the entry,
      most prominent first (e.g. `("USA", "Europe")` for No-Intro's "(USA, Europe)").
      A source that only says the entry is from one of several regions
      (e.g. a game released in several) should give each region as a claim of its own.
    :param named: The regions that the entry's own name lists, if any.
      If there are some, the first of them that any claim includes is returned instead,
      which is how the existing DAT files fill in their `region` fields.
    """
    claims = [c for c in claims if c]
    if not claims:
        return None

    if named:
        return next((r for r in named if any(r in c for c in claims)), None)

    firsts = {c[0] for c in claims}
    return firsts.pop() if len(firsts) == 1 else None


class Deriver:
    """Derives `.rdb` fields for the entries of one playlist."""

    def __init__(self, catalog: Catalog, playlist: Playlist) -> None:
        self.catalog = catalog
        self.playlist = playlist
        self.dumps = frozenset(playlist.hasheous_dirs)
        self.igdb_games = catalog.igdb_playlists.get(playlist.title, frozenset())
        self.platforms = playlist_igdb_platforms(playlist)
        self.by_serial = playlist.id_type == "serial"

    def _links(self, rom: Mapping[str, Any]) -> list[HasheousLink]:
        """
        Finds the Hasheous games listing a ROM by the identifiers RetroArch itself uses:
        its CRC32, or failing that (on platforms identified by serial) its serial.
        """
        lookups: list[tuple[str, dict[str, list[HasheousLink]], Any]] = [
            ("crc", self.catalog.links_by_crc, str.lower),
        ]
        if self.by_serial:
            # A serial identifies a game, not a dump, so it's the last resort
            lookups.append(("serial", self.catalog.links_by_serial, str.upper))

        for id_type, index, normalize in lookups:
            value = rom.get(id_type)
            if isinstance(value, str) and (links := index.get(normalize(value))):
                return [link for link in links if link.dump in self.dumps]

        return []

    def derive(self, entry: CompiledEntry) -> Derivation:
        roms: list[Any] = list(entry.roms or (entry.game.get("rom"),))
        if self.by_serial and isinstance(serial := entry.game.get("serial"), str) and not any(
            isinstance(rom, Mapping) and rom.get("serial") for rom in roms
        ):
            # Some DATs only give a serial for the whole game
            roms.append({"serial": serial})

        links_per_rom = [self._links(rom) for rom in roms if isinstance(rom, Mapping)]
        links = [link for rom_links in links_per_rom for link in rom_links]

        # Each ROM gets one vote per IGDB game, however many Hasheous entries repeat it
        votes = Counter(
            igdb
            for rom_links in links_per_rom
            for igdb in {l.igdb for l in rom_links if l.igdb in self.igdb_games}
        )

        fields: dict[str, str] = {}
        info: IgdbInfo | None = None
        chosen: int | None = None
        ambiguous = False

        if votes:
            top = max(votes.values())
            tied = [igdb for igdb, n in votes.items() if n == top]
            if len(tied) == 1:
                chosen = tied[0]
            else:
                # Hasheous sometimes lists one ROM under several games
                # that are mapped to different IGDB entries (e.g. SimCity and SimCity 2000);
                # the identifiers alone can't say which is right, so neither is used
                ambiguous = True

        if chosen is not None:
            info = self.catalog.igdb[chosen]
            fields.update(self._igdb_fields(info, entry))

        if any(link.retroachievements is not None for link in links):
            fields["achievements"] = "1"

        named = tuple(REGION_ALIASES.get(r, r) for r in name_regions(entry, DAT_REGIONS))
        if region := self._region(links, info, named):
            fields["region"] = region

        if isinstance(existing := entry.game.get("region"), str) and existing:
            regions: tuple[str, ...] = (REGION_ALIASES.get(existing, existing),)
        else:
            regions = named or ((region,) if region else ())

        if language := self._language(entry, links, info, regions):
            fields["language"] = language

        return Derivation(
            {f: fields[f] for f in RDB_FIELDS if f in fields},
            chosen,
            frozenset(l.game for l in links),
            ambiguous,
        )

    def _on_platform(self, platform: int | None) -> bool:
        return self.platforms is None or platform is None or platform in self.platforms

    def _igdb_fields(self, info: IgdbInfo, entry: CompiledEntry) -> dict[str, str]:
        fields: dict[str, str] = {}

        if info.developers:
            fields["developer"] = MULTI_VALUE_SEPARATOR.join(info.developers)

            countries = info.developer_countries
            if len(countries) == 1 and (code := next(iter(countries))) is not None:
                code = f"{int(code):03}"
                if origin := ORIGIN_OVERRIDES.get(code):
                    fields["origin"] = origin
                elif country := pycountry.countries.get(numeric=code):
                    fields["origin"] = getattr(country, "common_name", None) or country.name

        if info.publishers:
            fields["publisher"] = MULTI_VALUE_SEPARATOR.join(info.publishers)

        if info.genres:
            fields["genre"] = MULTI_VALUE_SEPARATOR.join(info.genres)

        if info.franchise:
            fields["franchise"] = info.franchise
        elif len(info.franchises) == 1:
            fields["franchise"] = info.franchises[0]

        if info.perspectives:
            fields["perspective"] = MULTI_VALUE_SEPARATOR.join(info.perspectives)

        fields.update(self._release_fields(info, entry))
        fields.update(self._player_fields(info))

        if info.keywords.intersection(RUMBLE_KEYWORD_IDS):
            fields["rumble"] = "1"

        if info.keywords.intersection(ANALOG_KEYWORD_IDS):
            fields["analog"] = "1"

        fields.update(self._rating_fields(info, entry))

        if self.platforms and info.platforms:
            fields["platform_exclusive"] = "1" if info.platforms <= self.platforms else "0"

        platform_types = {
            PLATFORM_TYPE_OVERRIDES.get(p, self.catalog.platform_types.get(p))
            for p in info.platforms
        }
        if platform_types and None not in platform_types:
            fields["console_exclusive"] = "1" if platform_types <= CONSOLE_PLATFORM_TYPES else "0"

        if tags := sorted({tag for keyword in info.keywords if (tag := self.catalog.tags.get(keyword))}):
            fields["tags"] = MULTI_VALUE_SEPARATOR.join(tags)

        return fields

    def _release_fields(self, info: IgdbInfo, entry: CompiledEntry) -> dict[str, str]:
        releases = [r for r in info.releases if self._on_platform(r.platform)]
        if not releases:
            return {}

        regions = entry_regions(entry)
        wanted = {r for region in regions for r in IGDB_REGIONS_BY_DAT_REGION.get(region, ())}

        matching = [r for r in releases if r.region in wanted] or [r for r in releases if r.region == WORLDWIDE]
        if not matching:
            if regions:
                # The game came out in a region IGDB has no date for;
                # another region's date could be years off.
                return {}
            matching = releases

        earliest = min(matching, key=lambda r: (r.year, r.month or 13))
        fields = {"releaseyear": str(earliest.year)}
        if earliest.month and earliest.format in PRECISE_MONTH_FORMATS:
            fields["releasemonth"] = str(earliest.month)

        return fields

    def _rating_fields(self, info: IgdbInfo, entry: CompiledEntry) -> dict[str, str]:
        fields: dict[str, str] = {}
        entry_regions_igdb = {r for region in entry_regions(entry) for r in IGDB_REGIONS_BY_DAT_REGION.get(region, ())}

        for organization, board in RATING_BOARDS.items():
            ratings = info.age_ratings.get(organization, set()) - IGNORED_AGE_RATINGS
            if len(ratings) != 1:
                continue

            if entry_regions_igdb and not (entry_regions_igdb & (board.regions | {WORLDWIDE})):
                # e.g. an ESRB rating on a Japan-only ROM
                continue

            releases = [
                r for r in info.releases
                if self._on_platform(r.platform) and r.region in (board.regions | {WORLDWIDE})
            ]
            if releases and min(r.year for r in releases) >= board.founded:
                fields[board.field] = next(iter(ratings))

        return fields

    def _player_fields(self, info: IgdbInfo) -> dict[str, str]:
        modes = [m for m in info.multiplayer if self._on_platform(m.platform)]
        fields: dict[str, str] = {}

        if players := max((m.max_players for m in modes if m.max_players), default=None):
            fields["users"] = str(max(players, 1))
        elif info.game_modes == {SINGLE_PLAYER_MODE}:
            fields["users"] = "1"

        if any(m.coop for m in modes) or (not modes and COOP_MODE in info.game_modes and self.platforms is not None):
            fields["coop"] = "1"

        return fields

    def _language(self, entry: CompiledEntry, links: Sequence[HasheousLink], info: IgdbInfo | None, regions: Sequence[str]) -> str | None:
        """
        Returns the languages of an entry's dump.

        Hasheous knows them for some ROMs (from No-Intro's language tags).
        Otherwise, the languages that Hasheous or IGDB list for the whole game
        only say which ones a particular dump is in if there's just one,
        since a game's regional releases are usually in different languages,
        and only if it's a language of the regions the dump is from (see `REGION_LANGUAGES`).

        :param regions: The regions the entry is from, if known.
        """
        if languages := agreed_languages(link.languages for link in links):
            return MULTI_VALUE_SEPARATOR.join(languages)

        name = entry.game.get("name")
        if isinstance(name, str) and TRANSLATION_TAG.search(name):
            return None

        expected = frozenset().union(*(REGION_LANGUAGES[r] for r in regions)) if all(r in REGION_LANGUAGES for r in regions) else frozenset()
        sources: tuple[Iterable[Sequence[str]], ...] = (
            ((language,) for game in {l.game for l in links} for language in self.catalog.hasheous_languages.get(game, ())),
            ((language,) for language in (info.languages if info else ())),
        )

        for claims in sources:
            if (languages := agreed_languages(claims)) and (not expected or expected.issuperset(languages)):
                return MULTI_VALUE_SEPARATOR.join(languages)

        return None

    def _region(self, links: Sequence[HasheousLink], info: IgdbInfo | None, named: Sequence[str]) -> str | None:
        """
        Returns the one region that an entry's dump was released in.

        Hasheous knows it for most ROMs (from No-Intro's region tags).
        Otherwise, the regions that Hasheous or IGDB list for the whole game
        only say which one a particular dump is from if there's just one.

        :param named: The regions that the entry's own name lists, if any.
          The first one that a source agrees with is used,
          which is how the existing DAT files fill in their `region` fields.
        """
        igdb_regions = {
            IGDB_RELEASE_REGIONS[r.region]
            for r in (info.releases if info else ())
            if self._on_platform(r.platform) and r.region in IGDB_RELEASE_REGIONS
        }

        sources: tuple[Iterable[Sequence[str]], ...] = (
            (link.countries for link in links),
            ((region,) for game in {l.game for l in links} for region in self.catalog.hasheous_countries.get(game, ())),
            ((region,) for region in igdb_regions),
        )

        for claims in sources:
            if region := agreed_region(claims, named):
                return region

        return None


def missing_fields(entry: CompiledEntry, fields: Mapping[str, str]) -> dict[str, str]:
    """
    Returns the fields that an entry doesn't already have.

    The existing DAT files are curated,
    so anything they already say about a game takes precedence over what we derive.
    """
    missing = {k: v for k, v in fields.items() if k not in entry.game}

    existing_year = entry.game.get("releaseyear")
    if "releasemonth" in missing and existing_year is not None and existing_year != fields.get("releaseyear"):
        # A month from some other year's release would make for a wrong date
        del missing["releasemonth"]

    return missing


async def load_playlists(config_path: Path, titles: Collection[str]) -> tuple[Playlist, ...]:
    async with aiofiles.open(config_path, "r") as config_file:
        config = PlaylistConfig.model_validate(tomllib.loads(await config_file.read()))

    if titles:
        return tuple(p for p in config.playlists if p.title in titles)

    return config.playlists


class GenerateSubCommand(BaseModel, PlaylistArgs, PoolArgs, VerboseArgs):
    """
    Generate one DAT file per playlist that fills in the gaps of RetroArch's databases
    with data from IGDB and Hasheous.

    Each generated DAT only lists entries that the playlist's `.rdb` already has
    (as compiled from this repo's DAT files by libretro-super's `libretro-build-database.sh`),
    keyed exactly as they are there,
    and only the fields that none of the existing DAT files provide.
    Compiling it last therefore adds fields to existing entries
    without adding, removing, reordering, or changing anything else.
    """

    input: FilePath = Field(
        default=PARENT_DIR / 'tmp' / 'index.db',
        description="Path to the input SQLite database file, as generated by the `index` subcommand.",
        validation_alias=AliasChoices('i', 'input'),
        validate_default=True,
    )

    outdir: Path = Field(
        default=PARENT_DIR / OUTDIR_NAME,
        description="Path to the output directory where generated DAT files will be written.",
        validation_alias=AliasChoices('o', 'outdir'),
        validate_default=True,
    )

    _log = logging.getLogger('match.generate')

    async def cli_cmd(self) -> None:
        start = time.perf_counter()

        self._log.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        self._log.addHandler(log_handler)

        playlists = await load_playlists(self.config, self.playlists)
        self.outdir.mkdir(parents=True, exist_ok=True)

        db = create_async_engine(f"sqlite+aiosqlite:///file:{self.input.as_posix()}?mode=ro&uri=true")

        self._log.info("Loading IGDB and Hasheous data from %s", self.input)
        async with db.connect() as connection:
            catalog = await Catalog.load(connection)
        await db.dispose()

        # Parsing the existing DAT files is the slow part, so it's spread across processes
        async with self.create_pool() as pool:
            async def generate(playlist: Playlist) -> GenerateStats | None:
                path = self.outdir / f"{playlist.title}.dat"
                sources = rdb_source_dats(PARENT_DIR, playlist.title)
                if not sources:
                    self._log.warning("No DAT files compile into %s.rdb; skipping", playlist.title)
                    path.unlink(missing_ok=True)
                    return None

                try:
                    entries = await pool.apply(compile_dats_async, (sources, match_key(playlist)))
                except (pe.ParseError, ProxyException) as e:
                    # (ProxyException is how the pool reports a ParseError raised in a worker)
                    # c_converter aborts on the same error, so this .rdb can't be built at all
                    self._log.error("Can't parse the DAT files for %s.rdb: %s", playlist.title, e)
                    path.unlink(missing_ok=True)
                    return None

                return self._generate_dat(catalog, playlist, entries, path)

            tasks = [asyncio.create_task(generate(p), name=p.title) for p in playlists]
            results = await asyncio.gather(*tasks)

        stats = [s for s in results if s is not None]
        self._log.info("Summary:\n%s", GenerateStats.table(stats))

        end = time.perf_counter()
        elapsed = timedelta(seconds=end - start)
        self._log.info(f"Elapsed time: %s", elapsed)

    def _generate_dat(self, catalog: Catalog, playlist: Playlist, entries: Mapping[str, CompiledEntry], path: Path) -> GenerateStats:
        deriver = Deriver(catalog, playlist)
        stats = GenerateStats(playlist.title, entries=len(entries))
        games: list[DatTopLevelRecord] = []

        for key, entry in entries.items():
            derivation = deriver.derive(entry)
            stats.hasheous += bool(derivation.hasheous_games)
            stats.igdb += derivation.igdb is not None
            stats.ambiguous += derivation.ambiguous

            new_fields = missing_fields(entry, derivation.fields)
            if not new_fields:
                continue

            if get_dat_match(entry.game, match_key(playlist)) != key:
                # c_converter keyed this entry by some other field (see compile_dats),
                # so the `rom` record that would identify it here
                # would overwrite the entry's own `rom` record with a different value
                stats.rekeyed += 1
                continue

            stats.filled.update(new_fields.keys())
            game: list[DatPair] = []
            if isinstance(name := entry.game.get("name"), str):
                # The same name that the entry ends up with anyway, for readability
                game.append(("name", name))
            game.extend(new_fields.items())
            game.append(("rom", ((playlist.id_type, key),)))
            games.append(("game", tuple(game)))

        stats.written = len(games)
        if games:
            header: DatTopLevelRecord = ("clrmamepro", (
                ("name", playlist.title),
                ("description", f"{playlist.title} (IGDB and Hasheous metadata)"),
                ("comment", (
                    "Generated by scripts/match.py from IGDB, Hasheous, and this repo's DAT files. "
                    "Only lists fields that no other DAT file provides; compile it after all of them."
                )),
                ("homepage", "https://github.com/libretro/libretro-database"),
            ))

            # c_converter reads bytes, and so may the DAT files this echoes names and keys from
            with path.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as out:
                encode_dat((header, *games), out)
        else:
            path.unlink(missing_ok=True)

        self._log.info(
            "%s: %d of %d entries matched to IGDB, %d DAT entries written",
            playlist.title, stats.igdb, stats.entries, stats.written,
        )
        return stats


@dataclass
class GenerateStats:
    playlist: str
    entries: int = 0
    hasheous: int = 0
    igdb: int = 0
    ambiguous: int = 0
    rekeyed: int = 0
    written: int = 0
    filled: Counter[str] = field(default_factory=Counter)

    @staticmethod
    def table(stats: Iterable[GenerateStats]) -> str:
        stats = sorted(stats, key=lambda s: s.playlist)
        header = ("playlist", "entries", "hasheous", "igdb", "ambiguous", "rekeyed", "written", *RDB_FIELDS)
        rows = [header]
        for s in stats:
            rows.append((s.playlist, s.entries, s.hasheous, s.igdb, s.ambiguous, s.rekeyed, s.written, *(s.filled[f] for f in RDB_FIELDS)))

        totals = ("TOTAL", *(sum(r[i] for r in rows[1:]) for i in range(1, len(header))))
        rows.append(totals)
        return "\n".join("\t".join(str(c) for c in r) for r in rows)


MODEL_TYPES = (
    *IGDB_OBJECT_TYPES,
    *HASHEOUS_OBJECT_TYPES,
    *DAT_OBJECT_TYPES,
    AllRoms,
)

log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter('[%(asctime)s][%(name)s][%(taskName)s] %(message)s'))
sqlalchemy_engine_log = logging.getLogger('sqlalchemy.engine.Engine')
sqlalchemy_engine_log.addHandler(log_handler)


class IndexSubCommand(BaseModel, CommonArgs, PlaylistArgs, IndexArgs, PoolArgs, VerboseArgs):
    """Build a single SQLite index database containing IGDB, DAT, and Hasheous data."""

    output: Path = Field(
        default=PARENT_DIR / 'tmp' / 'index.db',
        description="Path to the output SQLite database file.",
        validation_alias=AliasChoices('o', 'output'),
        validate_default=True,
    )

    filter_hasheous: bool = Field(
        default=True,
        description="""
            Skip Hasheous games and ROMs that no DAT file describes.
            They can't contribute to an `.rdb`, and they outnumber the ones that can
            by more than ten to one.
            Turn this off to index every Hasheous entry.
        """,
    )

    _db_lock = asyncio.Lock()
    _log = logging.getLogger('match.index')

    async def cli_cmd(self) -> None:
        start = time.perf_counter()

        self._log.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        self._log.addHandler(log_handler)
        logging.getLogger('hasheous.index').setLevel(logging.DEBUG if self.verbose else logging.INFO)
        logging.getLogger('hasheous.index').addHandler(log_handler)
        logging.getLogger('igdb.index').setLevel(logging.DEBUG if self.verbose else logging.INFO)
        logging.getLogger('igdb.index').addHandler(log_handler)
        logging.getLogger('dats.index').setLevel(logging.DEBUG if self.verbose else logging.INFO)
        logging.getLogger('dats.index').addHandler(log_handler)

        if self.verbose:
            sqlalchemy_engine_log.setLevel(logging.INFO)

        self.output.parent.mkdir(parents=True, exist_ok=True)

        if self.output.exists() and not self.force:
            raise FileExistsError(f"Output database file '{self.output}' already exists. Use --force to overwrite.")

        # Remove existing database file if it exists
        self.output.unlink(missing_ok=True)

        async with aiofiles.open(self.config, "r") as config_file:
            config = PlaylistConfig.model_validate(tomllib.loads(await config_file.read()))

        if self.playlists:
            playlists = tuple(p for p in config.playlists if p.title in self.playlists)
        else:
            playlists = config.playlists

        # Create a single database with tables for all three data sources
        db, metadata = await create_db(self.output, MODEL_TYPES)
        sqlalchemy_engine_log.setLevel(logging.WARNING)

        # Run all three indexing tasks concurrently against the same database

        async with self.create_pool() as pool:
            async with asyncio.TaskGroup() as group:
                # IGDB has no ROM data, so it can run alongside anything.
                group.create_task(
                    index_igdb(db=db, metadata=metadata, db_lock=self._db_lock, playlists=playlists, igdb_path=self.igdb_path, pool=pool, concurrency=self.concurrency or DEFAULT_IGDB_CONCURRENCY),
                    name="IGDB"
                )

                # The DAT files come first, because they decide
                # which of Hasheous's much larger dumps are worth indexing at all.
                await index_dats(db=db, metadata=metadata, db_lock=self._db_lock, playlists=playlists, dat_dirs=self.dat_dirs, pool=pool, concurrency=self.concurrency or DEFAULT_DAT_CONCURRENCY)

                dat_keys_path = None
                if self.filter_hasheous:
                    dat_keys_path = await write_dat_key_index(db, metadata, self.output.with_suffix(".datkeys.db"))

                group.create_task(
                    index_hasheous(db=db, metadata=metadata, db_lock=self._db_lock, playlists=playlists, hasheous_path=self.hasheous_path, pool=pool, concurrency=self.concurrency or DEFAULT_HASHEOUS_CONCURRENCY, dat_keys_path=dat_keys_path),
                    name="Hasheous"
                )


        # Now that every row is in, build the indexes that were held back
        # while the tables were being filled.
        # Combining the ROM data needs them, so this has to happen first.
        self._log.info("Building deferred indexes")
        await create_deferred_indexes(db, metadata)

        await self._insert_allrom_mappings(db=db, db_lock=self._db_lock, metadata=metadata)

        async with db.connect() as connection:
            # Run the SQLite optimizer to improve performance on all tables (0x10000),
            # but don't take too long (0x00010)
            await connection.execute(text("PRAGMA optimize = 0x10012"))

        # Close the engine
        await db.dispose()

        end = time.perf_counter()
        elapsed = timedelta(seconds=end - start)
        self._log.info(f"Elapsed time: %s", elapsed)

    # The columns that a DAT ROM and a Hasheous ROM can be matched on,
    # from most to least trustworthy.
    # A weaker identifier is only consulted for ROMs that a stronger one didn't match.
    _HASH_MATCH_COLUMNS = ("sha1", "md5", "crc")

    _MAX_GAMES_PER_SERIAL = 8
    """
    How many DAT games a serial may be shared by before it stops counting as an identifier.

    A game's regional releases and revisions legitimately share one serial,
    which is why this isn't simply one:
    99.88% of the serials in the DAT files are shared by eight games or fewer.
    Beyond that are the placeholders that stand in for a serial nobody recorded --
    `00000000-00` alone covers 330 games, and `NTRJ`, `MK-0000-00` and `XXXXXXXX-XX`
    cover dozens each. Matching on one of those would attach
    some unrelated game's metadata to every ROM that carries it.
    """

    async def _insert_allrom_mappings(self, db: AsyncEngine, db_lock: asyncio.Lock, metadata: MetaData) -> None:
        self._log.debug("Aggregating ROM data into one table")
        allroms = metadata.tables[AllRoms.__tablename__]
        datrom = metadata.tables[DatRom.__tablename__]
        hasheousrom = metadata.tables["HasheousRomItem"]

        # A Hasheous ROM that some AllRoms row has already claimed.
        # Each Hasheous ROM may only be attached to one AllRoms row,
        # so later (weaker) matching passes have to skip the ones already taken.
        already_claimed = (
            select(allroms.c.hasheous_rom)
            .where(allroms.c.hasheous_rom == hasheousrom.c.id)
            # Correlate the Hasheous side only, so that AllRoms stays
            # in the subquery's own FROM clause instead of being correlated away.
            .correlate(hasheousrom)
            .exists()
        )

        async with db_transaction(db, db_lock) as tx:
            # Seed the table with every ROM that the DAT files know about.
            # DatRom's hashes are unique, so these rows can't collide with each other.
            self._log.debug("Seeding %s from %s", AllRoms.__tablename__, DatRom.__tablename__)
            await tx.execute(
                insert(allroms).from_select(
                    ["dat_rom", "crc", "serial", "md5", "sha1"],
                    select(
                        datrom.c.rowid.label("dat_rom"),
                        datrom.c.crc,
                        datrom.c.serial,
                        datrom.c.md5,
                        datrom.c.sha1,
                    )
                    # SQLite can't tell an upsert's ON from a join's ON
                    # unless the SELECT it reads from has a WHERE clause.
                    .where(true())
                ).on_conflict_do_nothing()
            )

            # Attach each Hasheous ROM to the DAT ROM it matches, strongest hash first.
            # SQLite can't take several conflict targets in one upsert,
            # so the merge is done as a series of UPDATE ... FROM statements
            # instead of one INSERT ... ON CONFLICT DO UPDATE.
            for match_column in self._HASH_MATCH_COLUMNS:
                result = await tx.execute(
                    allroms.update()
                        .where(
                            allroms.c.hasheous_rom.is_(None),
                            allroms.c[match_column].is_not(None),
                            allroms.c[match_column] == hasheousrom.c[match_column],
                            ~already_claimed,
                        )
                        .values(
                            hasheous_rom=hasheousrom.c.id,
                            crc=coalesce(allroms.c.crc, hasheousrom.c.crc),
                            serial=coalesce(allroms.c.serial, hasheousrom.c.serial),
                            md5=coalesce(allroms.c.md5, hasheousrom.c.md5),
                            sha1=coalesce(allroms.c.sha1, hasheousrom.c.sha1),
                            sha256=coalesce(allroms.c.sha256, hasheousrom.c.sha256),
                        )
                )
                self._log.info("Matched %d ROMs on %s", result.rowcount, match_column)

            # Serials identify a game, not a particular dump of it:
            # the same disc can be dumped many ways, and a fan translation
            # or a mod usually keeps the serial of whatever it was built from.
            # So a serial match may legitimately be many DAT ROMs to one Hasheous ROM,
            # and it must not copy that ROM's hashes across,
            # since those describe a different dump than the one the DAT file lists.
            game_roms = metadata.tables[f"{DatGame.__tablename__}_roms"]
            overused_serials = (
                select(game_roms.c.serial)
                .where(game_roms.c.serial.is_not(None))
                .group_by(game_roms.c.serial)
                .having(count(game_roms.c.game.distinct()) > self._MAX_GAMES_PER_SERIAL)
                .scalar_subquery()
            )

            result = await tx.execute(
                allroms.update()
                    .where(
                        allroms.c.hasheous_rom.is_(None),
                        allroms.c.serial.is_not(None),
                        allroms.c.serial == hasheousrom.c.serial,
                        allroms.c.serial.not_in(overused_serials),
                    )
                    .values(hasheous_rom=hasheousrom.c.id)
            )
            self._log.info("Matched %d ROMs on serial", result.rowcount)

            # Whatever Hasheous knows about but the DAT files don't gets its own row.
            result = await tx.execute(
                insert(allroms).from_select(
                    ["hasheous_rom", "crc", "serial", "md5", "sha1", "sha256"],
                    select(
                        hasheousrom.c.id.label("hasheous_rom"),
                        hasheousrom.c.crc,
                        hasheousrom.c.serial,
                        hasheousrom.c.md5,
                        hasheousrom.c.sha1,
                        hasheousrom.c.sha256,
                    ).where(~already_claimed)
                ).on_conflict_do_nothing()
            )
            self._log.info("Inserted %d unmatched Hasheous ROMs", result.rowcount)

            await tx.commit()


class MatchCommand(BaseSettings):
    generate: CliSubCommand[GenerateSubCommand]
    index: CliSubCommand[IndexSubCommand]

    model_config = SettingsConfigDict(
        case_sensitive=False,
        cli_avoid_json=True,
        cli_implicit_flags=True,
        cli_kebab_case=True,
        cli_parse_args=True,
        extra="ignore",
    )

    def cli_cmd(self):
        CliApp.run_subcommand(self)

if __name__ == "__main__":
    CliApp.run(MatchCommand)
