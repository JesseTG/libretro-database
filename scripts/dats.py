#!/usr/bin/env python3

import argparse
import asyncio
import dataclasses
import itertools
import json
import os
import os.path
import sys
import time
import typing

from collections.abc import Iterable, Sequence, Iterator, Mapping, Collection, Sized, AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from io import TextIOWrapper
from typing import Any, NamedTuple, Self, TextIO, TypeAlias, TypedDict, cast

# pe lacks type stubs, so let's silence MyPy's complaints
import pe  # type: ignore
from pe import ParseError
from pe.actions import Call, Pack
from pe.operators import Class, Star  # type: ignore

from igdb_playlists import Playlist, PLAYLISTS


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ClrMamePro:
    name: str
    description: str | None = None
    category: str | None = None
    date: str | None = None
    author: str | None = None
    email: str | None = None
    url: str | None = None
    version: str | None = None
    comment: str | None = None
    homepage: str | None = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ClrMamePro":
        if not isinstance(data, Mapping):
            raise ValueError(f"Expected first record to be a Mapping, got {type(data)} instead.")

        if 'name' not in data:
            raise ValueError("clrmamepro record must have a 'name' field.")
    
        return ClrMamePro(**data)

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Rom:
    crc: str | None = None
    serial: str | None = None
    image: str | None = None
    name: str | None = None
    size: int | None = None
    md5: str | None = None
    sha1: str | None = None
    sha1sum: str | None = None
    genre: str | None = None
    users: str | None = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Rom":
        kwargs = dict(data)
        if 'size' in kwargs:
            try:
                kwargs['size'] = int(kwargs['size'])
            except ValueError as e:
                raise ValueError(f"Invalid size value in rom record: {kwargs['size']}") from e
            
        return Rom(**kwargs)

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Game:
    rom: Sequence[Rom]
    name: str | None = None
    comment: str | None = None
    description: str | None = None
    id: str | None = None

    analog: bool | None = None
    bbfc_rating: str | None = None
    code: str | None = None
    date: str | None = None
    developer: str | None = None
    download: str | None = None
    edge_issue: int | None = None
    edge_rating: int | None = None
    elspa_rating: str | None = None
    enhancement_hardware: str | None = None
    enhancement_hw: str | None = None
    esrb_rating: str | None = None
    famitsu_rating: int | None = None
    franchise: str | None = None
    genre: str | None = None
    homepage: str | None = None
    license: str | None = None
    manufacturer: str | None = None
    origin: str | None = None
    patch: str | None = None
    publisher: str | None = None
    region: str | None = None
    releaseday: int | None = None
    releasemonth: int | None = None
    releaseyear: int | None = None
    rumble: bool | None = None
    serial: str | None = None
    tags: str | None = None
    users: int | None = None
    version: str | None = None
    year: int | None = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Game":
        kwargs = dict(data)
        if 'rom' in kwargs:
            rom_data = kwargs['rom']
            if isinstance(rom_data, Sequence):
                kwargs['rom'] = tuple(Rom.from_dict(r) if isinstance(r, dict) else Rom() for r in rom_data)
            elif isinstance(rom_data, dict):
                kwargs['rom'] = (Rom.from_dict(rom_data),)
            else:
                kwargs['rom'] = (Rom(),)
        else:
            kwargs['rom'] = (Rom(),)

        for key in ['edge_issue', 'edge_rating', 'famitsu_rating', 'releaseday', 'releasemonth', 'releaseyear', 'users', 'year']:
            if key in kwargs and kwargs[key] is not None:
                try:
                    kwargs[key] = int(kwargs[key])
                except ValueError:
                    pass

        for key in ['analog', 'rumble']:
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = str(kwargs[key]).lower() in ('true', 'yes', '1')

        return Game(**kwargs)

DatRecord: TypeAlias = Mapping[str, "DatValue"]
DatValue: TypeAlias = str | Sequence[DatRecord] | DatRecord
ParsedGameDatList: TypeAlias = Sequence[ClrMamePro | Game]

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

def _build_record(*args, **kwargs):
    """Build a record dictionary from parsed data."""
    record_type = kwargs.get('type', '')

    match kwargs.get('type', None):
        case None:
            raise ValueError("Record missing 'type' field.")
        case 'clrmamepro':
            clrmamepro_args = args[0]
            return ClrMamePro.from_dict(clrmamepro_args)
        case 'game':
            game_args = args[0]
            return Game.from_dict(game_args)
        case _:
            raise ValueError(f"Unknown record type: {record_type}")

class KeyValueDict(TypedDict):
    key: str
    value: DatValue

class KeyValueTuple(NamedTuple):
    key: str
    value: DatValue

def _build_record_content(args: tuple[KeyValueTuple, ...], **_):
    """Build record content from key-value pairs."""

    # group items with duplicate keys together, and put them in a tuple
    def flatten(value: Iterator[KeyValueTuple]):
        result = tuple(v for (_, v) in value)
        return result[0] if len(result) == 1 else result

    grouped_by_key = itertools.groupby(args, lambda x: x.key)
    result = {k:flatten(v) for k, v in grouped_by_key}
    return result

def _build_datfile(*args, **kwargs):
    """Build the top-level DAT file structure."""
    return args

# Actions for semantic processing
ACTIONS = {
    'Record': _build_record,
    'RecordContent': Pack(_build_record_content),
    'KeyValue': Call(KeyValueTuple),
    'DatFile': _build_datfile,
}

# Compile the parser
dat_parser = pe.compile(DAT_GRAMMAR, actions=ACTIONS, ignore=Star(Class(" \t\n\r\v\f")), flags=pe.OPTIMIZE | pe.MEMOIZE)

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
                parse_results = cast(ParsedGameDatList, match_result.value())
                self._clrmamepro, self._games = self._init_parse_results(parse_results)
            case TextIO() | TextIOWrapper() as dat_io:
                dat_content = dat_io.read()
                match_result = dat_parser.match(dat_content)
                if match_result is None:
                    raise ValueError("Failed to parse DAT file")
                parse_results = cast(ParsedGameDatList, match_result.value())
                self._clrmamepro, self._games = self._init_parse_results(parse_results)
                self._path = dat_io.name
            case _:
                raise TypeError(f"Unsupported type for records: {type(records)}")

    def to_dict(self):
        return {
            "clrmamepro": dataclasses.asdict(self._clrmamepro),
            "games": tuple(dataclasses.asdict(game) for game in self._games)
        }

    @typing.override
    def __iter__(self) -> Iterator[Game]:
        return self._games.__iter__()

    @typing.override
    def __len__(self) -> int:
        return len(self._games)

    @staticmethod
    def _init_parse_results(records: Sequence[ClrMamePro | Game])-> tuple[ClrMamePro, Sequence[Game]]:
        if not records:
            raise ValueError("No records found in the DAT file.")

        return cast(ClrMamePro, records[0]), cast(Sequence[Game], records[1:])


def crc_key(game: Game) -> str:
    if not game.rom:
        return ''

    roms = game.rom
    rom = roms[0] if len(roms) > 0 else None
    
    if not rom:
        return ''

    if rom.crc:
        return rom.crc.lower()
    elif rom.serial:
        return rom.serial.lower()
    else:
        return ''

async def handle_tojson(args: argparse.Namespace):
    with open(args.infile, 'r', encoding='utf-8') as infile:
        dat = DatFile(infile)
        json.dump(dat.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
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
    print(f"Loaded \"{dat_path}\" with {len(dat.games)} games in {(finish - start) / 1_000_000:.2f} ms")

    return dat

def game_name_key(game: Game):
    if game.name:
        return game.name

    if game.description:
        return game.description

    if game.comment:
        return game.comment

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
            name = dat.clrmamepro.name
            if playlist := playlists_with_alts.get(name):
                # If we have a playlist that matches this DAT's name field, return the "canonical" title
                return playlist.title
            else:
                # Otherwise just use the DAT's name as-is
                return name

        def union_games(games: Iterable[Game]) -> Game:
            # For dataclasses, we need to merge the games by creating a new instance
            # with the first non-None value for each field
            games_list = tuple(games)
            if not games_list:
                raise ValueError("Cannot union empty games list")

            if len(games_list) == 1:
                # Only one game, nothing to merge
                return games_list[0]
            
            first_game = games_list[0]
            # Start with first game's values
            kwargs = dataclasses.asdict(first_game)
            
            for g in games_list[1:]:
                # Update with non-None values from subsequent games, but keep existing values
                for field in dataclasses.fields(Game):
                    value = getattr(g, field.name)
                    if value is not None and kwargs[field.name] is None:
                        # Only update if we don't already have a value
                        kwargs[field.name] = value

            return Game(**kwargs)

        # Sort the DATs by logical name to simplify manual inspection,
        # and omit DAT files that don't actually have any games.
        dats_list = sorted((d for d in dats if len(d) > 0), key=playlist_key)
        dats_by_platform = itertools.groupby(dats_list, key=playlist_key)
        for (platform, dat_group) in dats_by_platform:
            # For each set of DAT files that represent the same platform...

            # Sort the DAT files by name, as the README says that
            # earlier-named DATs take precedence over later ones
            # when the same field is defined in more than one.
            dats_sorted = sorted(dat_group, key=lambda d: cast(str, d.path))

            # Sort all games (across all DAT files) by CRC,
            # since itertools.groupby needs the input to be sorted by the key function
            games: list[Game] = sorted(itertools.chain.from_iterable(dats_sorted), key=crc_key)
            grouped_games = itertools.groupby(games, crc_key)
            games_by_crc = {crc: union_games(g) for (crc, g) in grouped_games}
            self.dats[platform] = tuple(sorted(games_by_crc.values(), key=game_name_key))

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