#!/usr/bin/env python3

import asyncio
import logging
import sys
import time
import tomllib

from abc import ABC
from collections.abc import Collection, Sequence
from datetime import timedelta
from io import StringIO
from itertools import chain, repeat, product
from os import PathLike
from pathlib import Path
from typing import IO, Annotated, Any, BinaryIO, ClassVar, Literal, LiteralString, NamedTuple, Self, TextIO, TypedDict

import aiofiles
import aioitertools.builtins as aiobuiltins
import aiofiles.ospath as aiopath
import pe

from aioitertools.asyncio import as_completed
from aiomultiprocess import Pool
from frozendict import frozendict
from more_itertools import map_reduce, partition, prepend
from pe.actions import Pack
from pe.operators import Class, Star
from pydantic import AfterValidator, AliasChoices, BaseModel, ByteSize, DirectoryPath, Field, FilePath, GetPydanticSchema, ModelWrapValidatorHandler, OnErrorOmit, RootModel, TypeAdapter, ValidationInfo, computed_field, model_validator
from pydantic_core import from_json, core_schema
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, MetaData, column, text
from sqlalchemy.dialects.sqlite import JSON, insert
from sqlalchemy.sql.functions import coalesce

from igdb import Playlist, PlaylistConfig, PlaylistTitle
from utils import AsyncEngine, Crc, DatabaseModel, EmptyStringToNone, FrozenDict, IndexArgs, Md5, OnlyFirst, PlaylistArgs, PoolArgs, Relationship, RowId, RowIdColumn, Sha1, VerboseArgs, create_db

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
        CheckConstraint("crc NOT NULL OR serial NOT NULL", name="ix_DatRom_has_retroarch_id"),
        Index("ix_DatRom", "crc", "serial", "md5", "sha1", unique=True),
        Index("ix_DatRom_name_where_not_null", "name", sqlite_where=column("name").is_not(None)),
        Index("ix_DatRom_crc_where_not_null", "crc", sqlite_where=column("crc").is_not(None), unique=True),
        Index("ix_DatRom_serial_where_not_null", "serial", sqlite_where=column("serial").is_not(None)),
    )

    rowid: RowIdColumn
    name: Annotated[str | None, Field(alias="image")] = None
    crc: Annotated[Crc | None, Column(CheckConstraint("crc IS NULL OR length(crc) = 8"), unique=True)] = None
    serial: str | None = None
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
                primary_key=True,
            ),
            "serial": Column("serial", ForeignKey("DatRom.serial"), nullable=True, primary_key=True),
            "md5": Column(
                "md5",
                ForeignKey("DatRom.md5"),
                CheckConstraint("md5 IS NULL OR length(md5) = 32"),
                nullable=True,
                unique=True,
                index=True,
                primary_key=True,
            ),
            "sha1": Column(
                "sha1",
                ForeignKey("DatRom.sha1"),
                CheckConstraint("sha1 IS NULL OR length(sha1) = 40"),
                nullable=True,
                unique=True,
                index=True,
                primary_key=True,
            ),
        }),
        tableargs=(
            CheckConstraint("crc NOT NULL OR serial NOT NULL", name="chk_game_rom_mapping_retroarch_id"),
            Index("ix_DatGame_roms", "crc", "serial", "md5", "sha1"),
            Index("ix_DatGame_crc_where_not_null", "crc", sqlite_where=column("crc").is_not(None), unique=True),
            Index("ix_DatGame_serial_where_not_null", "serial", sqlite_where=column("serial").is_not(None)),
            Index("ix_DatGame_md5_where_not_null", "md5", sqlite_where=column("md5").is_not(None), unique=True),
            Index("ix_DatGame_sha1_where_not_null", "sha1", sqlite_where=column("sha1").is_not(None), unique=True)
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

    @property
    def clrmamepro(self) -> ClrMamePro:
        """The ClrMamePro record of this DAT file, which contains metadata about the DAT."""
        return self.root[0]

    @property
    def games(self) -> tuple[Game, ...]:
        """The Game records of this DAT file, which contain the actual game data."""
        return self.root[1:] if len(self.root) > 1 else ()

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
    async def from_dat_file_async_or_error(cls, dat: PathLike) -> Self | Exception:
        try:
            return await cls.from_dat_file_async(dat)
        except Exception as e:
            return e

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

class CheckSubCommand(BaseModel, VerboseArgs):
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

PARENT_DIR = Path(__file__).parent.parent

class LoadJobResult(NamedTuple):
    playlist: Playlist
    path: Path
    datfile: ParsedDatFile


_dats_index_log = logging.getLogger('dats.index')


async def _insert_dat_file(db: AsyncEngine, metadata: MetaData, db_lock: asyncio.Lock, dat: LoadJobResult) -> None:
    """Insert a parsed DAT file's contents into the database."""
    log = _dats_index_log
    clrmamepro = dat.datfile.clrmamepro
    games = dat.datfile.games
    roms = tuple(chain.from_iterable(g.roms for g in games))

    async with db_lock:
        async with db.begin() as tx:
            log.debug("Inserting %d games", len(games))

            game_table = metadata.tables[Game.__tablename__]
            game_columns = game_table.columns
            game_cursor = await tx.execute(
                insert(game_table).returning(game_columns.rowid, game_columns.romids),
                [g.as_row for g in games]
            )
            inserted_game_rows = game_cursor.all()
            log.info("Inserted %d games", len(games))

            game_playlist_mappings = tuple(PlaylistGameMapping(playlist=dat.playlist.title, game=row.rowid) for row in inserted_game_rows)
            if game_playlist_mappings:
                log.debug("Inserting %d game-playlist mappings", len(game_playlist_mappings))
                await tx.execute(
                    insert(metadata.tables[PlaylistGameMapping.__tablename__]).on_conflict_do_nothing(),
                    [m.as_row for m in game_playlist_mappings]
                )
                log.info("Inserted %d game-playlist mappings", len(game_playlist_mappings))
            else:
                log.info("No game-playlist mappings to insert")

            if roms:
                log.debug("Inserting %d ROMs", len(tuple(roms)))
                # The same ROM is often represented in multiple DAT files,
                # so instead of discarding duplicates we merge them together;
                # NULL fields in the existing record are filled in
                # with non-NULL values from the new record.
                rom_table = metadata.tables[Rom.__tablename__]
                insert_roms = insert(rom_table)
                update_set = {
                    "crc": coalesce(rom_table.columns.crc, insert_roms.excluded.crc),
                    "md5": coalesce(rom_table.columns.md5, insert_roms.excluded.md5),
                    "name": coalesce(rom_table.columns.name, insert_roms.excluded.name),
                    "serial": coalesce(rom_table.columns.serial, insert_roms.excluded.serial),
                    # TODO: Insert the excluded sha1 if and only if the existing one is null and the sha1 is valid (40 hex characters)
                    "sha1": coalesce(
                        rom_table.columns.sha1,
                        #text("CASE WHEN excluded.sha1 IS NOT NULL AND length(excluded.sha1) = 40 THEN excluded.sha1 END")
                        insert_roms.excluded.sha1
                    ),
                    "size": coalesce(rom_table.columns.size, insert_roms.excluded.size),
                }
                await tx.execute(
                    insert_roms.on_conflict_do_update(set_=update_set),
                    [r.as_row for r in roms]
                )
                log.info("Inserted %d ROMs", len(roms))

            game_rom_mappings = []
            for game in inserted_game_rows:
                rowid: RowId = game.rowid
                romids: list[RomId] = game.romids
                assert isinstance(rowid, int), f"Expected rowid to be an int, got {type(rowid)}"
                assert isinstance(romids, list), f"Expected romids to be a list, got {type(romids)}"

                game_rom_mappings.extend({"game": rowid, **r} for r in romids)

            if game_rom_mappings:
                log.debug("Inserting %d game-ROM mappings", len(game_rom_mappings))
                mapping_table = metadata.tables[f"{Game.__tablename__}_roms"]
                insert_mapping = insert(mapping_table)
                rom_update_set = {
                    "crc": coalesce(mapping_table.columns.crc, insert_mapping.excluded.crc),
                    "md5": coalesce(mapping_table.columns.md5, insert_mapping.excluded.md5),
                    "serial": coalesce(mapping_table.columns.serial, insert_mapping.excluded.serial),
                    "sha1": coalesce(mapping_table.columns.sha1, insert_mapping.excluded.sha1),
                }
                await tx.execute(
                    insert_mapping.on_conflict_do_update(set_=rom_update_set),
                    game_rom_mappings
                )
                log.info("Inserted %d game-ROM mappings", len(game_rom_mappings))

            await tx.commit()
            # Commit the session to persist all added objects

    log.info("Inserted %d games into database", len(games))


async def index_dats(
    *,
    db: AsyncEngine,
    metadata: MetaData,
    db_lock: asyncio.Lock,
    playlists: Collection[Playlist],
    dat_dirs: tuple[DirectoryPath, ...],
    pool: Pool,
) -> None:
    """Load and index DAT files into the database."""
    log = _dats_index_log

    # Recursively find all subdirectories of the requested DAT directories
    nested_dat_paths = chain.from_iterable(p.rglob("*") for p in dat_dirs)
    dat_subdirs = await aiobuiltins.tuple(p for p in nested_dat_paths if await aiopath.isdir(p))
    all_dat_dirs = tuple(chain(dat_dirs, dat_subdirs))

    async def get_dat_paths(playlist: Playlist) -> tuple[Path, ...]:
        # Use the name of the playlist and the alt names to find existing DAT files
        dat_names = prepend(str(playlist.title), playlist.alts)

        # Check for playlists of these names in all requested DAT directories
        dat_paths = (d / f'{n}.dat' for d, n in product(all_dat_dirs, dat_names))

        # HACK: Some XML files have a `.dat` extension, filter them out
        dat_paths = filter(lambda p: 'xml' not in p.name.lower(), dat_paths)
        return await aiobuiltins.tuple(p for p in dat_paths if await aiopath.exists(p))

    dat_paths = {p: await get_dat_paths(p) for p in playlists}

    log.debug("Loading games from %d playlists", len(playlists))

    async def job(playlist: Playlist, path: Path) -> LoadJobResult:
        log.debug("Loading")
        dat = await pool.apply(ParsedDatFile.from_dat_file_async, args=(path,))
        log.info("Loaded with %d games", len(dat.root) - 1)
        return LoadJobResult(playlist, path, dat)

    async with asyncio.TaskGroup() as group:
        jobs = (group.create_task(job(playlist, path), name=f"Load: {path}") for playlist in playlists for path in dat_paths[playlist])
        async for j in as_completed(jobs):
            if len(j.datfile.root) > 1:
                group.create_task(
                    _insert_dat_file(db, metadata, db_lock, j),
                    name=f"Insert: {j.path}"
                )
            else:
                log.warning("DAT file %s has no games, skipping database insertion", j.path)

    log.info("Finished inserting data")


class IndexSubCommand(BaseModel, VerboseArgs, PlaylistArgs, IndexArgs, PoolArgs):
    dat_dirs: tuple[DirectoryPath, ...] = Field(
        default=(PARENT_DIR / 'dat', PARENT_DIR / 'metadat',),
        description="Paths to the directories containing existing DAT files to scan for games to process.",
        validation_alias=AliasChoices('d', 'dat'),
        validate_default=True,
    )

    output: Path = Field(
        default=PARENT_DIR / 'tmp' / 'dats.db',
        description="Path to the output SQLite database file.",
        validation_alias=AliasChoices('o', 'output'),
        validate_default=True,
    )

    _db_lock = asyncio.Lock()
    _log = logging.getLogger('dats.index')

    async def cli_cmd(self):
        start = time.perf_counter()

        log_handler = logging.StreamHandler()
        log_handler.setFormatter(logging.Formatter('[%(asctime)s][%(name)s][%(taskName)s] %(message)s'))
        sqlalchemy_engine_log = logging.getLogger('sqlalchemy.engine.Engine')
        sqlalchemy_engine_log.addHandler(log_handler)

        self._log.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        self._log.addHandler(log_handler)
        if self.verbose:
            # Log the SQL table creation statements being executed,
            # but we'll lower the level later during data insertion
            # so we don't get overwhelmed with output.
            sqlalchemy_engine_log.setLevel(logging.INFO)

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
        db, metadata = await create_db(self.output, DAT_OBJECT_TYPES)
        sqlalchemy_engine_log.setLevel(logging.WARNING)


        async with self.create_pool() as pool:
            await index_dats(db=db, metadata=metadata, db_lock=self._db_lock, playlists=playlists, dat_dirs=self.dat_dirs, pool=pool)

        async with db.connect() as connection:
            # Run the SQLite optimizer to improve performance on all tables (0x10000),
            # but don't take too long (0x00010)
            await connection.execute(text("PRAGMA optimize = 0x10012"))

        # Close the engine
        await db.dispose()

        end = time.perf_counter()
        elapsed = timedelta(seconds=end - start)
        self._log.info(f"Elapsed time: %s", elapsed)


class DatCommand(BaseSettings):
    tojson: CliSubCommand[ToJsonSubCommand]
    fromjson: CliSubCommand[FromJsonSubCommand]
    check: CliSubCommand[CheckSubCommand]
    index: CliSubCommand[IndexSubCommand]

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
    "index_dats",
    "load_dat",
    "Rom",
    "DAT_OBJECT_TYPES",
    "ParsedDatFile",
)

if __name__ == "__main__":
    CliApp.run(DatCommand)