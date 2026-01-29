#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import csv
import itertools
import logging
import time
import tomllib
import sys

from asyncio import TaskGroup
from collections.abc import AsyncIterable, Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from io import StringIO
from itertools import chain, product
from pathlib import Path
from pprint import pprint
from typing import NamedTuple, Optional
from warnings import deprecated

import aiofiles
import aioitertools.builtins as aiobuiltins
import aiofiles.ospath as aiopath

from aioitertools.asyncio import as_completed
from aiomultiprocess import Pool
from more_itertools import map_reduce, prepend
from pydantic import AliasChoices, BaseModel, DirectoryPath, Field, FilePath
from pydantic_settings import BaseSettings, CliSubCommand, SettingsConfigDict, CliApp
from sqlalchemy import MetaData, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from dats import DAT_OBJECT_TYPES, Game as DatGame, ParsedDatFile, ClrMamePro
from igdb import Game as IgdbGame, PlaylistConfig, load_game_file
from igdb import *
from hasheous import HASHEOUS_OBJECT_TYPES, DataObject, MatchRecord, load_zip

log = logging.getLogger('match')

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

@deprecated("Use more_itertools.first_true instead")
def find[T](items: Iterable[T] | None, predicate: Callable[[T], bool]) -> T | None:
    """Find the first item in items that matches the predicate, or None if not found."""
    if items is None:
        return None

    for item in items:
        if predicate(item):
            return item

    return None

PARENT_DIR = Path(__file__).parent.parent
class CommonArgs(BaseModel):

    config: FilePath = Field(
        default=PARENT_DIR / 'playlists.toml',
        title="Playlist Config File",
        description="Path to the config file that defines available playlists.",
        validation_alias=AliasChoices('c', 'config'),
        validate_default=True,
    )

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

    dat_dirs: tuple[DirectoryPath, ...] = Field(
        default=(PARENT_DIR / 'dat', PARENT_DIR / 'metadat',),
        description="Paths to the directories containing existing DAT files to scan for games to process.",
        validation_alias=AliasChoices('d', 'dat'),
        validate_default=True,
    )

    playlists: tuple[PlaylistTitle, ...] = Field(
        default=(),
        description="""
            Use these playlists (as defined in the config file)
            to limit which data is used to build the index.
            Pass as -p '<playlist_title>' multiple times or once as -p '<playlist1>,<playlist2>,...'.
            If omitted, all playlists in the config will be used.
            Unrecognized playlist titles will be ignored.
        """,
        validation_alias=AliasChoices('p', 'playlists'),
        examples=[("Coleco - ColecoVision", "Dinothawr")]
    )

    verbose: bool = Field(
        default=False,
        description="Enable verbose output.",
        validation_alias=AliasChoices('v', 'verbose'),
    )


class GenerateSubCommand(CommonArgs):

    async def cli_cmd(self) -> None:
        verbose = bool(args.verbose)
        igdb: Path = args.igdb
        hasheous: Path = args.hasheous
        dat_dirs: Sequence[Path] = args.dat
        selected_playlists: Sequence[PlaylistTitle] = args.playlist
        outdir: Path = args.outdir

        if hasheous.exists() and not hasheous.is_dir():
            raise NotADirectoryError(f"--hasheous argument '{hasheous}' is not a directory")

        print(f"Using directory '{hasheous}' to build Hasheous index")

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

    def match_games(self, playlist: PlaylistData, hasheous_index: HasheousIndex, igdb_index: IgdbIndex) -> Iterable[GameMatch]:

        def generate_game(dat: DatGame, igdb: IgdbGame, hasheous: DataObject) -> DatGame:
            """Generate a new DatGame object by combining data from the given DatGame, IgdbGame, and Hasheous DataObject."""

            # TODO: How to handle games with multiple ROMs (e.g. bin/cue games)?

            def get_achievements():
                if hasheous.id in hasheous_index.supports_achievements:
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

            igdb_id = hasheous_index.hasheous_to_igdb.get(hasheous_entry.id, None) if hasheous_entry else None
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
                hasheous_id=hasheous_entry.id if hasheous_entry else None,
                hasheous_url=None # TODO: Populate this field
            )
            yield GameMatch(
                source_dat=dat,
                igdb=igdb_entry,
                hasheous=hasheous_entry,
                generated_dat=game,
                record=match_record,
            )

MODEL_TYPES = (
    *IGDB_OBJECT_TYPES,
    *HASHEOUS_OBJECT_TYPES,
    *DAT_OBJECT_TYPES,
)

class IndexSubCommand(CommonArgs):
    """
    Generate an SQLite database indexing data from IGDB and Hasheous.
    """

    output: Path = Field(
        default=PARENT_DIR / 'tmp' / 'index.db',
        description="Path to the output SQLite database file.",
        validation_alias=AliasChoices('o', 'output'),
        validate_default=True,
    )

    force: bool = Field(
        default=False,
        description="Overwrite existing output database file if it exists.",
        validation_alias=AliasChoices('f', 'force'),
    )

    _db_lock = asyncio.Lock()

    async def cli_cmd(self):
        start = time.perf_counter()
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

        # Create async engine with SQLite
        db = create_async_engine(
            f"sqlite+aiosqlite:///{self.output}",
            echo=self.verbose,  # Log SQL statements if verbose
            connect_args={
                "check_same_thread": False,
                "autocommit": True,
            },
        )

        metadata = MetaData()
        for model_type in MODEL_TYPES:
            model_type.create_tables(metadata)

        async with db.connect() as connection:
            # Use in-memory journaling for better performance at the expense of durability,
            # but that's okay since the database is just used as a local cache
            # (as opposed to persistent storage of critical data).
            await connection.execute(text("PRAGMA synchronous = OFF"))
            await connection.execute(text("PRAGMA journal_mode = MEMORY"))

            # Explicitly disable foreign key constraints for two reasons:
            # 1. Some data sources may refer to newer games
            #    that we're not interested in tracking in RetroArch,
            #    like current-gen remakes of SNES games.
            #    We want to keep the IDs in the database so we can query exclusivity.
            # 2. Enforcing foreign key constraints would require that
            #    related records be inserted in the same transaction.
            #
            # Foreign key constraints are still useful for visualizing or browsing
            # the raw SQLite database, even if they're not enforced at runtime.
            #
            # SQLite doesn't enforce foreign key constraints by default,
            # but the docs say that could change in the future.
            await connection.execute(text("PRAGMA foreign_keys = OFF"))

            await connection.run_sync(metadata.create_all)

        async with Pool() as pool:
            async with TaskGroup() as group:
                igdb_task = group.create_task(
                    self._insert_igdb_games(db, pool, metadata, config, playlists),
                    name="IGDB"
                )

                hasheous_task = group.create_task(
                    self._insert_hasheous_games(db, pool, metadata, config, playlists),
                    name="Hasheous"
                )

                dat_task = group.create_task(
                    self._insert_dat_games(db, pool, metadata, config, playlists),
                    name="DAT"
                )

        # Close the engine
        await db.dispose()

        end = time.perf_counter()
        print(f"Elapsed time: {end - start:.2f} seconds")

    async def _insert_igdb_games(self, db: AsyncEngine, pool: Pool, metadata: MetaData, config: PlaylistConfig, playlists: Collection[Playlist]):
        # Load all playlists concurrently, yielding them as they're loaded.
        if self.verbose:
            print(f"Inserting data from {len(playlists)} IGDB playlists:")
            pprint([p.title for p in playlists], width=120)

        async def job(playlist: Playlist):
            path = self.igdb_path / f"{playlist.title}.json"
            return (playlist, await pool.apply(load_game_file, (path,)))

        playlist_jobs = (job(p) for p in playlists)

        # TODO: Process each playlist in a separate task
        # (unless it doesn't offer the improved concurrency I want)
        async for (playlist, games) in as_completed(playlist_jobs):
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

            async with self._db_lock:
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

                    # TODO: Insert the age rating-related relationships
                    for (field_name, rels) in relationships.items():
                        tablename = f"{IgdbGame.__tablename__}_{field_name}"
                        assert tablename in metadata.tables, f"Relationship field '{field_name}' has no corresponding table in metadata"

                        await tx.execute(
                            insert(metadata.tables[tablename]).prefix_with("OR IGNORE"),
                            rels
                        )


                    await tx.commit()
                    # Commit the session to persist all added objects

            print(f"Inserted {len(games)} IGDB games for playlist '{playlist.title}' into database")

    async def _insert_hasheous_games(self, db: AsyncEngine, pool: Pool, metadata: MetaData, config: PlaylistConfig, playlists: Iterable[Playlist]):
        requested_dumps = set(chain.from_iterable(p.hasheous_dirs for p in playlists))
        requested_dumps.add("Unknown Platform")
        requested_dump_paths = tuple(self.hasheous_path / f"{d}.zip" for d in requested_dumps)
        dump_iterator = as_completed(pool.apply(load_zip, (d,)) for d in requested_dump_paths)

        if self.verbose:
            print(f"Inserting data from {len(requested_dump_paths)} Hasheous dump files:")
            pprint(requested_dump_paths, width=120)

        # TODO: Process each playlist in a separate task
        # (unless it doesn't offer the improved concurrency I want)
        async for games in dump_iterator:
            # Collect all unique objects to insert

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
            async with self._db_lock:
                async with db.begin() as tx:
                    for (model_type, models) in nested_models.items():
                        assert model_type.__tablename__ in metadata.tables, f"Model type '{model_type.__name__}' has no corresponding table in metadata"

                        await tx.execute(
                            insert(metadata.tables[model_type.__tablename__]).prefix_with("OR IGNORE"),
                            # Insert game records, ignoring conflicts because
                            # the same game (or franchise, or genre, or other object)
                            # may appear in multiple dumps

                            [m.as_row for m in models]
                            # DataObject.as_row serializes the model to a dict,
                            # suitable for insertion into the database.
                        )

                    for (field_name, rels) in relationships.items():
                        tablename = f"{DataObject.__tablename__}_{field_name}"
                        assert tablename in metadata.tables, f"Relationship field '{field_name}' has no corresponding table in metadata"

                        await tx.execute(
                            insert(metadata.tables[tablename]).prefix_with("OR IGNORE"),
                            rels
                        )


                    await tx.commit()
                    # Commit the session to persist all added objects

            print(f"Inserted {len(games)} Hasheous games into database")

    async def _insert_dat_games(self, db: AsyncEngine, pool: Pool, metadata: MetaData, config: PlaylistConfig, playlists: Collection[Playlist]):
        if self.verbose:
            print(f"Inserting data from {len(playlists)} DAT playlists:")
            pprint([p.title for p in playlists], width=120)

        async def load_dats(playlist: Playlist) -> AsyncIterable[DatGame]:
            # Use the name of the playlist and the alt names to find all relevant DAT files
            dat_names = prepend(str(playlist.title), playlist.alts)

            # Check for playlists of these names in all requested DAT directories
            dat_paths = (d / f'{n}.dat' for d, n in product(self.dat_dirs, dat_names))

            # HACK: Some XML files have a `.dat` extension, filter them out
            dat_paths = filter(lambda p: 'xml' not in p.name.lower(), dat_paths)
            valid_dat_paths = await aiobuiltins.tuple(p for p in dat_paths if await aiopath.exists(p))

            # Now that we've checked all the paths, load them concurrently
            # (Pydantic models pickle efficiently)
            jobs = (pool.apply(ParsedDatFile.from_dat_file_ignore_errors, (p,)) for p in valid_dat_paths)
            dats = (d async for d in as_completed(jobs) if d is not None)

            async for dat in dats:
                (clrmamepro, *dat_games) = dat.root
                for game in dat_games:
                    yield game

        dat_iterator = ((p, await aiobuiltins.tuple(load_dats(p))) for p in playlists)

        async for (playlist, games) in dat_iterator:
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
            async with self._db_lock:
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
                        tablename = f"{DatGame.__tablename__}_{field_name}"
                        assert tablename in metadata.tables, f"Relationship field '{field_name}' has no corresponding table in metadata"

                        await tx.execute(
                            insert(metadata.tables[tablename]).prefix_with("OR IGNORE"),
                            rels
                        )


                    await tx.commit()
                    # Commit the session to persist all added objects

class MatchCommand(BaseSettings):
    index: CliSubCommand[IndexSubCommand]
    generate: CliSubCommand[GenerateSubCommand]

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


__all__ = ("PlaylistData", "GameMatch", "MatchRecord")

if __name__ == "__main__":
    CliApp.run(MatchCommand)