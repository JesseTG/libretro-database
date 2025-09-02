#!/usr/bin/env python3

import argparse
import asyncio
import itertools
import json
import os
import os.path
import time
import typing
from collections import ChainMap
from collections.abc import Iterable, Sequence, Iterator, Mapping, Collection, Sized, AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from io import TextIOWrapper
from typing import TypedDict, Required, TypeAlias, TextIO, Union, cast
import sys

import pe
from pe import OPTIMIZE, ParseError
from pe.operators import Class, Star

from igdb_playlists import Playlist, PLAYLISTS


class ClrMamePro(TypedDict, total=False):
    name: Required[str]
    description: str
    category: str
    date: str
    author: str
    email: str
    url: str
    version: str
    comment: str
    homepage: str

class Rom(TypedDict, total=False):
    crc: str
    serial: str
    image: str
    name: str
    size: int
    md5: str
    sha1: str

class Game(TypedDict, total=False):
    rom: Required[Rom | Sequence[Rom]]
    name: str
    comment: str
    description: str
    id: str

    analog: bool
    bbfc_rating: str
    code: str
    date: str
    developer: str
    edge_issue: int
    edge_rating: int
    elspa_rating: str
    enhancement_hardware: str
    esrb_rating: str
    famitsu_rating: int
    franchise: str
    genre: str
    homepage: str
    license: str
    manufacturer: str
    origin: str
    patch: str
    publisher: str
    region: str
    releaseday: int
    releasemonth: int
    releaseyear: int
    rumble: bool
    tags: str
    users: int
    year: int

DatRecord: TypeAlias = Union[Mapping[str, Union[str, Sequence["DatRecord"], "DatRecord"]], dict]

# PEG grammar for DAT file format
DAT_GRAMMAR = r'''
# Main entry points
DatFile < (Record)* EndOfFile

# Record structure
Record < type:(~RecordType) Open RecordContent Close
RecordType <- [a-zA-Z_][-a-zA-Z0-9_]*
RecordContent <- (KeyValue)*
KeyValue < key:(~Key) value:Value

# Keys and Values
Key <- [a-zA-Z_][-a-zA-Z0-9_]*
Value <- (Open RecordContent Close) / QuotedString / UnquotedString

# Characters
QuotedString <- ["] ~(Char*) ["]
UnquotedString <- ~(![" \r\t\n\\] Char)+
Char <- ("\\" ['"\\] / !["] .)

Open <- "("
Close <- ")"

# Whitespace and comments
Space <- [ \t\r\n]
EndOfLine <- '\r\n' / '\n' / '\r'
EndOfFile <- !.
'''

def _flatten(data):
    result = tuple(data)
    return result if len(result) > 1 else result[0]

def _build_record(*args, **kwargs):
    """Build a record dictionary from parsed data."""
    record_type = kwargs.get('type', '')


    if record_type == 'clrmamepro':
        clrmamepro_args = args[0]
        return ClrMamePro(**clrmamepro_args)

    return dict(**(args[0]))

def _build_record_content(*args, **kwargs):
    """Build record content from key-value pairs."""

    # group items with duplicate keys together, and put them in a tuple
    def flatten_kv(key, value):
        return key, _flatten(v[1] for v in value)

    return dict(flatten_kv(k, v) for k, v in itertools.groupby(args, lambda x: x[0]))

def _build_keyvalue(*args, **kwargs):
    """Build a key-value pair."""
    return kwargs['key'], kwargs['value']

def _build_datfile(*args, **kwargs):
    """Build the top-level DAT file structure."""
    return args

# Actions for semantic processing
ACTIONS = {
    'Record': _build_record,
    'RecordContent': _build_record_content,
    'KeyValue': _build_keyvalue,
    'DatFile': _build_datfile,
}

# Compile the parser
dat_parser = pe.compile(DAT_GRAMMAR, actions=ACTIONS, ignore=Star(Class(" \t\n\r\v\f")), flags=pe.OPTIMIZE | pe.MEMOIZE)

def _init_parse_results(records: list) -> tuple[ClrMamePro, Sequence[Game]]:
    """Initialize ClrMamePro and Game objects from parsed records."""
    if not records:
        raise ValueError("No records found in the DAT file.")

    # Find clrmamepro record
    clrmamepro = ClrMamePro(records[0])
    if 'name' not in clrmamepro:
        raise ValueError("clrmamepro record must have a 'name' field.")

    game_records = records[1:]

    def init_rom(rom_data: dict) -> Rom:
        """Initialize a Rom object from parsed data."""
        kwargs = dict(rom_data)
        if 'size' in kwargs:
            try:
                kwargs['size'] = int(kwargs['size'])
            except ValueError as e:
                raise ValueError(f"Invalid size value in rom record: {kwargs['size']}") from e
        return Rom(**kwargs)

    def init_game(game_data: dict) -> Game | None:
        """Initialize a Game object from parsed data."""
        if 'rom' not in game_data:
            return None

        # Handle other game properties
        kwargs = dict(game_data)
        for key in ['edge_issue', 'edge_rating', 'famitsu_rating', 'releaseday', 'releasemonth', 'releaseyear', 'users', 'year']:
            if key in kwargs:
                try:
                    kwargs[key] = int(kwargs[key])
                except ValueError:
                    pass

        for key in ['analog', 'rumble']:
            if key in kwargs:
                kwargs[key] = kwargs[key].lower() in ('true', 'yes', '1')

        return Game(**kwargs)

    games = tuple(g for g in (init_game(game_data) for game_data in game_records) if g is not None)

    return clrmamepro, games

class DatFile(Sized, Iterable[Game]):
    @property
    def clrmamepro(self) -> ClrMamePro:
        return self._clrmamepro

    @property
    def games(self) -> Sequence[Game]:
        return self._games

    @property
    def path(self) -> str | None:
        return self._path

    def __init__(self, records: Iterable[DatRecord] | str | TextIO):
        self._path: str | None = None
        self._clrmamepro: ClrMamePro
        match records:
            case str() as dat_string:
                match_result = dat_parser.match(dat_string)
                if match_result is None:
                    raise ValueError("Failed to parse DAT string")
                parsed_records = match_result.value()
                self._clrmamepro, self._games = _init_parse_results(parsed_records)
            case TextIO() | TextIOWrapper() as dat_io:
                dat_content = dat_io.read()
                match_result = dat_parser.match(dat_content)
                if match_result is None:
                    raise ValueError("Failed to parse DAT file")
                parsed_records = match_result.value()
                self._clrmamepro, self._games = _init_parse_results(parsed_records)
                self._path = dat_io.name
            case Iterable() as dat_records:
                dats = tuple(dat_records)
                self._clrmamepro = dats[0]
                self._games = tuple(Game(d) for d in dats[1:])
            case _:
                raise TypeError(f"Unsupported type for records: {type(records)}")

    def to_dict(self):
        return {
            "clrmamepro": self._clrmamepro,
            "games": tuple(game for game in self._games)
        }

    @typing.override
    def __iter__(self) -> Iterator[Game]:
        return self._games.__iter__()

    @typing.override
    def __len__(self) -> int:
        return len(self._games)

def crc_key(game: Game) -> str:
    if 'rom' not in game:
        return ''

    roms = game['rom']
    if isinstance(roms, Sequence) and len(roms) > 0:
        rom = roms[0]
    else:
        rom = roms

    if 'crc' in rom:
        return rom['crc'].lower()
    elif 'serial' in rom:
        return rom['serial'].lower()
    else:
        return ''

async def handle_tojson(args: argparse.Namespace):
    with open(args.infile, 'r', encoding='utf-8') as infile:
        dat = DatFile(infile)
        json.dump(dat, sys.stdout, indent=2, default=lambda o: o.to_dict(), ensure_ascii=False)
        print('')  # Ensure a newline at the end of the output

def load_dat(dat_path: str) -> DatFile | None:
    start = time.perf_counter_ns()
    try:

        with open(dat_path, 'r', encoding='utf-8') as infile:
            dat = DatFile(infile)
    except ParseError as e:
        # Don't want to let one bad record crash the whole process
        return None
    except Exception as e:
        raise Exception(f"Failed to load DAT file {dat_path}: {e}") from e

    finish = time.perf_counter_ns()
    print(f"Loaded DAT file from {dat_path} with {len(dat.games)} games in {(finish - start) / 1_000_000:.2f} ms")

    return dat

def game_name_key(game: Game):
    if 'name' in game:
        return game['name']

    if 'description' in game:
        return game['description']

    if 'comment' in game:
        return game['comment']

    return ''

class DatRepository(Mapping[str, Collection[Game]]):
    def __init__(self, dats: Iterable[DatFile], playlists: Iterable[Playlist]):
        self.dats: dict[str, Collection[Game]] = {}

        playlists_with_alts: dict[str, Playlist] = {}
        for p in playlists:
            playlists_with_alts[p.title] = p
            for a in p.alts:
                playlists_with_alts[a] = p

        def playlist_key(dat: DatFile):
            name = dat.clrmamepro['name']
            if playlist := playlists_with_alts.get(name):
                # If we have a playlist that matches this DAT's name field, return the "canonical" title
                return playlist.title
            else:
                # Otherwise just use the DAT's name as-is
                return name

        def union_games(games: Iterable[Game]) -> Game:
            game: dict = {}
            for g in games:
                for k in g:
                    if k not in game:
                        # Add new key-value pairs
                        game[k] = g[k]

            return typing.cast(Game, game)


        dats_list = [d for d in dats if len(d) > 0]
        dats_list.sort(key=playlist_key)
        dats_by_platform = itertools.groupby(dats_list, key=playlist_key)
        for (platform, dat_group) in dats_by_platform:
            # For each set of DAT files that represent the same platform...
            games: list[Game] = sorted(itertools.chain.from_iterable(dat_group), key=crc_key)
            grouped_games: Iterator[tuple[str, Iterator[Game]]] = itertools.groupby(games, crc_key)
            games_by_crc = {k: union_games(v) for (k, v) in grouped_games}
            self.dats[platform] = tuple(sorted(games_by_crc.values(), key=game_name_key))

        pass
        #dat_records: list[DatGame] = sorted(itertools.chain.from_iterable(self.dats), key=dat_key)
        #self.games = {k: tuple(v) for (k, v) in itertools.groupby(self.dat_records, dat_key)}
        #self.unioned_games = {k: ChainMap(*v) for (k, v) in self.games.items()}

    def __getitem__(self, key: str, /) -> Collection[Game]:
        if key in self.dats:
            return self.dats[key]

        raise KeyError(key)

    def __len__(self) -> int:
        return len(self.dats)

    def __iter__(self) -> Iterator[str]:
        return iter(self.dats)

async def load_dats(dats: Iterable[str], playlists: Iterable[Playlist]) -> DatRepository:
    with ProcessPoolExecutor() as executor:
        async def asynciter() -> AsyncIterator[DatFile]:
            for d in executor.map(load_dat, dats, chunksize=16):
                if d:
                    yield d
                await asyncio.sleep(0)  # Yield control to the event loop

        dat_playlists = [f async for f in asynciter()]

    return DatRepository(dat_playlists, playlists)

def get_existing_dat_files(datdir: str) -> Iterator[str]:
    for (dirpath, dirnames, filenames) in os.walk(datdir):
        for file in filter(lambda f: f.endswith('.dat'), filenames):
            if not ('xml' in file or 'XML' in file):  # Exclude XML files
                yield os.path.join(dirpath, file)

async def handle_bench(args: argparse.Namespace):

    existing_dat_paths = [os.path.realpath(p) for p in itertools.chain(get_existing_dat_files("dat"), get_existing_dat_files("metadat"))]
    start = time.perf_counter_ns()
    dat_repo = await load_dats(existing_dat_paths, PLAYLISTS)
    now = time.perf_counter_ns()
    print(f"Loaded {len(existing_dat_paths)} DAT files in {(now - start) / 1_000_000:.2f} ms")

def main():
    parser = argparse.ArgumentParser(
        description="Utilities for processing DAT files.",
        prog="dat"
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    tojson_parser = subparsers.add_parser(
        "tojson",
        help="Convert a DAT file to JSON format and print it to stdout."
    )
    tojson_parser.add_argument(
        "infile",
        type=str,
        help="Path to the input DAT"
    )
    tojson_parser.set_defaults(func=handle_tojson)

    bench_parser = subparsers.add_parser(
        "bench",
        help="Benchmark loading all DAT files in the 'dat' and 'metadat' directories."
    )
    bench_parser.set_defaults(func=handle_bench)

    args = parser.parse_args()
    asyncio.run(args.func(args))

if __name__ == "__main__":
    main()

__all__ = [
    "DatFile",
    "Game",
    "Rom",
    "ClrMamePro",
    "DatRepository",
    "load_dat",
    "load_dats",
    "get_existing_dat_files",
]