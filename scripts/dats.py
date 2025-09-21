#!/usr/bin/env python3

import argparse
import asyncio
import dataclasses
import functools
import itertools
import json
import os
import os.path
from pathlib import Path
import sys
import time
import typing

from collections.abc import Iterable, Sequence, Iterator, Mapping, Collection, Sized, AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from io import TextIOWrapper
from itertools import groupby
from typing import Any, NamedTuple, TextIO, TypeAlias, TypedDict, cast

# pe lacks type stubs, so let's silence MyPy's complaints
import pe  # type: ignore
from pe import ParseError
from pe.actions import Call, Pack
from pe.operators import Class, Star
import typelib
import typelib.ctx
import typelib.serdes

from igdb_playlists import Playlist


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

    def __post_init__(self):
        # Called by dataclasses after __init__, but before the instance is returned
        if not self.crc and not self.serial:
            raise ValueError("Rom record must have at least a 'crc' or 'serial' field.")

    @property
    def id(self) -> str:
        if self.crc:
            return self.crc.lower()

        if self.serial:
            return self.serial.lower()

        raise TypeError("Rom record has neither 'crc' nor 'serial' field.")


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

    @property
    def name_key(self) -> str:
        if self.name:
            return self.name

        if self.description:
            return self.description

        if self.comment:
            return self.comment

        return ''

    @property
    def crc_key(self) -> str:
        if not self.rom:
            return ''

        roms = self.rom
        rom = roms[0] if len(roms) > 0 else None

        if not rom:
            return ''

        return rom.id

DatValue: TypeAlias = "str | Sequence[DatRecord] | DatRecord"
DatRecord: TypeAlias = Mapping[str, DatValue]

ParsedGameDatList: TypeAlias = tuple[ClrMamePro, *tuple[Game, ...]]
''' A parsed DAT file is a tuple where the first element is a ClrMamePro record,
and the remaining elements are Game records. '''

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
    return args[0]

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
    return list(args)

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

class ParsedGameDatListMarshaller(typelib.AbstractMarshaller[ParsedGameDatList]):
    def __call__(self, value: ParsedGameDatList) -> typelib.serdes.MarshalledValueT:
        raise NotImplementedError("TODO: Implement marshalling from ParsedGameDatList to serializable object")

class ParsedGameDatListUnmarshaller(typelib.AbstractUnmarshaller[ParsedGameDatList]):
    def __call__(self, value: typelib.serdes.MarshalledValueT) -> ParsedGameDatList:
        if isinstance(value, str):
            raise TypeError("Expected a sequence for unmarshalling ParsedGameDatList, got str")

        if not isinstance(value, Sequence):
            raise TypeError(f"Expected a sequence for unmarshalling ParsedGameDatList, got {type(value)}")

        if not value:
            raise ValueError("Cannot unmarshal empty sequence to ParsedGameDatList")

        clrmamepro_dict = value[0]
        if not isinstance(clrmamepro_dict, dict):
            raise TypeError(f"Expected first element of sequence to be a dict for ClrMamePro, got {type(value[0])}")

        try:
            clrmamepro = typelib.unmarshal(ClrMamePro, clrmamepro_dict)
        except (LookupError, ValueError) as e:
            raise ValueError(f"Failed to unmarshal clrmamepro record") from e

        # Need to unmarshal each record separately,
        # as the default unmarshaller doesn't handle `ParsedGameDatList` correctly.
        game_dicts = value[1:]
        games = tuple(typelib.unmarshal(Game, g) for g in game_dicts)

        # Return a tuple with the ClrMamePro as the first element,
        # and the rest as Game records.
        return clrmamepro, *games


def encode_dat(value: typelib.serdes.MarshalledValueT) -> bytes:

    raise NotImplementedError("TODO: Implement encoding from DatFile to bytes")

def decode_dat(value: bytes) -> typelib.serdes.MarshalledValueT:
    dat = value.decode('utf-8')

    match_result = dat_parser.match(dat)
    if match_result is None:
        raise ValueError("Failed to parse DAT string")

    result = match_result.value()
    assert result is not None
    return result

ctx = typelib.ctx.TypeContext()
GameDataListCodec: typelib.Codec[ParsedGameDatList] = typelib.codec(
    ParsedGameDatList,
    marshaller=ParsedGameDatListMarshaller(ParsedGameDatList, ctx),
    unmarshaller=ParsedGameDatListUnmarshaller(ParsedGameDatList, ctx),
    encoder=encode_dat,
    decoder=decode_dat
)

async def handle_tojson(args: argparse.Namespace):
    with open(args.infile, 'rb') as infile:
        dat = GameDataListCodec.decode(infile.read())
        json.dump(dat, sys.stdout, indent=2, ensure_ascii=False, default=dataclasses.asdict)
        print('')  # Ensure a newline at the end of the output

class LoadedDat(NamedTuple):
    playlist: str
    path: Path
    clrmamepro: ClrMamePro
    games: Sequence[Game]

def load_dat(dat_path: tuple[str, Path]) -> LoadedDat | None:
    """
    Load a DAT file from the given path.
    :param dat_path: A tuple of (playlist name, path to the DAT file).
    :return: The loaded DatFile, or None if there was an error.

    :note: Including the playlist title in the argument
    simplifies parallel processing with ProcessPoolExecutor.
    """
    start = time.perf_counter_ns()
    try:
        with open(dat_path[1], 'rb') as infile:
            dat = GameDataListCodec.decode(infile.read())
    except ParseError as e:
        # Don't want to let one bad record crash the whole process
        return None
    except Exception as e:
        raise Exception(f"Failed to load DAT file {dat_path}: {e}") from e

    finish = time.perf_counter_ns()
    print(f"Loaded \"{dat_path}\" with {len(dat)} records in {(finish - start) / 1_000_000:.2f} ms")

    return LoadedDat(
        *dat_path,
        clrmamepro=dat[0],
        games=dat[1:]
    )

async def load_dats(dat_playlists: Mapping[str, Sequence[Path]]) -> Mapping[str, Collection[Game]]:
    """
    Load game data from DAT files for each playlist.

    :param paths: A mapping of playlist names to the paths of the DAT files
    that contain the game data for those playlists.

    :return: A mapping of playlist names to the games in those playlists.
      Each game will be combined from all DAT files for that playlist,
      with precedence given to earlier DAT files in the list.
    """
    paths = itertools.chain.from_iterable(
        ((name, p) for p in dats) for (name, dats) in dat_playlists.items()
    )
    # Break the mapping of playlist names to lists of paths
    # into a flat iterable of (playlist name, path) tuples

    with ProcessPoolExecutor() as executor:
        async def asynciter() -> AsyncIterator[LoadedDat]:
            for d in executor.map(load_dat, paths, chunksize=16):
                if d:
                    yield d
                await asyncio.sleep(0)  # Yield control to the event loop

        dat_files = [pair async for pair in asynciter()]

    def reduce_game(map: dict[str, Any], game: Game) -> dict[str, Any]:
        for field in dataclasses.fields(Game):
            value = getattr(game, field.name)
            if value is not None and map.get(field.name) is None:
                # Only update if we don't already have a value
                map[field.name] = value
        return map

    def reduce_games(games: Iterable[Game]) -> Game:
        game_dict = functools.reduce(reduce_game, games, {})
        return Game(**game_dict)

    def reduce_dats(dats: Iterable[LoadedDat]) -> Collection[Game]:
        games_iterable = itertools.chain.from_iterable(d.games for d in dats)
        games = sorted(games_iterable, key=lambda g: g.crc_key)
        games_by_crc = itertools.groupby(games, key=lambda g: g.crc_key)
        reduced_games = tuple(reduce_games(dats) for (crc, dats) in games_by_crc)
        return reduced_games

    # Group the loaded DAT files by playlist name
    dat_files.sort(key=lambda p: p.playlist)
    dat_groups = itertools.groupby(dat_files, key=lambda p: p.playlist)
    game_groups = ((p, reduce_dats(g)) for (p, g) in dat_groups)

    return dict(game_groups)

def get_existing_dat_files(datdir: str) -> Iterator[str]:
    for (dirpath, dirnames, filenames) in os.walk(datdir):
        for file in filter(lambda f: f.endswith('.dat'), filenames):
            if not ('xml' in file or 'XML' in file):  # Exclude XML files
                yield os.path.join(dirpath, file)

def get_target_dat_paths(outpath: Path, playlist_titles: Iterable[str]) -> Iterator[Path]:
    """Get the paths to the DAT files that will be generated from the given playlists, rooted at the given directory."""
    for title in playlist_titles:
        yield outpath / f"{title}.dat"

async def handle_bench(args: argparse.Namespace):
    from igdb_playlists import PLAYLISTS, get_playlist

    # TODO: Don't hardcode these paths
    existing_dat_paths = {Path(p) for p in itertools.chain(get_existing_dat_files("dat"), get_existing_dat_files("metadat"))}
    playlists = ((get_playlist(p), p) for p in existing_dat_paths)
    playlists_to_dats = ((p.title, d) for (p, d) in playlists if p)
    sorted_by_title = sorted(playlists_to_dats, key=lambda x: x[0])
    datgroups = {k: tuple(vv[1] for vv in v) for k, v in groupby(sorted_by_title, key=lambda x: x[0])}

    start = time.perf_counter_ns()
    dats = await load_dats(datgroups)
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