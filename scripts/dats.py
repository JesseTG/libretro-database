#!/usr/bin/env python3

import asyncio
import dataclasses
import functools
import itertools
import sys

from collections.abc import Iterable, Sequence, Collection
from io import StringIO
from itertools import chain, repeat
from pathlib import Path
from typing import IO, Annotated, Any, BinaryIO, ClassVar, Literal, LiteralString, NamedTuple, Self, TextIO

import aiofiles
# pe lacks type stubs, so let's silence MyPy's complaints
from more_itertools import map_reduce, partition
import pe  # type: ignore

from aiomultiprocess import Pool
from pe.actions import Pack
from pe.operators import Class, Star
from pydantic import AliasChoices, BaseModel, ByteSize, DirectoryPath, Field, FilePath, ModelWrapValidatorHandler, SerializationInfo, SerializerFunctionWrapHandler, TypeAdapter, ValidationError, ValidationInfo, model_serializer, model_validator
from pydantic_core import from_json
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict

from igdb import ColumnDef, Playlist, PlaylistTitle
from utils import DatabaseModel, EmptyStringToNone, Hash, WrapInTuple

type DatValidationMode = Literal['dat'] | None
type DatPair = tuple[str, DatValue]
type DatRecord = tuple[DatPair, ...]
type DatValue = str | DatRecord
type DatTopLevelRecord = tuple[str, DatRecord]
type DatFile = tuple[DatTopLevelRecord, ...]


class DatModel(DatabaseModel, frozen=True):
    __dattype__: ClassVar[LiteralString]

    @classmethod
    def from_dat(cls, value: DatRecord | DatTopLevelRecord) -> Self:
        return cls.model_validate(value, context="dat")

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo):
        match info.context:
            case 'dat':
                pass
            case 'row':
                pass
            case _:
                return handler(self)

    @model_validator(mode="wrap")
    @classmethod
    def _validate(cls, data: Any, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo) -> Self:
        """
        If the validation context is 'dat', construct the object from a DAT record.
        """
        if info.context != 'dat':
            return handler(data)

        match data:
            case str(type) | (str(type), [*_]) if type != cls.__dattype__:
                # A DAT key or keyed DatRecord with an unexpected type
                raise ValidationError(f"Expected a DAT type of {cls.__dattype__}, got {type}")
            case str(), str():
                # A DatPair with a string value
                return handler(data)
            case (str(), [*pairs]) | [*pairs]:
                # A DatRecord with multiple DatPairs, possibly as a top-level record
                datdict: dict[str, tuple[DatValue, ...]] = map_reduce(
                    (p for p in pairs if isinstance(p, tuple) and len(p) == 2),
                    lambda pair: str(pair[0]), # the DAT pair type
                    lambda pair: pair[1], # the DAT pair value
                    lambda vals: vals[0] if len(vals) == 1 and not isinstance(vals[0], tuple) else tuple(v for v in vals),
                )
                result = handler(datdict)
                return result
            case _:
                # Handle other cases normally
                result = handler(data)
                return result

class ClrMamePro(DatModel, frozen=True):
    __tablename__ = "DatClrMamePro"
    __dattype__ = "clrmamepro"

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


class Rom(DatModel, frozen=True):
    # TODO: Add a table-level CHECK constraint that at least one of `crc` or `serial` is non-NULL
    __tablename__ = "DatRom"
    __dattype__ = "rom"

    crc: Annotated[Hash, ColumnDef(unique=True, index=True)] | None = None
    serial: Annotated[str, ColumnDef(unique=True, index=True)] | None = None
    image: str | None = None
    name: str | None = None
    size: ByteSize | None = None
    md5: Annotated[Hash, ColumnDef(unique=True, index=True)] | None = None
    sha1: Annotated[Hash, ColumnDef(unique=True, index=True)] | None = None
    genre: str | None = None
    users: str | None = None

    # TODO: Figure out how to ensure that at least one of `crc` or `serial` is non-NULL at the model level
    def same_as(self, other: 'Rom') -> bool:
        if self.crc and other.crc and self.crc.lower() == other.crc.lower():
            return True

        if self.serial and other.serial and self.serial.lower() == other.serial.lower():
            return True

        if self.md5 and other.md5 and self.md5.lower() == other.md5.lower():
            return True

        if self.sha1 and other.sha1 and self.sha1.lower() == other.sha1.lower():
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


class Game(DatModel, frozen=True):
    """
    A parsed and unmarshalled game record from a DAT file.

    Unrecognized fields are ignored.
    You can read or write a new field by adding it to this class.

    At least one of `name`, `description`, `comment`, or `id` should be present.
    """
    __tablename__ = "DatGame"
    __dattype__ = "game"

    name: str | None = None
    comment: WrapInTuple[str] | None = None
    description: str | None = None
    id: str | None = None

    achievements: int | None = None
    analog: bool | None = None

    bbfc_rating: str | None = None
    category: str | None = None
    """May include multiple categories separated by commas, slashes, or pipes."""

    cero_rating: str | None = None
    code: str | None = None
    console_exclusive: bool | None = None
    controls: str | None = None
    coop: bool | None = None
    date: str | None = None
    developer: str | None = None
    """May include multiple developers separated by commas, slashes, or pipes"""

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
    """May include multiple genres separated by commas, slashes, or pipes."""

    homepage: str | None = None
    igdb_id: int | None = None
    igdb_url: str | None = None
    """URL of the IGDB page for this game."""

    igdb_platform_id: int | None = None
    igdb_release_date_id: int | None = None
    language: str | None = None
    """May include multiple languages separated by commas, slashes, or pipes."""

    license: str | None = None
    manufacturer: str | None = None
    media: str | None = None
    """May include multiple media types separated by commas, slashes, or pipes."""
    origin: str | None = None
    patch: WrapInTuple[str] | None = None
    pegi_rating: str | None = None
    perspective: str | None = None
    platform_exclusive: bool | None = None
    publisher: str | None = None
    """May include multiple publishers separated by commas, slashes, or pipes."""

    region: str | None = None
    releaseday: int | None = None
    releasemonth: int | None = None
    releaseyear: EmptyStringToNone[str] = None
    rumble: bool | None = None
    score: str | None = None
    serial: str | None = None
    setting: str | None = None
    tags: str | None = None
    users: int | None = None
    version: str | None = None
    visual: str | None = None

    # May be a string because of entries like "???" for unknown years,
    # or "198?" for an unknown year in the 1980s
    year: int | str | None = None

    # Declared last so that it appears last in the generated DATs
    rom: tuple[Rom, ...] | None = None

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

# PEG grammar for DAT file format
DAT_GRAMMAR = r'''
# Main entry points
DatFile < (DatTopLevelRecord)* EndOfFile

# Record structure
DatTopLevelRecord < type:(~DatKey) Open DatRecord Close
DatRecord <- (DatPair)*
DatPair < key:(~DatKey) value:DatValue

# Keys and Values
DatKey <- [a-zA-Z_][-a-zA-Z0-9_]*
DatValue <- (Open DatRecord Close) / QuotedString / UnquotedString

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
It should handle all of them correctly,
but you can test this by running `python scripts/dats.py check`.

These are the semantics I came up with:

- A DatKey is a string that's a valid C identifier (plus hyphens).
- A DatValue is either a string or a DatRecord.
- A DatPair is a DatKey followed by a DatValue.
- A DatRecord is an ordered sequence of zero or more DatPairs.
- A DatRecord may have multiple pairs with the same key.
  The application may interpret this as a list of values for that key.
- A DatFile is a DatRecord where all DatValues are DatRecords.

This is the syntax I came up with:

- Strings may be quoted or unquoted.
- Unquoted strings may not contain spaces, parentheses, backslashes, or double quotes.
- Quoted strings may contain backslash-escaped quotes and backslashes.
- Whitespace (including newlines) outside of quoted strings is ignored.
- Any key may appear multiple times in a record.
- DatFiles are not wrapped in parentheses.
- DatRecords are wrapped in parentheses.

We don't try to interpret the meaning of any keys or values while parsing;
this means we just treat everything as a string,
and let the unmarshalling step figure out what to do with it.
"""

def build_top_level_record(pairs: tuple[DatPair, ...], type: str) -> DatTopLevelRecord:
    return (type, pairs)

def build_pair(key: str, value: DatValue) -> DatPair:
    return (key, value)

# Actions for semantic processing
ACTIONS = {
    'DatTopLevelRecord': build_top_level_record,
    'DatRecord': Pack(tuple), # Wrap all DatPairs into a tuple
    'DatPair': build_pair, # Wrap the parsed values (bound to "key" and "value") into a DatPair
    'DatFile': Pack(tuple), # Wrap all DatTopLevelRecords into a tuple
}

dat_parser = pe.compile(DAT_GRAMMAR, actions=ACTIONS, ignore=Star(Class(" \t\n\r\v\f")), flags=pe.OPTIMIZE | pe.MEMOIZE | pe.STRICT)

def encode_dat(dat: DatFile, output: IO | None = None):
    if not output:
        output = StringIO()

    def write_pair(pair: DatPair, indent: int = 0) -> None:
        match pair:
            case (key, str(value)):
                # leaf-level DAT pair (like a ROM name)
                output.write('\t' * indent)
                output.write(key)
                output.write(' ')
                # Escape backslashes and double quotes in the string
                escaped = value.replace('\\', '\\\\').replace('"', '\\"')
                output.write('"')
                output.write(escaped)
                output.write('"\n')
            case (key, [*pairs]):
                # nested DAT record (usually a game, clrmamepro, or rom)
                output.write('\t' * indent)
                output.write(key)
                output.write(' (\n')
                for p in pairs:
                    write_pair(p, indent + 1) # type: ignore
                    # p is definitely a DatPair but the type checker says otherwise
                output.write('\t' * indent)
                output.write(')\n')
            case _:
                raise TypeError(f"Cannot encode {val} of type {type(val)}")

    for pair in dat:
        write_pair(pair)
        output.write('\n')


def to_dat(value: DatFile) -> str:
    output = StringIO()
    encode_dat(value, output)
    return output.getvalue()

DAT_OBJECT_TYPES = (
    Game,
    Rom,
)

def load_dat(dat: str | bytes | Path | TextIO | BinaryIO) -> DatRecord:
    match dat:
        case str():
            dat_content = dat
        case bytes():
            dat_content = dat.decode('utf-8')
        case Path() as p:
            with p.open('r', encoding='utf-8') as dat_file:
                dat_content = dat_file.read()
        case TextIO() as f:
            dat_content = f.read()
        case BinaryIO() as f:
            dat_content = f.read().decode('utf-8')
        case _:
            raise TypeError(f"Expect a str, bytes, Path, TextIO, or BinaryIO, got {type(dat)}")

    match_result = dat_parser.match(dat_content, flags=pe.MEMOIZE | pe.OPTIMIZE | pe.STRICT | pe.INLINE)
    if match_result is None:
        raise ValueError("Failed to parse DAT string")

    result = match_result.value()
    assert result is not None
    return result

class LoadedDat(NamedTuple):
    playlist: PlaylistTitle
    path: Path
    clrmamepro: ClrMamePro
    games: Sequence[Game]

async def load_dats(playlist: Playlist, dat_dirs: Iterable[Path]) -> tuple[PlaylistTitle, tuple[Game, ...]]:
    dat_files: list[LoadedDat] = []
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

    if executor:
        loop = asyncio.get_running_loop()
        futures = (loop.run_in_executor(executor, load_dat, p) for p in paths)
        dat_files = [d for d in await asyncio.gather(*futures) if d]
    else:
        dat_files = [d for d in map(load_dat, paths) if d]

    game_fields = dataclasses.fields(Game)
    def reduce_game(merged: dict[str, bool | str | int | Sequence[Rom]], game: Game) -> dict[str, Any]:
        # Merge the fields of `game` into `merged`, without overwriting existing values
        for field in game_fields:
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

# I wanted to have a tuple[ClrMamePro, *tuple[Game, ...]],
# but Pydantic doesn't read the Game correctly in that case.
GameTupleAdapter = TypeAdapter(tuple[Game, ...])

class CheckSubCommand(BaseModel):
    """Check DAT files for valid syntax."""

    dat_paths: CliPositionalArg[list[FilePath | DirectoryPath]] = Field(
        default=(Path(__file__).parent.parent / 'dat', Path(__file__).parent.parent / 'metadat'),
        description="Paths to directories containing DAT files to check.",
        validate_default=True,
    )

    check_models: bool = Field(
        default=False,
        description="If set, check that all DATs can be validated as Pydantic models. Otherwise just check syntax.",
        validation_alias=AliasChoices('m', 'models'),
    )

    verbose: bool = Field(
        default=False,
        description="Enable verbose output.",
        validation_alias=AliasChoices('v', 'verbose'),
    )

    @staticmethod
    async def load_dat_async(dat_path: Path, check_models: bool, verbose: bool) -> bool:
        # Returning a bool to indicate success
        # so we don't have to pickle a whole DAT file
        # when we're just checking for validity
        try:
            async with aiofiles.open(dat_path, 'r', encoding='utf-8') as dat_file:
                dat_content = await dat_file.read()

            match_result = dat_parser.match(dat_content, flags=pe.MEMOIZE | pe.OPTIMIZE | pe.STRICT | pe.INLINE)
            if not match_result:
                print(f"Invalid DAT file: {dat_path}", file=sys.stderr)
                return False

            parsed_value = match_result.value()
            if not check_models:
                if verbose and parsed_value is not None:
                    print(f"Valid DAT file: {dat_path}")
                return parsed_value is not None

            if not isinstance(parsed_value, Sequence) or len(parsed_value) == 0:
                print(f"Invalid DAT file structure: {dat_path}", file=sys.stderr)
                return False

            clrmamepro = ClrMamePro.model_validate(parsed_value[0], context="dat")
            games = GameTupleAdapter.validate_python(parsed_value[1:], context="dat")

            # Validate as Pydantic models
            if verbose:
                print(f"Valid DAT file with models: {dat_path} ({len(games)} games)")

            return True

        except Exception as e:
            print(f"Failed to load DAT file {dat_path}: {e}", file=sys.stderr)
            return False

    async def cli_cmd(self) -> None:
        files, dirs = partition(Path.is_dir, self.dat_paths)
        child_files = filter(Path.is_file, chain.from_iterable(p.rglob('*.dat') for p in dirs))
        paths = {p for p in chain(files, child_files) if 'xml' not in p.name.lower()}
        async with Pool() as pool:
            jobs = zip(paths, repeat(self.check_models), repeat(self.verbose))
            await pool.starmap(CheckSubCommand.load_dat_async, tuple(jobs))
            # TODO: Return non-zero exit code if any files failed

class ToJsonSubCommand(BaseModel):
    """
    Convert a DAT file to equivalent JSON.
    """

    infile: CliPositionalArg[Path | None] = Field(
        default=None,
        description="Path to the input DAT file, or stdin if not provided."
    )

    async def cli_cmd(self) -> None:
        import json

        if self.infile:
            async with aiofiles.open(self.infile, 'rb') as infile:
                dat_contents = await infile.read()
        else:
            dat_contents = await aiofiles.stdin_bytes.read()

        dat = load_dat(dat_contents)
        json.dump(dat, sys.stdout, indent=2, ensure_ascii=False)

class FromJsonSubCommand(BaseModel):
    infile: CliPositionalArg[Path | None] = Field(
        default=None,
        description="Path to the input JSON file, or stdin if not provided."
    )

    encoding: str = Field(
        default='utf-8',
        description="Encoding of the input JSON file."
    )

    async def cli_cmd(self) -> None:
        if self.infile:
            async with aiofiles.open(self.infile, 'r', encoding=self.encoding) as infile:
                json_contents = await infile.read()
        else:
            json_contents = await aiofiles.stdin.read()

        dat = from_json(json_contents)
        encode_dat(dat, sys.stdout)

class DatCommand(BaseSettings):
    tojson: CliSubCommand[ToJsonSubCommand]
    fromjson: CliSubCommand[FromJsonSubCommand]
    check: CliSubCommand[CheckSubCommand]

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


__all__ = (
    "ClrMamePro",
    "Game",
    "load_dat",
    "Rom",
    "DAT_OBJECT_TYPES",
)

if __name__ == "__main__":
    CliApp.run(DatCommand)