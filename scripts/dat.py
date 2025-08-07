#!/usr/bin/env python3

import argparse
import json
from collections.abc import Iterable, Sequence, Mapping
from io import TextIOWrapper
from typing import TypedDict, Required, TypeAlias, TextIO
import sys

import pyparsing as pp
from pyparsing import ParseResults


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
    rom: Required[Sequence[Rom]]
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

DatRecord: TypeAlias = "Mapping[str, str | Sequence['DatRecord'] | 'DatRecord']"

# Forward declaration for recursive grammar
dat_record = pp.Forward()

# Basic components
key = pp.Word(pp.alphanums + "_-")
quoted_string = pp.QuotedString('"')
unquoted_string = pp.Word(pp.printables, excludeChars='() \t\n\r')

# Value can be quoted string, unquoted string, or nested record
value = quoted_string | unquoted_string | dat_record

# Record content can be either key-value pairs or nested records

# Record structure: record_type ( content )
record_type = pp.Word(pp.alphanums + "_-")
dat_record <<= pp.Suppress('(') + pp.dict_of(key, value) + pp.Suppress(')')

# Complete DAT file parser (multiple records)
dat_file = pp.dict_of(key, dat_record)

# For convenience, also provide a single record parser
single_record = dat_record

def _init_parse_results(dat: ParseResults) -> tuple[ClrMamePro, Sequence[Game]]:
    if not dat or len(dat) == 0:
        raise ValueError("No records found in the DAT file.")

    dat_clrmamepro: ParseResults = dat[0]
    if dat_clrmamepro[0] != "clrmamepro":
        raise ValueError("First record must be of type 'clrmamepro'.")

    clrmamepro = ClrMamePro(**dat_clrmamepro)
    if 'name' not in clrmamepro:
        raise ValueError("clrmamepro record must have a 'name' field.")

    def init_rom(rom: ParseResults) -> Rom:
        if rom[0] != "rom":
            raise ValueError("Expected a 'rom' record.")

        kwargs = dict()
        if 'size' in rom:
            try:
                kwargs['size'] = int(rom['size'])
            except ValueError as e:
                raise ValueError(f"Invalid size value in rom record: {rom['size']}") from e

        return Rom(rom, **kwargs)

    def init_game(game: ParseResults) -> Game:
        if game[0] != "game":
            raise ValueError("Expected a 'game' record.")

        if 'rom' not in game:
            raise ValueError("Games must have at least one 'rom' record.")

        roms = tuple(init_rom(r) for r in game if r[0] == "rom")

        return Game(game, rom=roms)


    dat_games = dat[1:]
    games = tuple(init_game(g) for g in dat_games if g[0] == "game")

    return clrmamepro, games

class DatFile:
    @property
    def clrmamepro(self) -> ClrMamePro:
        return self._clrmamepro

    @property
    def games(self) -> Sequence[Game]:
        return self._games

    def __init__(self, records: Iterable[DatRecord] | str | TextIO | ParseResults):
        match records:
            case str() as dat_string:
                dat = dat_file.parse_string(dat_string)
                self._clrmamepro, self._games = _init_parse_results(dat)
            case ParseResults() as parsed_dat:
                self._clrmamepro, self._games = _init_parse_results(parsed_dat)
            case TextIO() | TextIOWrapper() as dat_io:
                dat = dat_file.parse_file(dat_io)
                self._clrmamepro, self._games = _init_parse_results(dat)
            case Iterable() as dat_records:
                raise NotImplementedError("Implement this once we start generating DAT files from records.")
            case _:
                raise TypeError(f"Unsupported type for records: {type(records)}")

    def to_dict(self):
        return {
            "clrmamepro": self._clrmamepro,
            "games": tuple(game for game in self._games)
        }

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
