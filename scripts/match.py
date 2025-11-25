#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import time
import sys

from asyncio import TaskGroup
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from io import StringIO
from pathlib import Path
from pprint import pprint
from typing import Callable, ClassVar, Literal, NamedTuple, Optional, TYPE_CHECKING, Protocol, TypeAlias, TypeVar

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

import aiofiles
import aiosqlite

from pycountry import countries

from dats import Game as DatGame
from dats import *
from hasheous import *
from igdb import *
from igdb import Game as IgdbGame

class PlaylistData(NamedTuple):
    playlist: Playlist
    igdb: Collection[IgdbGame]
    dats: Collection[DatGame]
    hasheous: Collection[DataObject]



class GameMatch(NamedTuple):
    source_dat: DatGame
    igdb: Optional[IgdbGame]
    hasheous: Optional[DataObject]
    generated_dat: Optional[DatGame]
    record: MatchRecord

def find[T](items: Iterable[T] | None, predicate: Callable[[T], bool]) -> T | None:
    """Find the first item in items that matches the predicate, or None if not found."""
    if items is None:
        return None

    for item in items:
        if predicate(item):
            return item

    return None

@cache
def get_country_name(code: int) -> str | None:
    country = countries.get(numeric=str(code))
    return country.name if country else None

def match_games(playlist: PlaylistData, hasheous_index: HasheousIndex, igdb_index: IgdbIndex) -> Iterable[GameMatch]:

    def generate_game(dat: DatGame, igdb: IgdbGame, hasheous: DataObject) -> DatGame:
        """Generate a new DatGame object by combining data from the given DatGame, IgdbGame, and Hasheous DataObject."""

        # TODO: How to handle games with multiple ROMs (e.g. bin/cue games)?

        def get_achievements():
            if hasheous.Id in hasheous_index.supports_achievements:
                return True

            return None

        def get_analog():
            analog_keyword = find(igdb.keywords, lambda k: k.id in ANALOG_KEYWORD_IDS)
            if analog_keyword:
                return True
            elif dat.analog is not None:
                return dat.analog
            return None

        def get_cero(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (5, 7, 8, None):
                # 5 = Japan
                # 7 = Asia
                # 8 = Worldwide
                # CERO is a Japanese rating system,
                # so omit it if this release is known to be somewhere else.
                return None

            cero = find(igdb.age_ratings, lambda r: r.organization.name == "CERO")
            return cero.rating_category.rating if cero else None

        def get_coop(release: ReleaseDate | None):
            if igdb.multiplayer_modes:
                for m in igdb.multiplayer_modes:
                    if m.coop and m.platform and release and m.platform.id == release.platform.id:
                        return True

            if igdb.game_modes:
                for m in igdb.game_modes:
                    if m.id == 3: # IGDB ID for "Co-operative"
                        return True

            # Can't definitively say there's no coop mode, so return None
            return None

        def get_developer():
            if not igdb.involved_companies:
                return None

            return '|'.join(c.company.name for c in igdb.involved_companies if c.developer or c.porting)

        def get_esrb(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (2, 8, None):
                # 2 = North America
                # 8 = Worldwide
                # ESRB is a North American rating system,
                # so omit it if this release is known to be outside North America.
                return None

            esrb = find(igdb.age_ratings, lambda r: r.organization.name == "ESRB")
            return esrb.rating_category.rating if esrb else None

        def get_franchise():
            return igdb.franchise.name if igdb.franchise else None
            # TODO: Handle multiple franchises

        def get_genre():
            return '|'.join(g.name.title() for g in igdb.genres) if igdb.genres else None
            # Some string fields in RetroArch are treated as lists delimited by pipes, commas, or slashes.

        def get_language():
            # TODO: Extract languages from the DAT's name (check for Goodtools/No-Intro/Redump/TOSEC conventions)
            if not igdb.language_supports:
                return None

            language_names: set[str] = set()
            language_supports = sorted((l for l in igdb.language_supports), key=lambda l: l.language.name)
            languages = itertools.groupby(language_supports, key=lambda l: l.language.name)
            for (lang, supports) in languages:
                language_names.add(lang)
                language_names.update(f"{s.language.name} ({s.language_support_type.name})" for s in supports)

            if not language_names:
                return None

            return '|'.join(sorted(language_names))

        def get_pegi(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (1, 8, None):
                # 1 = Europe
                # 8 = Worldwide
                # PEGI is a European rating system,
                # so omit it if this release is known to be outside Europe.
                return None

            pegi = find(igdb.age_ratings, lambda r: r.organization.name == "PEGI")
            return pegi.rating_category.rating if pegi else None

        def get_perspective():
            if not igdb.player_perspectives:
                return None

            return '|'.join(p.name.title() for p in igdb.player_perspectives)

        def get_platform_exclusive():
            # TODO: What to do about legacy re-releases?
            if not igdb.platforms:
                return None

            num_platforms = len(igdb.platforms)
            num_remakes = len(igdb.remakes or ()) # TODO: Only count remakes on different platforms
            num_ports = len(igdb.ports or ()) # TODO: Only count ports on different platforms
            num_remasters = len(igdb.remasters or ()) # TODO: Only count remasters on different platforms
            num_collections = len(igdb.collections or ()) # TODO: Only count collections on different platforms
            total_releases = num_platforms + num_remakes + num_ports + num_remasters + num_collections

            return total_releases == 1

        def get_origin():
            if not igdb.involved_companies:
                return None

            country_codes = {c.company.country for c in igdb.involved_companies if c.developer and c.company.country}
            country_names = tuple(filter(None, (get_country_name(c) for c in country_codes)))

            if not country_names:
                return None

            return '|'.join(sorted(country_names))

        def get_publisher():
            if not igdb.involved_companies:
                return None

            return '|'.join(c.company.name for c in igdb.involved_companies if c.publisher)

        def get_region():
            # TODO: Guess the region from the DAT's name if the region isn't given
            # TODO: Guess the region from matching release dates if the region isn't given
            return dat.region

        def get_release(region: str | None):
            # TODO: What to do about cancelled games?
            if not igdb.release_dates:
                return None

            if len(igdb.release_dates) == 1:
                return igdb.release_dates[0]

            if not region:
                return None

            region_lower = region.lower()
            return find(igdb.release_dates, lambda rd: rd.release_region.region.lower() == region_lower)

        def get_rumble():
            rumble_keyword = find(igdb.keywords, lambda k: k.id in RUMBLE_KEYWORD_IDS)
            if rumble_keyword:
                return True
            elif dat.rumble is not None:
                return dat.rumble
            return None

        def get_tags():
            keywords = (k.name.title() for k in igdb.keywords) if igdb.keywords else ()
            themes = (t.name.title() for t in igdb.themes) if igdb.themes else ()
            tags = sorted(itertools.chain(keywords, themes))
            return '|'.join(tags) if tags else None

        def get_serial():
            if dat.serial:
                return dat.serial
            elif serial_rom := find(dat.rom, lambda r: r.serial is not None):
                return serial_rom.serial
            else:
                return None

        def get_users():
            if not igdb.multiplayer_modes:
                return None

            users = 1
            for m in igdb.multiplayer_modes:
                users = max(users, m.offlinecoopmax or 0, m.offlinemax or 0, m.onlinecoopmax or 0, m.onlinemax or 0)

            return users

        region = get_region()
        release = get_release(region)

        return DatGame(
            name=dat.name_key,
            rom=dat.rom,
            achievements=get_achievements(),
            analog=get_analog(),
            cero_rating=get_cero(release),
            #console_exclusive
            coop=get_coop(release),
            developer=get_developer(),
            #enhancement_hw
            esrb_rating=get_esrb(release),
            franchise=get_franchise(),
            genre=get_genre(),
            igdb_id=igdb.id,
            igdb_url=igdb.url,
            igdb_platform_id=release.platform.id if release else None,
            igdb_release_date_id=release.id if release else None,
            language=get_language(),
            origin=get_origin(),
            pegi_rating=get_pegi(release),
            perspective=get_perspective(),
            platform_exclusive=get_platform_exclusive(),
            publisher=get_publisher(),
            region=region,
            releasemonth=release.m if release else None,
            releaseyear=release.y if release else None,
            rumble=get_rumble(),
            serial=get_serial(),
            tags=get_tags(),
            users=get_users(),
        )

    for dat in playlist.dats:
        if not dat.rom or len(dat.rom) == 0:
            print(f"Warning: DAT game '{dat.name_key}' has no ROMs, skipping", file=sys.stderr)
            continue

        rom = dat.rom[0]
        crc = rom.crc
        md5 = rom.md5
        serial = rom.serial
        sha1 = rom.sha1

        hasheous_entry: Optional[DataObject] = None
        if crc:
            hasheous_entry = hasheous_index.by_crc.get(crc.upper(), None)

        if md5 and not hasheous_entry:
            hasheous_entry = hasheous_index.by_md5.get(md5.upper(), None)

        if sha1 and not hasheous_entry:
            hasheous_entry = hasheous_index.by_sha1.get(sha1.upper(), None)

        if serial and not hasheous_entry:
            hasheous_entry = hasheous_index.by_serial.get(serial.upper(), None)

        igdb_id = hasheous_index.hasheous_to_igdb.get(hasheous_entry.Id, None) if hasheous_entry else None
        igdb_entry = igdb_index.by_id.get(igdb_id, None) if igdb_id else None

        game = generate_game(dat, igdb_entry, hasheous_entry) if (igdb_entry and hasheous_entry) else None
        match_record = MatchRecord(
            name=dat.name_key,
            crc=crc.lower() if crc else None,
            md5=md5,
            sha1=sha1,
            serial=serial,
            igdb_id=igdb_entry.id if igdb_entry else None,
            igdb_url=igdb_entry.url if igdb_entry else None,
            igdb_release_id=game.igdb_release_date_id if game else None,
            igdb_platform_id=game.igdb_platform_id if game else None,
            hasheous_id=hasheous_entry.Id if hasheous_entry else None,
            hasheous_url=None # TODO: Populate this field
        )
        yield GameMatch(
            source_dat=dat,
            igdb=igdb_entry,
            hasheous=hasheous_entry,
            generated_dat=game,
            record=match_record,
        )

def get_playlists(playlistdir: Path) -> Iterator[tuple[Path, Playlist]]:
    """Get the Playlist objects represented by the JSON files in the specified directory."""
    for p in playlistdir.rglob('*.json'):
        # Walking through each JSON file inside the playlistdir...
        if (playlist := PLAYLISTS_BY_TITLE.get(p.stem, None)):
            # If it matches a known playlist title, yield it
            yield (p, playlist)

def get_target_dat_paths(outpath: Path, playlist_titles: Iterable[str]) -> Iterator[Path]:
    """Get the paths to the DAT files that will be generated from the given playlists, rooted at the given directory."""
    for title in playlist_titles:
        yield outpath / f"{title}.dat"

# Simple SQLite query builder helpers,
# for the parts that we actually use

PrimaryKeyColumnConstraint: TypeAlias = Literal["PRIMARY KEY"]
NotNullColumnConstraint: TypeAlias = Literal["NOT NULL"]

class ForeignKeyColumnConstraint(NamedTuple):
    foreign_table: str
    foreign_column: str

    def __str__(self) -> str:
        return f"REFERENCES {self.foreign_table}({self.foreign_column})"

ColumnConstraint: TypeAlias = PrimaryKeyColumnConstraint | NotNullColumnConstraint | ForeignKeyColumnConstraint | str

class ColumnDefinition(NamedTuple):
    name: str
    type: Optional[str]
    constraints: tuple[ColumnConstraint, ...]

    def __str__(self) -> str:
        tokens = [self.name]

        if self.type:
            tokens.append(self.type)

        if self.constraints:
            tokens += map(str, self.constraints)

        return ' '.join(tokens)

class PrimaryKeyTableConstraint(NamedTuple):
    columns: tuple[str, ...]

    def __str__(self) -> str:
        cols = ', '.join(self.columns)
        return f"PRIMARY KEY ({cols})"

class ForeignKeyTableConstraint(NamedTuple):
    columns: tuple[str, ...]
    foreign_table: str
    foreign_columns: tuple[str, ...]

    def __str__(self) -> str:
        cols = ', '.join(self.columns)
        foreign_cols = ', '.join(self.foreign_columns)
        return f"FOREIGN KEY ({cols}) REFERENCES {self.foreign_table}({foreign_cols})"

class UniqueTableConstraint(NamedTuple):
    columns: tuple[str, ...]

    def __str__(self) -> str:
        cols = ', '.join(self.columns)
        return f"UNIQUE ({cols})"

TableConstraint: TypeAlias = PrimaryKeyTableConstraint | ForeignKeyTableConstraint | UniqueTableConstraint

class TableDefinition(NamedTuple):
    name: str
    columns: tuple[ColumnDefinition, ...]
    constraints: tuple[TableConstraint, ...]

    def __str__(self) -> str:
        defs = ',\n    '.join(map(str, self.columns + self.constraints))
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {defs}\n)"


if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    D = TypeVar('D', bound=DataclassInstance, covariant=True)
    class DataModelType(DataclassInstance, Protocol[D]):
        __tablename__: ClassVar[str]


async def create_schema(db: aiosqlite.Connection) -> None:
    """Create all tables needed for the database schema."""

    # TODO: Use the TableDefinition and ColumnDefinition classes to build these
    # from the dataclasses defined in dats.py, igdb.py, and hasheous.py

    # DAT-related tables
    await db.execute("""
        CREATE TABLE IF NOT EXISTS clrmamepro (
            name TEXT PRIMARY KEY,
            description TEXT,
            category TEXT,
            date TEXT,
            author TEXT,
            email TEXT,
            url TEXT,
            version TEXT,
            comment TEXT,
            homepage TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS game (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            comment TEXT,
            description TEXT,
            game_id TEXT,
            achievements INTEGER,
            analog INTEGER,
            artstyle TEXT,
            bbfc_rating TEXT,
            category TEXT,
            cero_rating TEXT,
            code TEXT,
            console_exclusive INTEGER,
            controls TEXT,
            coop INTEGER,
            date TEXT,
            developer TEXT,
            download TEXT,
            edge_issue INTEGER,
            edge_rating INTEGER,
            elspa_rating TEXT,
            enhancement_hardware TEXT,
            enhancement_hw TEXT,
            esrb_rating TEXT,
            famitsu_rating INTEGER,
            franchise TEXT,
            gameplay TEXT,
            genre TEXT,
            homepage TEXT,
            igdb_id INTEGER,
            igdb_url TEXT,
            igdb_platform_id INTEGER,
            igdb_release_date_id INTEGER,
            language TEXT,
            license TEXT,
            manufacturer TEXT,
            media TEXT,
            narrative TEXT,
            origin TEXT,
            pacing TEXT,
            patch TEXT,
            pegi_rating TEXT,
            perspective TEXT,
            platform_exclusive INTEGER,
            publisher TEXT,
            region TEXT,
            releaseday INTEGER,
            releasemonth INTEGER,
            releaseyear INTEGER,
            rumble INTEGER,
            score TEXT,
            serial TEXT,
            setting TEXT,
            tags TEXT,
            users INTEGER,
            vehicular TEXT,
            version TEXT,
            visual TEXT,
            year TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS rom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER,
            crc TEXT,
            serial TEXT,
            image TEXT,
            name TEXT,
            size INTEGER,
            md5 TEXT,
            sha1 TEXT,
            sha1sum TEXT,
            genre TEXT,
            users TEXT,
            FOREIGN KEY (game_id) REFERENCES game(id)
        )
    """)

    # IGDB-related tables
    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_game (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT,
            first_release_date INTEGER,
            storyline TEXT,
            summary TEXT,
            url TEXT,
            version_title TEXT,
            aggregated_rating REAL,
            aggregated_rating_count INTEGER,
            total_rating REAL,
            total_rating_count INTEGER
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_platform (
            id INTEGER PRIMARY KEY,
            abbreviation TEXT,
            alternative_name TEXT,
            generation INTEGER,
            name TEXT NOT NULL,
            slug TEXT,
            summary TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_company (
            id INTEGER PRIMARY KEY,
            country INTEGER,
            name TEXT NOT NULL,
            slug TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_genre (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_keyword (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_theme (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_release_date (
            id INTEGER PRIMARY KEY,
            game_id INTEGER,
            platform_id INTEGER,
            date INTEGER,
            human TEXT,
            m INTEGER,
            y INTEGER,
            region TEXT,
            FOREIGN KEY (game_id) REFERENCES igdb_game(id),
            FOREIGN KEY (platform_id) REFERENCES igdb_platform(id)
        )
    """)

    # Hasheous-related tables
    await db.execute("""
        CREATE TABLE IF NOT EXISTS hasheous_data_object (
            id INTEGER PRIMARY KEY,
            object_type TEXT,
            name TEXT,
            created_date TEXT,
            updated_date TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS hasheous_rom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_object_id INTEGER,
            name TEXT,
            size INTEGER,
            crc TEXT,
            md5 TEXT,
            sha1 TEXT,
            sha256 TEXT,
            serial TEXT,
            FOREIGN KEY (data_object_id) REFERENCES hasheous_data_object(id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS hasheous_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_object_id INTEGER,
            immutable_id TEXT,
            status TEXT,
            match_method TEXT,
            source TEXT,
            link TEXT,
            FOREIGN KEY (data_object_id) REFERENCES hasheous_data_object(id)
        )
    """)

    # Relationship tables
    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_game_genre (
            game_id INTEGER,
            genre_id INTEGER,
            PRIMARY KEY (game_id, genre_id),
            FOREIGN KEY (game_id) REFERENCES igdb_game(id),
            FOREIGN KEY (genre_id) REFERENCES igdb_genre(id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_game_keyword (
            game_id INTEGER,
            keyword_id INTEGER,
            PRIMARY KEY (game_id, keyword_id),
            FOREIGN KEY (game_id) REFERENCES igdb_game(id),
            FOREIGN KEY (keyword_id) REFERENCES igdb_keyword(id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_game_theme (
            game_id INTEGER,
            theme_id INTEGER,
            PRIMARY KEY (game_id, theme_id),
            FOREIGN KEY (game_id) REFERENCES igdb_game(id),
            FOREIGN KEY (theme_id) REFERENCES igdb_theme(id)
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS igdb_game_platform (
            game_id INTEGER,
            platform_id INTEGER,
            PRIMARY KEY (game_id, platform_id),
            FOREIGN KEY (game_id) REFERENCES igdb_game(id),
            FOREIGN KEY (platform_id) REFERENCES igdb_platform(id)
        )
    """)

    # Create indexes for common queries
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rom_crc ON rom(crc)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rom_md5 ON rom(md5)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rom_sha1 ON rom(sha1)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rom_serial ON rom(serial)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_hasheous_rom_crc ON hasheous_rom(crc)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_hasheous_rom_md5 ON hasheous_rom(md5)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_hasheous_rom_sha1 ON hasheous_rom(sha1)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_hasheous_metadata_source ON hasheous_metadata(source, immutable_id)")

    await db.commit()

async def handle_generate(args: argparse.Namespace) -> None:
    """Handle the generate subcommand."""

    verbose = bool(args.verbose)
    igdb: Path = args.igdb
    hasheous: Path = args.hasheous
    dat_dirs: Sequence[Path] = args.dat
    selected_playlists: Sequence[PlaylistTitle] = args.playlist
    outdir: Path = args.outdir

    if hasheous.exists() and not hasheous.is_dir():
        raise NotADirectoryError(f"--hasheous argument '{hasheous}' is not a directory")

    print(f"Using directory '{hasheous}' to build Hasheous index")

    playlists = {p: pl for p, pl in get_playlists(igdb) if pl.title in selected_playlists}
    playlist_titles = tuple(p.title for p in playlists.values())

    target_dat_paths = {outdir / f"{t}.dat" for t in playlist_titles}
    existing_dats = itertools.chain.from_iterable(get_existing_dat_files(d) for d in dat_dirs)
    existing_dat_paths = {p for p in map(Path, existing_dats) if p.stem in playlist_titles}

    print(f"Found {len(target_dat_paths)} target DAT files to generate from playlists")
    if verbose:
        pprint(target_dat_paths, width=120)

    print(f"Found {len(existing_dat_paths)} existing DAT files")
    if verbose:
        pprint(existing_dat_paths, width=120)

    dats_to_scan = existing_dat_paths - target_dat_paths

    print(f"In total, will scan {len(dats_to_scan)} existing DAT files for games to process.")
    if verbose:
        pprint(dats_to_scan, width=120)

    hasheous_dirs = {p.title: p.hasheous_dirs for p in playlists.values()}
    # Some of the Playlists consist of multiple Hasheous directories

    playlists_for_dats = ((get_playlist(d), d) for d in dats_to_scan)
    playlists_for_dats = ((p.title, d) for p, d in playlists_for_dats if p is not None)
    sorted_playlists_for_dats = sorted(playlists_for_dats, key=lambda p: p[0])
    grouped_playlists_for_dats = itertools.groupby(sorted_playlists_for_dats, key=lambda p: p[0])
    dats_by_playlist = {k: [pd[1] for pd in g] for k, g in grouped_playlists_for_dats}

    async with TaskGroup() as group:
        with ProcessPoolExecutor() as executor:
            loaded_igdb, loaded_dats, loaded_hasheous = await asyncio.gather(
                group.create_task(load_games(playlists, executor)),
                group.create_task(load_dats(dats_by_playlist, executor)),
                group.create_task(load_dataobjects(hasheous, playlists.values(), executor))
            )

        keys = set(loaded_igdb.by_playlist.keys()) | set(loaded_dats.keys()) | set(loaded_hasheous.by_playlist.keys())

        playlist_dict: Mapping[str, PlaylistData] = {}
        for k in keys:
            playlist = get_playlist(k)
            if not playlist:
                print(f"Warning: Ignoring data for unknown playlist '{k}'", file=sys.stderr)
                continue

            playlist_dict[k] = PlaylistData(
                playlist=playlist,
                igdb=loaded_igdb.by_playlist.get(k, ()),
                dats=loaded_dats.get(k, ()),
                hasheous=loaded_hasheous.by_playlist.get(k, ()),
            )

        async def generate_dat(data: PlaylistData) -> None:
            title = data.playlist.title

            clrmamepro = ClrMamePro(
                name=title,
                description=title,
                comment=f"Games for {title} with metadata from IGDB",
                #version = "today's date"  # TODO: Set version to today's date
                version=None,
                author="Jesse Talavera",
            )

            print(f"Matching games for playlist '{title}' with {len(data.dats)} DAT games, {len(data.igdb)} IGDB games, and {len(data.hasheous)} Hasheous entries")
            matches = tuple(match_games(data, loaded_hasheous, loaded_igdb))
            games = (m.generated_dat for m in matches if m.generated_dat is not None)
            await asyncio.sleep(0)
            dat = (clrmamepro, *games, )
            num_matches = len(matches)
            num_matched_dats = len(dat) - 1 # Subtract 1 for the ClrMamePro header
            match_rate = (num_matched_dats / num_matches * 100.0) if num_matches > 0 else 0.0
            print(f"Matched {num_matched_dats} of {num_matches} ({match_rate:.2f}%) DAT records in playlist '{title}'")
            if not num_matched_dats:
                # If nothing was matched, don't write any files
                print(f"Skipping DAT output for playlist '{title}' because no records were matched")
            else:
                encoded_dat = GameDataListCodec.encode(dat)
                dat_path = outdir.joinpath(f"{title}.dat")

                async with aiofiles.open(dat_path, 'wb') as outfile:
                    await outfile.write(encoded_dat)

                print(f"Wrote DAT at '{dat_path}' with {num_matched_dats} records")

            tsv_path = outdir.joinpath(f"{title}.tsv")
            tsv_output = StringIO(newline=None) # csv.DictWriter writes its own newlines
            writer = csv.DictWriter(tsv_output, fieldnames=MatchRecord._fields, dialect='excel-tab')
            # Using the excel-tab dialect because some fields may contain commas
            writer.writeheader()

            checkpoint: float = time.perf_counter()
            for match in sorted(matches, key=lambda m: m.record.name):
                writer.writerow(match.record._asdict())
                if time.perf_counter() - checkpoint > 1.0:
                    # If this has been running for more than a second, yield to the event loop
                    await asyncio.sleep(0)
                    checkpoint = time.perf_counter()

            async with aiofiles.open(tsv_path, 'w', encoding='utf-8') as outfile:
                await outfile.write(tsv_output.getvalue())

            print(f"Wrote TSV at '{tsv_path}' with {len(matches)} records (including incomplete matches)")

        dat_tasks = tuple(group.create_task(generate_dat(p), name=p.playlist.title) for p in playlist_dict.values())

        await asyncio.gather(*dat_tasks)

async def handle_index(args: argparse.Namespace) -> None:
    """Handle the index subcommand."""
    verbose = bool(args.verbose)
    igdb: Path = args.igdb
    hasheous: Path = args.hasheous
    output: Path = args.output

    if not igdb.exists():
        raise FileNotFoundError(f"IGDB directory not found: {igdb}")

    if not hasheous.exists():
        raise FileNotFoundError(f"Hasheous directory not found: {hasheous}")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing database file if it exists
    output.unlink(missing_ok=True)

    async with aiosqlite.connect(output) as db:
        print("Creating database schema...")
        await create_schema(db)

        # Load IGDB data
        print("Loading IGDB data...")
        playlists = {p: pl for p, pl in get_playlists(igdb)}

        with ProcessPoolExecutor() as executor:
            igdb_index = await load_games(playlists, executor)

        print(f"Loaded {len(igdb_index.by_id)} IGDB games")

        # Insert IGDB data
        print("Inserting IGDB games...")
        for game in igdb_index.by_id.values():
            await db.execute("""
                INSERT OR REPLACE INTO igdb_game
                (id, name, slug, first_release_date, storyline, summary, url, version_title,
                 aggregated_rating, aggregated_rating_count, total_rating, total_rating_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game.id, game.name, game.slug, game.first_release_date,
                game.storyline, game.summary, game.url, game.version_title,
                game.aggregated_rating, game.aggregated_rating_count,
                game.total_rating, game.total_rating_count
            ))

            # Insert related data
            if game.genres:
                for genre in game.genres:
                    await db.execute("INSERT OR IGNORE INTO igdb_genre (id, name) VALUES (?, ?)",
                                   (genre.id, genre.name))
                    await db.execute("INSERT OR IGNORE INTO igdb_game_genre (game_id, genre_id) VALUES (?, ?)",
                                   (game.id, genre.id))

            if game.keywords:
                for keyword in game.keywords:
                    await db.execute("INSERT OR IGNORE INTO igdb_keyword (id, name, slug) VALUES (?, ?, ?)",
                                   (keyword.id, keyword.name, keyword.slug))
                    await db.execute("INSERT OR IGNORE INTO igdb_game_keyword (game_id, keyword_id) VALUES (?, ?)",
                                   (game.id, keyword.id))

            if game.themes:
                for theme in game.themes:
                    await db.execute("INSERT OR IGNORE INTO igdb_theme (id, name) VALUES (?, ?)",
                                   (theme.id, theme.name))
                    await db.execute("INSERT OR IGNORE INTO igdb_game_theme (game_id, theme_id) VALUES (?, ?)",
                                   (game.id, theme.id))

            if game.platforms:
                for platform in game.platforms:
                    await db.execute("""
                        INSERT OR IGNORE INTO igdb_platform
                        (id, abbreviation, alternative_name, generation, name, slug, summary)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        platform.id, platform.abbreviation, platform.alternative_name,
                        platform.generation, platform.name, platform.slug, platform.summary
                    ))
                    await db.execute("INSERT OR IGNORE INTO igdb_game_platform (game_id, platform_id) VALUES (?, ?)",
                                   (game.id, platform.id))

            if game.release_dates:
                for rd in game.release_dates:
                    await db.execute("""
                        INSERT OR REPLACE INTO igdb_release_date
                        (id, game_id, platform_id, date, human, m, y, region)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        rd.id, game.id, rd.platform.id, rd.date, rd.human,
                        rd.m, rd.y, rd.release_region.region
                    ))

        await db.commit()
        print("IGDB data inserted")

        # Load Hasheous data
        print("Loading Hasheous data...")
        with ProcessPoolExecutor() as executor:
            hasheous_index = await load_dataobjects(hasheous, playlists.values(), executor)

        print(f"Loaded {len(hasheous_index.by_id)} Hasheous data objects")

        # Insert Hasheous data
        print("Inserting Hasheous data...")
        for obj in hasheous_index.by_id.values():
            await db.execute("""
                INSERT OR REPLACE INTO hasheous_data_object
                (id, object_type, name, created_date, updated_date)
                VALUES (?, ?, ?, ?, ?)
            """, (obj.Id, obj.ObjectType, obj.Name, obj.CreatedDate, obj.UpdatedDate))

            # Insert metadata
            for meta in obj.Metadata:
                if meta.Status == 'Mapped':
                    await db.execute("""
                        INSERT INTO hasheous_metadata
                        (data_object_id, immutable_id, status, match_method, source, link)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (obj.Id, meta.ImmutableId, meta.Status, meta.MatchMethod, meta.Source, meta.Link))

            # Insert ROM data
            for attr in obj.Attributes:
                if attr.attributeName == 'ROMs' and isinstance(attr.Value, Sequence) and not isinstance(attr.Value, str):
                    for rom in attr.Value:
                        serial = rom.Attributes.get('serial') if rom.Attributes else None
                        await db.execute("""
                            INSERT INTO hasheous_rom
                            (data_object_id, name, size, crc, md5, sha1, sha256, serial)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            obj.Id, rom.Name, rom.Size, rom.Crc, rom.Md5,
                            rom.Sha1, rom.Sha256, serial
                        ))

        await db.commit()
        print("Hasheous data inserted")

        print(f"Database created successfully at {output}")

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Utilities for matching data from the various sources used by this repo."
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show more logging output."
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # `generate` subcommand (the only one for now)
    generate_parser = subparsers.add_parser(
        "generate",
        help="Output DAT files suitable for ClrMamePro and libretro."
    )
    generate_parser.add_argument(
        "--igdb",
        help="Path to a directory containing JSON files downloaded with `igdb fetch`.",
        type=Path,
        default="tmp/igdb",
    )
    generate_parser.add_argument(
        "--hasheous",
        help="Path to a directory containing ZIP archives downloaded with `hasheous fetch`.",
        type=Path,
        default="tmp/hasheous",
    )
    generate_parser.add_argument(
        "--dat",
        help="One or more directories containing existing DAT files to scan for games to process. Files that will be overwritten won't be scanned.",
        type=Path,
        nargs="+",
        default=["dat", "metadat"],
    )
    generate_parser.add_argument(
        "--playlist",
        help="The title of the playlists to process, as named in igdb.toml. If not provided, all playlists defined in that file will be processed.",
        type=PlaylistTitle,
        nargs="+",
        default=PLAYLIST_TITLES,
    )
    generate_parser.add_argument(
        "outdir",
        type=Path,
        help="The output directory for the processed DAT files.",
    )
    generate_parser.set_defaults(func=handle_generate)

    # `index` subcommand
    index_parser = subparsers.add_parser(
        "index",
        help="Create an SQLite database that indexes data from the various data sources used by this script."
    )
    index_parser.add_argument(
        "--igdb",
        help="Path to a directory containing JSON files downloaded with `igdb fetch`.",
        type=Path,
        default="tmp/igdb",
    )
    index_parser.add_argument(
        "--hasheous",
        help="Path to a directory containing ZIP archives downloaded with `hasheous fetch`.",
        type=Path,
        default="tmp/hasheous",
    )
    index_parser.add_argument(
        "output",
        help="Path to the output SQLite database file.",
        type=Path,
    )
    index_parser.set_defaults(func=handle_index)


    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = ("PlaylistData", "match_games", "GameMatch", "MatchRecord")

if __name__ == "__main__":
    main()