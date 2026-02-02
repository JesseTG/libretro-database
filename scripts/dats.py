#!/usr/bin/env python3

import dataclasses
import sys

from abc import ABC
from collections.abc import Sequence
from io import StringIO
from itertools import chain, repeat
from os import PathLike
from pathlib import Path
from typing import IO, Annotated, Any, BinaryIO, ClassVar, Literal, LiteralString, NamedTuple, NewType, Self, TextIO, overload

import aiofiles
# pe lacks type stubs, so let's silence MyPy's complaints
from more_itertools import map_reduce, partition
import pe  # type: ignore

from aiomultiprocess import Pool
from pe.actions import Pack
from pe.operators import Class, Star
from pydantic import AliasChoices, BaseModel, ByteSize, DirectoryPath, Field, FilePath, ModelWrapValidatorHandler, RootModel, SerializationInfo, SerializerFunctionWrapHandler, TypeAdapter, ValidationError, ValidationInfo, computed_field, model_serializer, model_validator
from pydantic_core import CoreSchema, from_json, core_schema
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict
from sqlalchemy import Column, ForeignKey

from igdb import PlaylistTitle
from utils import DatabaseModel, EmptyStringToNone, Hash, Relationship, WrapInTuple

type DatValidationMode = Literal['dat'] | None
type DatPair = tuple[str, DatValue]
type DatRecord = tuple[DatPair, ...]
type DatValue = str | DatRecord
type DatTopLevelRecord = tuple[str, DatRecord]
type DatFile = tuple[DatTopLevelRecord, ...]

class DatModel(DatabaseModel, ABC, frozen=True):
    __dattype__: ClassVar[LiteralString]

    @classmethod
    def from_dat(cls, value: DatRecord | DatTopLevelRecord) -> Self:
        return cls.model_validate(value, context="dat")

    @model_validator(mode="wrap")
    @classmethod
    def validate_dat(cls, data: Any, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo) -> Self:
        """
        If the validation context is 'dat', construct the object from a DAT record.
        """
        if info.context != 'dat':
            return handler(data)

        match data:
            case str(type) | (str(type), [*_]) if type != cls.__dattype__:
                # A DAT key or keyed DatRecord with an unexpected type
                raise ValueError(f"Expected a DAT type of {cls.__dattype__}, got {type}")
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
                return handler(datdict)
            case _:
                # Handle other cases normally
                return handler(data)


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
    __tablekwargs__ = {
        "sqlite_with_rowid": False,
    }

    crc: Annotated[Crc, Column(unique=True, index=True)] | None = None
    serial: Annotated[str, Column(unique=True, index=True)] | None = None
    image: str | None = None
    name: str | None = None
    size: ByteSize | None = None
    md5: Annotated[Md5, Column(unique=True, index=True)] | None = None
    sha1: Annotated[Sha1, Column(unique=True, index=True)] | None = None

    @computed_field
    @property
    def pk(self) -> Annotated[str, Column(primary_key=True)]:
        if self.crc:
            return self.crc
        if self.serial:
            return self.serial

        raise ValueError("Rom model must have at least one of `crc` or `serial`")

    @override
    def model_post_init(self, _context) -> None:
        self.pk # Force computation of pk to validate presence of identifying fields

GamePrimaryKey = NewType('GamePrimaryKey', str)

class Game(DatModel, frozen=True):
    """
    A parsed and unmarshalled game record from a DAT file.

    Unrecognized fields are ignored.
    You can read or write a new field by adding it to this class.

    At least one of `name`, `description`, `comment`, or `id` should be present.
    """
    __tablename__ = "DatGame"
    __dattype__ = "game"
    __tablekwargs__ = {
        "sqlite_with_rowid": False,
    }

    achievements: int | None = None
    analog: bool | None = None

    bbfc_rating: str | None = None
    category: str | None = None
    """May include multiple categories separated by commas, slashes, or pipes."""

    cero_rating: str | None = None
    code: str | None = None
    comment: WrapInTuple[str] | None = None
    console_exclusive: bool | None = None
    controls: str | None = None
    coop: bool | None = None
    date: str | None = None
    description: str | None = None
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
    id: str | None = None
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
    name: str | None = None
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
    year: EmptyStringToNone[str] = None

    # Declared last so that it appears last in the generated DATs;
    # not semantically important, but easier to read.
    rom: Annotated[tuple[Rom, ...], Relationship(
        self_columns=Column("pk",  ForeignKey("DatGame.pk"), primary_key=True, nullable=False),
        related_columns=({
            # The field names don't map 1:1 with column names,
            # so we specify the field names explicitly as keys
            "pk": Column(
                "rom",
                ForeignKey("DatRom.pk"),
                primary_key=True,
                nullable=False,
            ),
            "crc": Column(
                "crc",
                ForeignKey("DatRom.crc"),
                nullable=True,
            ),
            "serial": Column(
                "serial",
                ForeignKey("DatRom.serial"),
                nullable=True,
            ),
            "md5": Column(
                "md5",
                ForeignKey("DatRom.md5"),
                nullable=True,
            ),
            "sha1": Column(
                "sha1",
                ForeignKey("DatRom.sha1"),
                nullable=True,
            ),
        }),
    )] = ()

    @computed_field
    @property
    def pk(self) -> Annotated[GamePrimaryKey, Column(primary_key=True)]:
        """
        A unique identifier for this game.

        Checks several possible fields in order of preference,
        since DATs use different fields as the "name" of a game.
        """
        if self.id:
            return GamePrimaryKey(self.id)

        if self.name:
            return GamePrimaryKey(self.name)

        if self.comment:
            return GamePrimaryKey(self.comment[0])

        if self.description:
            return GamePrimaryKey(self.description)

        raise ValueError("Game record has neither 'id', 'name', 'comment', nor 'description' field.")

    @override
    def model_post_init(self, _context: Any) -> None:
        self.pk # Force computation of pk to validate presence of identifying fields


class DatPlaylists(DatabaseModel, frozen=True):
    """
    A mapping of playlist titles to DAT file paths.
    Derived from the loaded playlist config,
    but doesn't directly represent a specific file.
    """
    __tablename__ = "DatPlaylists"

    playlist: Annotated[PlaylistTitle, Column(primary_key=True)]
    games: tuple[GamePrimaryKey, ...] = ()


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

class ParsedDatFile(RootModel, frozen=True):
    """
    A RootModel representing a parsed DAT file
    as a tuple starting with a ClrMamePro record followed by zero or more Game records.
    """
    root: Annotated[
        tuple[ClrMamePro, *tuple[Game, ...]],
        GetPydanticSchema(
            lambda tp, handler: core_schema.tuple_schema(
                items_schema=[
                    handler.generate_schema(ClrMamePro),  # first item
                    handler.generate_schema(Game),        # repeated item
                ],
                variadic_item_index=1,  # repeat schema at index 1
                min_length=1,           # must have at least the first item
            )
        )
    ]
    """
    Pydantic doesn't seem to generate schemae for unpacked tuples,
    so we have to define it ourselves.

    See https://github.com/pydantic/pydantic/issues/5952 for the issue,
    and https://stackoverflow.com/a/79877584/1089957 for the workaround's details.

    Once Unpack is supported properly, we can omit the GetPydanticSchema handler above.
    """

    @classmethod
    async def from_dat_file_async(cls, dat: PathLike) -> Self:
        """
        Load and parse a DAT file asynchronously from the given path.
        Raises or returns errors based on the `errors` parameter.
        """
        async with aiofiles.open(dat, 'r', encoding='utf-8') as dat_file:
            dat_content = await dat_file.read()

        raw_dat = load_dat(dat_content)

        return cls.model_validate(raw_dat, context="dat")

    @classmethod
    async def from_dat_file_async_or_error(cls, dat: PathLike) -> Self | tuple[Exception, PathLike]:
        try:
            return await cls.from_dat_file_async(dat)
        except Exception as e:
            return e, dat

DAT_OBJECT_TYPES = (
    Game,
    Rom,
)

def load_dat(dat: str | bytes | PathLike | TextIO | BinaryIO) -> DatRecord:
    """
    Loads a DAT file from disk, memory, or a file-like object and parses it into a DatRecord.
    """
    match dat:
        case str():
            dat_content = dat
        case bytes():
            dat_content = dat.decode('utf-8')
        case PathLike() as p:
            with open(p, 'r', encoding='utf-8') as dat_file:
                dat_content = dat_file.read()
        case TextIO() as f:
            dat_content = f.read()
        case BinaryIO() as f:
            dat_content = f.read().decode('utf-8')
        case _:
            raise TypeError(f"Expect a str, bytes, PathLike, TextIO, or BinaryIO, got {type(dat)}")

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
    async def check_dat_async(dat_path: Path, check_models: bool, verbose: bool) -> Exception | None:
        try:
            async with aiofiles.open(dat_path, 'r', encoding='utf-8') as dat_file:
                dat_content = await dat_file.read()

            match_result = dat_parser.match(dat_content, flags=pe.MEMOIZE | pe.OPTIMIZE | pe.STRICT | pe.INLINE)
            if not match_result:
                raise ValueError("Failed to parse DAT file")

            parsed_value = match_result.value()
            if not check_models:
                if verbose and parsed_value is not None:
                    print(f"Valid DAT file: {dat_path}")
                return None

            if not isinstance(parsed_value, Sequence):
                raise TypeError(f"Unexpected parsed DAT value: {type(parsed_value)}")

            if len(parsed_value) == 0:
                raise ValueError("DAT file is empty")

            parsed_datfile = await ParsedDatFile.from_dat_file_async(dat_path)

            # Validate as Pydantic models
            if verbose:
                print(f"Valid DAT file with models: {dat_path} ({len(parsed_datfile.root) - 1} games)")
        except Exception as e:
            print(f"Failed to load DAT file {dat_path}: {e}", file=sys.stderr)
            return e

    async def cli_cmd(self) -> None:
        files, dirs = partition(Path.is_dir, self.dat_paths)
        child_files = filter(Path.is_file, chain.from_iterable(p.rglob('*.dat') for p in dirs))
        paths = {p for p in chain(files, child_files) if 'xml' not in p.name.lower()}
        async with Pool() as pool:
            jobs = zip(paths, repeat(self.check_models), repeat(self.verbose))
            result = await pool.starmap(CheckSubCommand.check_dat_async, tuple(jobs))
            if errors := tuple(e for e in result if e is not None):
                raise ExceptionGroup(f"{len(errors)} DAT files failed to load or validate.", errors)

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
    "GamePrimaryKey",
    "load_dat",
    "Rom",
    "DAT_OBJECT_TYPES",
    "ParsedDatFile",
)

if __name__ == "__main__":
    CliApp.run(DatCommand)