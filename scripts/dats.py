#!/usr/bin/env python3

import argparse
import asyncio
import dataclasses
import functools
import itertools
import json
import os
import os.path
import sys
import time

from collections.abc import Iterable, Sequence, Iterator, Mapping, Collection, AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from itertools import groupby
from pathlib import Path
from typing import Any, NamedTuple, Optional, TypeAlias, TypedDict, Union

# pe lacks type stubs, so let's silence MyPy's complaints
import pe  # type: ignore
from pe import ParseError
from pe.actions import Call, Pack
from pe.operators import Class, Star
import typelib
import typelib.ctx
import typelib.serdes

from typelib.serdes import MarshalledValueT

from igdb import PlaylistTitle

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ClrMamePro:
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    date: Optional[str] = None
    author: Optional[str] = None
    email: Optional[str] = None
    url: Optional[str] = None
    version: Optional[str] = None
    comment: Optional[str] = None
    homepage: Optional[str] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Rom:
    crc: Optional[str] = None
    serial: Optional[str] = None
    image: Optional[str] = None
    name: Optional[str] = None
    size: Optional[int] = None
    md5: Optional[str] = None
    sha1: Optional[str] = None
    sha1sum: Optional[str] = None
    genre: Optional[str] = None
    users: Optional[str] = None

    def __post_init__(self):
        # Called by dataclasses after __init__, but before the instance is returned
        if not self.crc and not self.serial:
            raise ValueError("Rom record must have at least a 'crc' or 'serial' field.")

    def same_as(self, other: 'Rom') -> bool:
        if self.crc and other.crc and self.crc.lower() == other.crc.lower():
            return True

        if self.serial and other.serial and self.serial.lower() == other.serial.lower():
            return True

        if self.md5 and other.md5 and self.md5.lower() == other.md5.lower():
            return True

        selfsha = self.sha1 or self.sha1sum
        othersha = other.sha1 or other.sha1sum

        if selfsha and othersha and selfsha.lower() == othersha.lower():
            return True

        return False

    def __or__(self, other: 'Rom') -> 'Rom':
        # Merge two Rom records, preferring non-None values from self
        if not isinstance(other, Rom):
            return NotImplemented

        this = {k: v for k, v in dataclasses.asdict(self).items() if v is not None}
        that = {k: v for k, v in dataclasses.asdict(other).items() if v is not None}

        return Rom(**(that | this))

    @property
    def id(self) -> str:
        if self.crc:
            return self.crc.lower()

        if self.serial:
            return self.serial.lower()

        raise TypeError("Rom record has neither 'crc' nor 'serial' field.")


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Game:
    """
    A parsed and unmarshalled game record from a DAT file.

    Unrecognized fields are ignored.
    You can read or write a new field by adding it to this class.

    At least one of `name`, `description`, `comment`, or `id` should be present.
    """
    name: Optional[str] = None
    comment: Optional[str] = None
    description: Optional[str] = None
    id: Optional[str] = None

    achievements: Optional[int] = None
    analog: Optional[bool] = None
    artstyle: Optional[str] = None
    """May include multiple art styles separated by commas, slashes, or pipes."""

    bbfc_rating: Optional[str] = None
    category: Optional[str] = None
    """May include multiple categories separated by commas, slashes, or pipes."""

    cero_rating: Optional[str] = None
    code: Optional[str] = None
    console_exclusive: Optional[bool] = None
    controls: Optional[str] = None
    coop: Optional[bool] = None
    date: Optional[str] = None
    developer: Optional[str] = None
    """May include multiple developers separated by commas, slashes, or pipes"""

    download: Optional[str] = None
    edge_issue: Optional[int] = None
    edge_rating: Optional[int] = None
    elspa_rating: Optional[str] = None
    enhancement_hardware: Optional[str] = None
    enhancement_hw: Optional[str] = None
    esrb_rating: Optional[str] = None
    famitsu_rating: Optional[int] = None
    franchise: Optional[str] = None
    gameplay: Optional[str] = None
    """May include multiple gameplay types separated by commas, slashes, or pipes."""

    genre: Optional[str] = None
    """May include multiple genres separated by commas, slashes, or pipes."""

    homepage: Optional[str] = None
    igdb_id: Optional[int] = None
    igdb_url: Optional[str] = None
    """URL of the IGDB page for this game."""

    igdb_platform_id: Optional[int] = None
    igdb_release_date_id: Optional[int] = None
    language: Optional[str] = None
    """May include multiple languages separated by commas, slashes, or pipes."""

    license: Optional[str] = None
    manufacturer: Optional[str] = None
    media: Optional[str] = None
    """May include multiple media types separated by commas, slashes, or pipes."""

    narrative: Optional[str] = None
    """May include multiple narrative types separated by commas, slashes, or pipes."""

    origin: Optional[str] = None

    pacing: Optional[str] = None
    """May include multiple pacing types separated by commas, slashes, or pipes."""

    patch: Optional[str] = None
    pegi_rating: Optional[str] = None
    perspective: Optional[str] = None
    platform_exclusive: Optional[bool] = None
    publisher: Optional[str] = None
    """May include multiple publishers separated by commas, slashes, or pipes."""

    region: Optional[str] = None
    releaseday: Optional[int] = None
    releasemonth: Optional[int] = None
    releaseyear: Optional[int] = None
    rumble: Optional[bool] = None
    score: Optional[str] = None
    serial: Optional[str] = None
    setting: Optional[str] = None
    tags: Optional[str] = None
    users: Optional[int] = None
    vehicular: Optional[str] = None
    """May include multiple vehicule types separated by commas, slashes, or pipes."""

    version: Optional[str] = None
    visual: Optional[str] = None
    """May include multiple visual types separated by commas, slashes, or pipes."""

    # May be a string because of entries like "???" for unknown years,
    # or "198?" for an unknown year in the 1980s
    year: Optional[int | str] = None

    # Declared last so that it appears last in the generated DATs
    rom: Optional[Sequence[Rom]] = None

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

        rom = self.rom if isinstance(self.rom, Rom) else self.rom[0]

        if not rom:
            return ''

        return rom.id

DatValue: TypeAlias = Union[str, Sequence["DatRecord"], "DatRecord"]
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
"""
I don't know of a formal spec for DAT files,
so I wrote this PEG based on my observations
of the DAT files in this repo.
It should handle all of them.

These are the rules I came up with:

- A Record is a parentheses-wrapped sequence of key-value pairs.
- The key is a string that's a valid C identifier (plus hyphens).
- The value is either a string or another record.
- Strings may be quoted or unquoted.
- Unquoted strings may not contain spaces or parentheses.
- Quoted strings may contain escaped quotes and backslashes.
- Whitespace and newlines outside of strings is ignored.
- Any key may appear multiple times in a record.
- A DAT file is a top-level record with implicit parentheses,
  and all values are records.

We don't try to interpret the meaning of any keys or values while parsing;
this means we just treat everything as a string,
and let the unmarshalling step figure out what to do with it.
"""

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
    def dedupe_keys(value: Iterator[KeyValueTuple]):
        result = tuple(v for (_, v) in value)
        match result:
            case (str(),) as result_str:
                return result_str[0]
            case (dict(), *_) as records:
                return records
            case _:
                return result

    sorted_by_key = sorted(args, key=lambda x: x.key)
    grouped_by_key = itertools.groupby(sorted_by_key, lambda x: x.key)
    result = {k:dedupe_keys(v) for k, v in grouped_by_key}
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

class ParsedGameDatListMarshaller(typelib.AbstractMarshaller[ParsedGameDatList]):
    def __call__(self, value: ParsedGameDatList) -> typelib.serdes.MarshalledValueT:
        return [dataclasses.asdict(g) for g in value] # type: ignore

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
    # value is a list of dicts, one per DAT record
    if isinstance(value, str):
        raise TypeError("Expected a sequence for encoding ParsedGameDatList, got str")

    if not isinstance(value, Sequence):
        raise TypeError(f"Expected a sequence for encoding ParsedGameDatList, got {type(value)}")

    output = BytesIO()

    def write_record(val: 'tuple[MarshalledValueT, MarshalledValueT]', indent = 0):
        match val:
            case (str(), None):
                return # An absent field, skip it
            case (str(key), bool(b)):
                output.write(b'  ' * indent)
                output.write(key.encode('utf-8'))
                output.write(b' ')
                output.write(b'1\n' if b else b'0\n')
            case (str(key), int() | float() as number):
                output.write(b'  ' * indent)
                output.write(key.encode('utf-8'))
                output.write(b' ')
                output.write(str(number).encode('utf-8'))
                output.write(b'\n')
            case (str(key), str(text)):
                output.write(b'  ' * indent)
                output.write(key.encode('utf-8'))
                output.write(b' ')
                # Escape backslashes and double quotes in the string
                escaped = text.replace('\\', '\\\\').replace('"', '\\"')
                output.write(b'"')
                output.write(escaped.encode('utf-8'))
                output.write(b'"\n')
            case (str(key), list() as sequence):
                for item in sequence:
                    # Don't add extra indentation to list items,
                    # as they're encoded as a sequence of records
                    # with the same key
                    # (i.e. there's no real list syntax)
                    write_record((key, item), indent)
                    output.write(b'\n')
            case (str(key), dict() as record):
                output.write(b'  ' * indent)
                output.write(key.encode('utf-8'))
                output.write(b' (\n')
                for pair in record.items():
                    write_record(pair, indent + 1)
                output.write(b'  ' * indent)
                output.write(b')')
            case _:
                raise TypeError(f"Cannot encode {val} of type {type(val)}")

    clrmamepro_dict = value[0]
    if not isinstance(clrmamepro_dict, dict):
        raise TypeError(f"Expected first element of sequence to be a dict for ClrMamePro, got {type(value[0])}")

    write_record(('clrmamepro', clrmamepro_dict))
    output.write(b'\n\n')

    def game_name(game: MarshalledValueT) -> str:
        if not isinstance(game, dict):
            raise TypeError(f"Expected game record to be a dict for encoding a Game, got {type(game)}")

        for key in ('name', 'description', 'comment', 'id'):
            if name := game.get(key):
                return str(name)

        return ''

    for game in sorted(value[1:], key=game_name):
        if not isinstance(game, dict):
            raise TypeError(f"Expected each record to be a dict for encoding a Game, got {game}")

        write_record(('game', game))
        output.write(b'\n\n')

    result = output.getvalue()
    return result


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
    playlist: PlaylistTitle
    path: Path
    clrmamepro: ClrMamePro
    games: Sequence[Game]

def load_dat(dat_path: tuple[PlaylistTitle, Path]) -> LoadedDat | None:
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
    print(f"Loaded {len(dat)} records from \"{str(dat_path[1])}\" in {(finish - start) / 1_000_000:.2f} ms")

    return LoadedDat(
        *dat_path,
        clrmamepro=dat[0],
        games=dat[1:]
    )

async def load_dats(dat_playlists: Mapping[PlaylistTitle, Sequence[Path]], parallel=True) -> Mapping[PlaylistTitle, Collection[Game]]:
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

    if parallel:
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor() as executor:
            futures = (loop.run_in_executor(executor, load_dat, p) for p in paths)
            dat_files = [d for d in await asyncio.gather(*futures) if d]
    else:
        dat_files = [d for d in map(load_dat, paths) if d]

    def reduce_game(merged: dict[str, bool | str | int | Sequence[Rom]], game: Game) -> dict[str, Any]:
        # Merge the fields of `game` into `merged`, without overwriting existing values
        for field in dataclasses.fields(Game):
            name = field.name
            old = merged.get(name)
            new = getattr(game, name)

            match (name, old, new):
                case _, None, None:
                    # Both old and new are None, nothing to do
                    pass
                case _, None, value:
                    # Apply any new field value if we don't already have one
                    merged[name] = value
                case 'year', str(unknown), int(year) if '?' in unknown:
                    # If we have a string year like "198?" or "???"
                    # but we found a specific year in another DAT,
                    # use the integer year
                    merged[name] = year
                case 'rom', None, list(roms) if roms:
                    # Add any ROMs if we haven't found any yet,
                    # but only if the list is non-empty
                    merged[name] = list(roms)
                case 'rom', list(known_roms), list(new_roms) if new_roms:
                    roms = []
                    for k in known_roms:
                        updated_rom = k
                        for n in new_roms:
                            if k.same_as(n):
                                updated_rom = k | n

                        roms.append(updated_rom)

                    merged[name] = roms

        return merged

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

    result = dict(game_groups)
    print(f"Loaded {len(result)} playlists from {len(dat_files)} DAT files")
    return result

def get_existing_dat_files(datdir: str | Path) -> Iterator[str]:
    for (dirpath, dirnames, filenames) in os.walk(datdir):
        for file in filter(lambda f: f.endswith('.dat'), filenames):
            if not ('xml' in file or 'XML' in file):  # Exclude XML files
                yield os.path.join(dirpath, file)

def get_target_dat_paths(outpath: Path, playlist_titles: Iterable[str]) -> Iterator[Path]:
    """Get the paths to the DAT files that will be generated from the given playlists, rooted at the given directory."""
    for title in playlist_titles:
        yield outpath / f"{title}.dat"

async def handle_bench(args: argparse.Namespace):
    from igdb import get_playlist

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
    "Game",
    "Rom",
    "ClrMamePro",
    "load_dat",
    "load_dats",
    "get_existing_dat_files",
]