#!/usr/bin/env python3

import sys

from abc import ABC
from collections.abc import Sequence
from io import StringIO
from itertools import chain, repeat
from os import PathLike
from pathlib import Path
from typing import IO, Annotated, Any, BinaryIO, ClassVar, Literal, LiteralString, NamedTuple, Self, TextIO, TypedDict

import aiofiles
import pe

from aiomultiprocess import Pool
from frozendict import frozendict
from more_itertools import map_reduce, partition
from pe.actions import Pack
from pe.operators import Class, Star
from pydantic import AfterValidator, AliasChoices, BaseModel, ByteSize, DirectoryPath, Field, FilePath, GetPydanticSchema, ModelWrapValidatorHandler, OnErrorOmit, RootModel, TypeAdapter, ValidationInfo, computed_field, model_validator
from pydantic_core import from_json, core_schema
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, column
from sqlalchemy.dialects.sqlite import JSON

from igdb import PlaylistTitle
from utils import Crc, DatabaseModel, EmptyStringToNone, FrozenDict, Md5, OnlyFirst, Relationship, RowId, RowIdColumn, Sha1

type DatValidationMode = Literal['dat'] | None
type DatPair = tuple[str, DatValue]
type DatRecord = tuple[DatPair, ...]
type DatValue = str | DatRecord
type DatTopLevelRecord = tuple[str, DatRecord]
type DatFile = tuple[DatTopLevelRecord, ...]

class DatModel(DatabaseModel, ABC, frozen=True, extra="allow", str_strip_whitespace=True, validate_by_name=True):
    __dattype__: ClassVar[LiteralString]

    @computed_field(
        return_type=Annotated[FrozenDict[str, Any] | None, Column(JSON(none_as_null=True), nullable=True)],
        repr=False,
    )
    @property
    def extra(self) -> frozendict[str, Any] | None:
        """
        A computed field that gathers any extra fields not defined in the model
        into a dictionary. This allows us to preserve unrecognized fields from the DAT file
        without losing them during validation.
        """
        return frozendict(self.model_extra) if self.model_extra else None

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

def _split_for_retroarch_validator(value: str) -> tuple[str, ...]:
    """
    Split the string by commas, pipes, or slashes, matching RetroArch's logic.

    Rules:
    - Strip whitespace before and after each segment
    - Don't treat corporate suffixes (e.g. ", Inc." or ", Ltd." or ", The") as separators

    Matches logic from:
    https://github.com/libretro/RetroArch/blob/master/menu/menu_explore.c#L272
    """
    import re

    if not value or not value.strip():
        return ()

    # Split on delimiters that are NOT preceded by a corporate suffix.
    # The pattern uses a negative lookbehind to exclude commas that follow suffixes.
    # For slashes and pipes, we always split (they're not used with company names).
    pattern = r'\s*(?:(?<=\.)(?=\s*[,/|])|(?<!\s(?:Inc|Ltd|The)\.?))\s*[,/|]\s*'

    # Split and filter out empty strings
    segments = [seg.strip() for seg in re.split(pattern, value, re.IGNORECASE) if seg.strip()]

    return tuple(segments)

RetroArchStringTuple = Annotated[tuple[str, ...], AfterValidator(_split_for_retroarch_validator)]

class ClrMamePro(DatModel, frozen=True):
    __tablename__ = "DatClrMamePro"
    __dattype__ = "clrmamepro"

    rowid: RowIdColumn
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

class RomId(TypedDict, total=False):
    crc: Crc | None
    serial: str | None
    md5: Md5 | None
    sha1: Sha1 | None

class Rom(DatModel, frozen=True):
    __tablename__ = "DatRom"
    __dattype__ = "rom"
    __tableargs__ = (
        CheckConstraint("crc NOT NULL OR serial NOT NULL", name="chk_retroarch_id"),
        Index("idx_rom_ids", "crc", "serial", "md5", "sha1", unique=True)
    )

    rowid: RowIdColumn
    name: Annotated[str | None, Column(index=True), Field(alias="image")] = None
    crc: Annotated[Crc | None, Column(
        CheckConstraint("crc IS NULL OR length(crc) = 8"),
        unique=True,
        index=True,
        sqlite_where=column("crc").is_not(None)
    )] = None
    serial: Annotated[str | None, Column(index=True, sqlite_where=column("serial").is_not(None))] = None
    md5: Annotated[Md5 | None, Column(CheckConstraint("md5 IS NULL OR length(md5) = 32"), unique=True, index=True)] = None
    sha1: Annotated[Sha1 | None, Column(CheckConstraint("sha1 IS NULL OR length(sha1) = 40"), unique=True, index=True), Field(alias="sha1sum")] = None
    size: ByteSize | None = None

class Game(DatModel, frozen=True):
    """
    A parsed and unmarshalled game record from a DAT file.

    Unrecognized fields are ignored.
    You can read or write a new field by adding it to this class.

    At least one of `name`, `description`, `comment`, or `id` should be present.
    """
    __tablename__ = "DatGame"
    __dattype__ = "game"
    __tableconstraints__ = (
        CheckConstraint("name IS NOT NULL OR description IS NOT NULL OR comment IS NOT NULL OR id IS NOT NULL", name="chk_game_at_least_one_identifier"),
    )

    rowid: RowIdColumn
    analog: bool | None = None
    comment: OnlyFirst[str] | None = None
    description: str | None = None
    developer: str | None = None
    """May include multiple developers separated by commas, slashes, or pipes"""

    franchise: str | None = None

    genre: str | None = None
    """May include multiple genres separated by commas, slashes, or pipes."""

    id: str | None = None

    manufacturer: str | None = None
    """May include multiple media types separated by commas, slashes, or pipes."""

    name: str | None = None
    publisher: str | None = None
    """May include multiple publishers separated by commas, slashes, or pipes."""

    region: str | None = None
    releaseday: int | None = None
    releasemonth: int | None = None
    releaseyear: EmptyStringToNone[int] = None
    rumble: bool | None = None
    tags: str | None = None
    users: int | None = None

    # Declared last so that it appears last in the generated DATs;
    # not semantically important, but easier to read.
    roms: Annotated[tuple[Rom, ...], Field(validation_alias="rom"), Relationship(
        self_columns={"rowid": Column("game", ForeignKey("DatGame.rowid"), primary_key=True)},
        related_columns=({
            # The field names don't map 1:1 with column names,
            # so we specify the field names explicitly as keys
            "crc": Column(
                "crc",
                ForeignKey("DatRom.crc"),
                CheckConstraint("crc IS NULL OR length(crc) = 8"),
                nullable=True,
                unique=True,
                index=True,
                primary_key=True,
                sqlite_where=column('crc').is_not(None)
            ),
            "serial": Column(
                "serial",
                ForeignKey("DatRom.serial"),
                nullable=True,
                index=True,
                primary_key=True,
                sqlite_where=column('serial').is_not(None)
            ),
            "md5": Column(
                "md5",
                ForeignKey("DatRom.md5"),
                CheckConstraint("md5 IS NULL OR length(md5) = 32"),
                nullable=True,
                unique=True,
                index=True,
                primary_key=True,
                sqlite_where=column('md5').is_not(None)
            ),
            "sha1": Column(
                "sha1",
                ForeignKey("DatRom.sha1"),
                CheckConstraint("sha1 IS NULL OR length(sha1) = 40"),
                nullable=True,
                unique=True,
                index=True,
                primary_key=True,
                sqlite_where=column('sha1').is_not(None)
            ),
        }),
        tableargs=(
            CheckConstraint("crc NOT NULL OR serial NOT NULL", name="chk_game_rom_mapping_retroarch_id"),
            Index("idx_game_rom_mapping_rom_ids", "crc", "serial", "md5", "sha1"),
        ),
        tablekwargs=None,
        # Unlike most other relationship tables in this project,
        # this one isn't WITHOUT ROWID because some columns of the primary key are nullable.
    )] = ()

    @computed_field(return_type=Annotated[tuple[RomId, ...], Column(JSON, nullable=False)])
    @property
    def romids(self):
        """Rom IDs"""
        return tuple(RomId(**r.model_dump(include={"crc", "serial", "md5", "sha1"})) for r in self.roms)


class PlaylistGameMapping(DatabaseModel, frozen=True):
    __tablename__ = "DatPlaylistGameMapping"
    __tablekwargs__ = {"sqlite_with_rowid": False}

    playlist: Annotated[PlaylistTitle, Column(primary_key=True, index=True)]
    game: Annotated[RowId, Column(ForeignKey("DatGame.rowid"), primary_key=True, index=True)]

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
                    handler.generate_schema(OnErrorOmit[Game]),        # repeated item
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
    ClrMamePro,
    PlaylistGameMapping,
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
    "load_dat",
    "Rom",
    "DAT_OBJECT_TYPES",
    "ParsedDatFile",
)

if __name__ == "__main__":
    CliApp.run(DatCommand)