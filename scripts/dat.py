#!/usr/bin/env python3

import argparse
import itertools
import json
import typing
from collections.abc import Iterable, Sequence, Iterator, Mapping, Collection, Sized
from io import TextIOWrapper
from typing import TypedDict, Required, TypeAlias, TextIO, Union
import sys

import pe
from pe import OPTIMIZE
from pe.operators import Class, Star


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

def main():
    parser = argparse.ArgumentParser(
        description="Convert DAT files to JSON and print them to stdout.",
        prog="dat"
    )

    parser.add_argument(
        "infile",
        type=str,
        help="Path to the input DAT"
    )

    args = parser.parse_args()

    with open(args.infile, 'r', encoding='utf-8') as infile:
        dat = DatFile(infile)
        json.dump(dat, sys.stdout, indent=2, default=lambda o: o.to_dict(), ensure_ascii=False)
        print('')  # Ensure a newline at the end of the output

if __name__ == "__main__":
    main()
