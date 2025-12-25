#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import time
import sys

from asyncio import TaskGroup
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from io import StringIO
from itertools import chain
from pathlib import Path
from pprint import pprint
from typing import NamedTuple, Optional

import aiofiles

from aioitertools.asyncio import as_completed
from aiomultiprocess import Pool
from more_itertools import map_reduce
from sqlalchemy import MetaData, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from dats import Game as DatGame, load_dats, get_existing_dat_files, ClrMamePro, GameDataListCodec
from igdb import Game as IgdbGame, load_game_file
from igdb import *
from hasheous import DataObject, MatchRecord, HasheousIndex, load_dataobjects

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
                    if m.coop and m.platform and release and m.platform == release.platform:
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
            total_releases = num_platforms + num_remakes + num_ports + num_remasters

            return total_releases == 1

        def get_origin():
            if not igdb.involved_companies:
                return None

            country_names = {c.company.country.short_name for c in igdb.involved_companies if c.developer and c.company.country}

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
            igdb_url=str(igdb.url) if igdb.url else None,
            igdb_platform_id=release.platform if release else None,
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
            igdb_url=str(igdb_entry.url) if igdb_entry else None,
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

async def insert_igdb_games(db: AsyncEngine, pool: Pool, metadata: MetaData, playlists: Mapping[Path, Playlist]):
    # Load all playlists concurrently, yielding them as they're loaded.
    playlist_iterator = as_completed(pool.apply(load_game_file, p) for p in playlists.items())

    db.echo = False
    # TODO: Process each playlist in a separate task
    # (unless it doesn't offer the improved concurrency I want)
    async for (title, games) in playlist_iterator:
        # Collect all unique objects to insert

        # Aggregate all nested models from all games in the playlist
        nested_models = map_reduce(
            chain(games, chain.from_iterable(g.nested_models for g in games)),
            lambda model: type(model),
            None,
            frozenset
        )
        relationships = map_reduce(
            chain.from_iterable(g.relationships.items() for g in games),
            lambda rels: rels[0], # key is the field name
            lambda rels: rels[1], # value is the set of relationships
            lambda entries: tuple(chain.from_iterable(entries)) # group by field name, aggregate unique relationships
        )
        async with db.begin() as tx:
            for (model_type, models) in nested_models.items():
                assert model_type.__tablename__ in metadata.tables, f"Model type '{model_type.__name__}' has no corresponding table in metadata"

                await tx.execute(
                    insert(metadata.tables[model_type.__tablename__]).prefix_with("OR IGNORE"),
                    # Insert game records, ignoring conflicts because
                    # the same game (or franchise, or genre, or other object)
                    # may appear in multiple playlists

                    [m.model_dump(context="row") for m in models]
                    # BaseModel.model_dump serializes the model to a dict,
                    # and IgdbObject in particular defines custom serialization behavior
                    # that's activated by passing a context value of "row".
                )

            for (field_name, rels) in relationships.items():
                tablename = f"{IgdbGame.__tablename__}_{field_name}"
                assert tablename in metadata.tables, f"Relationship field '{field_name}' has no corresponding table in metadata"

                await tx.execute(
                    insert(metadata.tables[tablename]).prefix_with("OR IGNORE"),
                    rels
                )


            await tx.commit()
            # Commit the session to persist all added objects

        print(f"Inserted {len(games)} IGDB games for playlist '{title}' into database")

MODEL_TYPES = (
    *IGDB_OBJECT_TYPES,
)

async def handle_index(args: argparse.Namespace) -> None:
    """Handle the index subcommand."""
    verbose = bool(args.verbose)
    igdb_path: Path = args.igdb
    hasheous_path: Path = args.hasheous
    output: Path = args.output

    if not igdb_path.exists():
        raise FileNotFoundError(f"IGDB directory not found: {igdb_path}")

    if not hasheous_path.exists():
        raise FileNotFoundError(f"Hasheous directory not found: {hasheous_path}")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing database file if it exists
    output.unlink(missing_ok=True)

    playlists = {p: pl for p, pl in get_playlists(igdb_path)}

    # Create async engine with SQLite
    db = create_async_engine(
        f"sqlite+aiosqlite:///{output}",
        echo=verbose,  # Log SQL statements if verbose
        connect_args={"check_same_thread": False},
    )

    metadata = MetaData()
    for model_type in MODEL_TYPES:
        model_type.create_tables(metadata)

    # Configure pragmas for better performance
    # Note: These need to be set after engine creation
    async with db.connect() as connection:
        # Use in-memory journaling for better performance at the expense of durability,
        # but that's okay since the database is just used as a local cache
        # (as opposed to persistent storage of critical data).

        await connection.execute(text("PRAGMA journal_mode = MEMORY"))
        await connection.execute(text("PRAGMA synchronous = OFF"))

        await connection.run_sync(metadata.create_all)

    # Create async session factory
    async with Pool() as pool:
        async with TaskGroup() as group:
            igdb_task = group.create_task(
                insert_igdb_games(db, pool, metadata, playlists),
                name="IGDB"
            )

            await igdb_task
            # TODO: Start loading Hasheous objects in parallel
            # TODO: Start loading DAT games in parallel
            # TODO: Use a TaskGroup to manage these tasks
            # TODO: Write tasks to insert data into the database as it is loaded

    # Close the engine
    await db.dispose()

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
    # TODO: Add -vv support for more verbose logging

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