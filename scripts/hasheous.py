#!/usr/bin/env python3
"""
Dictionary definitions taken from the following Hasheous source files:

- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItemModel.cs
- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/Signatures_Games.cs
- https://github.com/gaseous-project/gaseous-signature-parser/blob/main/gaseous-signature-parser/models/RomSignatureObject.cs
"""

import argparse
import asyncio
import csv
import itertools
import os
import pickle
import sys
import time
import zipfile

from abc import ABC
from collections.abc import Collection, Iterable, Sequence, Mapping
from collections import ChainMap
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from pprint import pprint
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, NewType, Optional, TypeAlias, TypedDict
from warnings import deprecated
from zipfile import ZipFile, ZipInfo


import aiofiles
import aiofiles.os
import backoff
import httpx
import sqlalchemy

from frozendict import frozendict
from more_itertools import first_true
from pydantic import ByteSize, Field, FieldSerializationInfo, HttpUrl, PlainSerializer, PlainValidator, SerializerFunctionWrapHandler, StringConstraints, TypeAdapter, ValidationError, WrapValidator, computed_field, field_serializer
from sqlalchemy.util import is_non_string_iterable

from igdb import PLAYLISTS, IgdbId, Playlist, PlaylistTitle
from sqlite import ColumnDef, DatabaseModel, FrozenDictValidator, InsertInRowContext, RelationshipDef, TupleOf

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"

HasheousId = NewType('HasheousId', int)

type EmptyStringToNone[T] = Annotated[
    T | None,
    WrapValidator(lambda v, h: h(v) if v != "" else None),
    PlainSerializer(lambda v: v if v != "" else None, return_type=(T | None))
]
"""
A type that serializes and validates empty strings as None.
"""

class HasheousObject(DatabaseModel, ABC, frozen=True):
    pass

class SignatureDataObject(HasheousObject, frozen=True):
    __tablename__: ClassVar[str] = "HasheousSignatureDataObject"
    SignatureId: Annotated[int, ColumnDef(primary_key=True)]
    Name: EmptyStringToNone[str] = None
    Year: EmptyStringToNone[str] = None
    Platform: EmptyStringToNone[str] = None
    SourceId: Annotated[int | None, ColumnDef(index=True), WrapValidator(lambda v, h: h(v) if v != "" else None)] = None
    Publisher: EmptyStringToNone[str] = None
    MetadataSource: EmptyStringToNone[str] = None

MappingStatus: TypeAlias = Literal["NotMapped", "Mapped", "MappedWithErrors"]
Hash = Annotated[str, StringConstraints(to_lower=True)]
ImageId = Annotated[str, StringConstraints(to_upper=True)]

MatchMethodType: TypeAlias = Literal[
    "NoMatch",
    "Automatic",
    "Manual",
    "AutomaticTooManyMatches",
    "ManualByAdmin",
    "Voted",
]

MetadataSource: TypeAlias = Literal[
    "None",
    "IGDB",
    "TheGamesDb",
    "RetroAchievements",
    "GiantBomb",
    "Steam",
    "GOG",
    "EpicGameStore",
    "Wikipedia",
    "SteamGridDb",
]

@dataclass(frozen=True)
class MetadataItem:
    Id: EmptyStringToNone[str]
    ImmutableId: EmptyStringToNone[str]
    Status: MappingStatus
    MatchMethod: MatchMethodType
    Source: MetadataSource
    Link: Annotated[HttpUrl | None, WrapValidator(lambda v, h: h(v) if v else None), PlainSerializer(lambda v: v or None, str | None)]
    NextSearch: datetime
    WinningVoteCount: int
    TotalVoteCount: int
    WinningVotePercent: int

AttributeType: TypeAlias = Literal[
    "LongString",
    "ShortString",
    "DateTime",
    "ImageId",
    "ImageAttribution",
    "Link",
    "Boolean",
    "ObjectRelationship",
    "EmbeddedList",
]
"""
Values taken from https://tinyurl.com/yc7baymp
"""


AttributeName: TypeAlias = Literal[
    "Description", # LongString
    "Manufacturer", # ObjectRelationship (Company)
    "Publisher", # ObjectRelationship (Company)
    "Logo",
    "Platform", # ObjectRelationship (Platform)
    "Year",
    "Country", # ShortString
    "Language", # ShortString
    "ROMs", # EmbeddedList (RomItem)
    "VIMMManualId",
    "LogoAttribution",
    "VIMMPlatformName", # ShortString
    "HomePage",
    "IssueTracker",
    "Screenshot1",
    "Screenshot2",
    "Screenshot3",
    "Screenshot4",
    "Wikipedia",
    "Public",
    "DumpFile",
    "IssueTracker",
    "Tags",
]

DataObjectType: TypeAlias = Literal["None", "Company", "Platform", "Game", "ROM", "App"]
RomTypeName: TypeAlias = Literal["Unknown", "Disc", "Disk", "File", "Part", "Tape", "Side"]
SignatureSourceType: TypeAlias = Literal[
    "None",
    "TOSEC",
    "MAMEArcade",
    "MAMEMess",
    "MAMERedump",
    "NoIntro",
    "NoIntros",
    "Redump",
    "WHDLoad",
    "RetroAchievements",
    "FBNeo",
    "PureDOSDAT",
    "Pleasuredome",
    "Generic",
]

class MediaType(TypedDict, total=False):
    MediaType: RomTypeName
    Media: str
    Number: int
    Count: int
    Side: str

class RomItem(HasheousObject, frozen=True):
    """
    Structure taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Schema/hasheous-1000.sql
    (specifically the Signatures_Roms table)
    """
    __tablename__: ClassVar[str] = "HasheousRomItem"
    Id: Annotated[int, ColumnDef(primary_key=True)]
    Name: EmptyStringToNone[str]
    Attributes: Annotated[Mapping[str, str], ColumnDef(type=sqlalchemy.JSON), FrozenDictValidator]
    RomType: Annotated[str, ColumnDef(index=True)]
    Size: ByteSize
    Crc: Annotated[EmptyStringToNone[Hash], ColumnDef(index=True)]
    Md5: Annotated[EmptyStringToNone[Hash], ColumnDef(index=True)]
    Sha1: Annotated[EmptyStringToNone[Hash], ColumnDef(index=True)]
    Sha256: Annotated[EmptyStringToNone[Hash], ColumnDef(index=True)]
    Status: EmptyStringToNone[str]

    # TODO: Represent Country with computed columns
    Country: Annotated[Mapping[str, str], ColumnDef(type=sqlalchemy.JSON), FrozenDictValidator]

    # TODO: Represent Language with computed columns
    Language: Annotated[Mapping[str, str], ColumnDef(type=sqlalchemy.JSON), FrozenDictValidator]
    DevelopmentStatus: EmptyStringToNone[str]
    RomTypeMedia: EmptyStringToNone[str]

    # TODO: Represent MediaDetail with computed columns
    MediaDetail: Annotated[MediaType, ColumnDef(type=sqlalchemy.JSON), FrozenDictValidator]
    MediaLabel: EmptyStringToNone[str]
    SignatureSource: EmptyStringToNone[str]

RomItemTupleAdapter = TypeAdapter(tuple[RomItem, ...])
def coerce_attribute(value: Any) -> "str | tuple[RomItem, ...] | DataObject | Mapping":
    match value:
        case {} if not value:
            return frozendict({})
        case {**mapping}:
            return DataObject.model_validate(mapping)
        case [*items]:
            return RomItemTupleAdapter.validate_python(items)
        case DataObject() as dobj:
            return dobj
        case str() as s:
            return s
        case _:
            raise TypeError(f"Cannot coerce value of type {type(value).__name__} to valid Attribute Value type")

@dataclass(frozen=True)
class Attribute:
    # I wanted to use pydantic.dataclass,
    # but for some reason using frozen=True
    # causes the attributes to not be recognized by pyright
    attributeType: str
    '''Not a typo, the API serializes it this way'''

    attributeName: str
    '''Not a typo, the API serializes it this way'''

    attributeRelationType: str
    '''Not a typo, the API serializes it this way'''

    Value: Annotated["str | tuple[RomItem, ...] | DataObject | Mapping", PlainValidator(coerce_attribute)]
    Id: Optional[int] = None


class DataObject(DatabaseModel, frozen=True):
    """
    Type info for attributes taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Classes/DataObjects.cs
    """
    __tablename__: ClassVar[str] = "HasheousDataObject"

    Id: Annotated[HasheousId, ColumnDef(type=sqlalchemy.Integer, primary_key=True)]
    Name: str
    ObjectType: DataObjectType
    SignatureDataObjects: Annotated[TupleOf[SignatureDataObject], RelationshipDef("HasheousSignatureDataObject.SignatureId")]
    Metadata: Annotated[TupleOf[MetadataItem], Field(exclude=True)]
    Attributes: Annotated[TupleOf[Attribute], Field(exclude=True)]
    CreatedDate: datetime
    UpdatedDate: datetime

    @computed_field
    @cached_property
    def manufacturer(self) -> "DataObject | None":
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "Manufacturer")
        return attribute.Value if attribute and isinstance(attribute.Value, DataObject) else None

    @computed_field
    @cached_property
    def publisher(self) -> "DataObject | None":
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "Publisher")
        return attribute.Value if attribute and isinstance(attribute.Value, DataObject) else None

    @computed_field
    @cached_property
    def platform(self) -> "DataObject | None":
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "Platform")
        return attribute.Value if attribute and isinstance(attribute.Value, DataObject) else None

    @computed_field
    @cached_property
    def country(self) -> Annotated[str | None, ColumnDef(index=True)]:
        # TODO: country is really a comma-separated list of countries,
        # so we should probably normalize that into a separate table
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "Country")
        return attribute.Value if attribute and isinstance(attribute.Value, str) else None

    @computed_field
    @cached_property
    def language(self) -> Annotated[str | None, ColumnDef(index=True)]:
        # TODO: language is really a comma-separated string of multiple languages,
        # so we should probably normalize that into a separate table
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "Language")
        return attribute.Value if attribute and isinstance(attribute.Value, str) else None

    @computed_field
    @cached_property
    def roms(self) -> Annotated[TupleOf[RomItem] | None, RelationshipDef("HasheousRomItem.Id")]:
        attribute = first_true(self.Attributes, pred=lambda a: a.attributeName == "ROMs")
        return tuple(attribute.Value) if attribute and is_non_string_iterable(attribute.Value) else None

    @computed_field
    @cached_property
    def igdb_id(self) -> IgdbId | None: #Annotated[IgdbId | None, ColumnDef(type=sqlalchemy.ForeignKey('IgdbGame.id'))]:
        """Returns the IGDB ID mapped to this DataObject, or None if there's no IGDB mapping."""
        igdb_metadata = first_true(self.Metadata, pred=lambda m: m.Source == "IGDB" and m.Status == "Mapped")
        if not igdb_metadata:
            return None
        if not igdb_metadata.ImmutableId:
            return None

        try:
            return IgdbId(int(igdb_metadata.ImmutableId))
        except ValueError:
            return None

    @field_serializer('platform', 'manufacturer', 'publisher', mode='wrap')
    def _serialize_field(self, value: Any, handler: SerializerFunctionWrapHandler, info: FieldSerializationInfo[InsertInRowContext]):
        match (info.context, value):
            case (None, _):
                # If no context is given, serialize the field as usual
                return handler(value)
            case ('row', DataObject()):
                # If serializing for a database row, serialize nested DataObjects as their IDs
                return value.Id
            case (_, _):
                # Otherwise, run the default serializer to handle other types or contexts
                return handler(value)

HASHEOUS_OBJECT_TYPES = (
    DataObject,
    SignatureDataObject,
    RomItem,
)

@deprecated("Use SQLite instead")
class HasheousIndex:
    def __init__(self, games: Iterable[tuple[PlaylistTitle, Iterable[DataObject]]]) -> None:
        playlists_iterators = dict(games)
        playlists = {title: tuple(obj_iter) for title, obj_iter in playlists_iterators.items()}
        self.by_playlist = playlists
        self.by_id: dict[int, DataObject] = {}
        self.by_igdb_id: dict[IgdbId, DataObject] = {}
        self.hasheous_to_igdb: dict[HasheousId, IgdbId] = {}
        self.supports_achievements: set[HasheousId] = set()

        self.by_crc: dict[str, DataObject] = {}
        """
        Mapping of ROM CRC32 hashes to DataObjects.
        CRCs must be all uppercase.
        """

        self.by_md5: dict[str, DataObject] = {}
        self.by_sha1: dict[str, DataObject] = {}
        self.by_serial: dict[str, DataObject] = {}

        self.by_game_id = ChainMap(self.by_crc, self.by_md5, self.by_sha1, self.by_serial)

        for obj in itertools.chain.from_iterable(playlists.values()):
            if obj.ObjectType != 'Game':
                continue

            self.by_id[obj.Id] = obj

            for m in filter(lambda mi: mi.Status == 'Mapped', obj.Metadata):
                self._handle_metadata(obj, m)

            for a in obj.Attributes:
                self._handle_attribute(obj, a)

    def _handle_metadata(self, obj: DataObject, m: MetadataItem) -> None:
        match m.Source:
            case 'IGDB':
                igdb_id = IgdbId(int(m.ImmutableId))
                if igdb_id not in self.by_igdb_id:
                    self.by_igdb_id[igdb_id] = obj
                    self.hasheous_to_igdb[obj.Id] = igdb_id
            case 'RetroAchievements':
                self.supports_achievements.add(obj.Id)

    def _handle_attribute(self, obj: DataObject, a: Attribute) -> None:
        match (a.attributeType, a.attributeName, a.Value):
            case ('EmbeddedList', 'ROMs', roms) if isinstance(roms, Sequence) and not isinstance(roms, str):
                for r in roms:
                    if r.Crc:
                        crc = r.Crc.upper()
                        if crc not in self.by_crc:
                            self.by_crc[crc] = obj

                    if r.Md5:
                        md5 = r.Md5.upper()
                        if md5 not in self.by_md5:
                            self.by_md5[md5] = obj

                    if r.Sha1:
                        sha1 = r.Sha1.upper()
                        if sha1 not in self.by_sha1:
                            self.by_sha1[sha1] = obj

                    if r.Attributes and (serial := r.Attributes.get('serial', None)):
                        serial_upper = serial.upper()
                        if serial_upper not in self.by_serial:
                            self.by_serial[serial_upper] = obj


class MatchRecord(NamedTuple):
    """
    A record of an attempt to match a game listed in one of this repo's DAT files
    with an entry in IGDB and/or Hasheous.
    Intended for output to a CSV file for later analysis.
    """

    name: str
    """
    The name of the game as listed in the DAT file.
    If the game is listed under multiple names,
    the first one found wins.
    """

    crc: Optional[str]
    """
    The CRC32 of the game's ROM, if available.
    """

    md5: Optional[str]
    """
    The MD5 hash of the game's ROM, if available.
    """

    sha1: Optional[str]
    """
    The SHA-1 hash of the game's ROM, if available.
    """

    serial: Optional[str]
    """
    The serial number of the game's ROM, if available.
    """

    hasheous_id: Optional[int]
    """
    The ID number of this game's entry on Hasheous, if one was found.
    """

    hasheous_url: Optional[str]
    """
    The URL of this game's entry on Hasheous, if one was found.
    """

    igdb_id: Optional[int]
    """
    The ID number of this game's entry on IGDB, if one was found.
    """

    igdb_url: Optional[str]
    """
    The URL of this game's entry on IGDB, if one was found.
    """

    igdb_release_id: Optional[int]
    """
    The ID number of this game's release on IGDB for the platform named by igdb_platform_id.
    """

    igdb_platform_id: Optional[int]
    """
    The ID number of the platform on IGDB that this game was released for.
    """

    @property
    def matched(self) -> bool:
        return \
            self.igdb_id is not None and \
            self.hasheous_id is not None and \
            (self.crc is not None or self.serial is not None)


class HasheousZip(NamedTuple):
    name: str
    objects: TupleOf[DataObject]

def parse_zip(path: Path) -> HasheousZip:
    start = time.perf_counter_ns()
    with ZipFile(path, 'r') as zip:
        paths = zip.infolist()
        json_infos = filter(lambda p: p.filename.endswith('.json') and p.filename != 'PlatformMapping.json', paths)
        objects = map(lambda i: DataObject.model_validate_json(zip.read(i)), json_infos)

        result = HasheousZip(path.stem, tuple(objects))

    end = time.perf_counter_ns()
    duration = (end - start) / 1_000_000
    print(f"Parsed {len(result.objects)} DataObjects from {path.name} in {duration:.2f} ms")
    return result
    # Returning the stem makes it easier to aggregate results later

async def load_zip(path: Path) -> HasheousZip:
    async with aiofiles.open(path, "rb") as zip_file:
        with ZipFile(zip_file.raw) as zip:

            def validate(info: ZipInfo):
                byte_data = zip.read(info)
                try:
                    return DataObject.model_validate_json(byte_data)
                except ValidationError as ve:
                    raise

            paths = zip.infolist()
            json_infos = filter(lambda p: p.filename.endswith('.json') and p.filename != 'PlatformMapping.json', paths)
            objects = map(validate, json_infos)

            return HasheousZip(path.stem, tuple(objects))


async def load_index(path: Path) -> HasheousIndex:
    """
    Load a HasheousIndex from the given pickle file.

    :param path: Path to the pickle file containing the HasheousIndex.

    :return: The loaded HasheousIndex.
    """

    async with aiofiles.open(path, "rb") as index_file:
        index = pickle.load(index_file.raw)

        if not isinstance(index, HasheousIndex):
            raise TypeError(f"Expected HasheousIndex in pickle file at {path}; got {type(index).__name__}")

    return index

DEFAULT_CHUNKSIZE = 16

async def load_dataobjects(zip_paths: Iterable[Path] | Path, playlists: Iterable[Playlist], executor: Executor) -> HasheousIndex:
    """
    Create a HasheousIndex from the given metadata directory for the specified playlists.

    :param metadata_dir: Path to the directory containing Hasheous metadata ZIP files.
    :param playlists: An iterable of Playlist objects to load DataObjects for.

    :return: An index of all loaded DataObjects.
    """

    if isinstance(zip_paths, Iterable):
        resolved_zip_paths = {p.resolve() for p in zip_paths if zipfile.is_zipfile(p)}
    else:
        resolved_zip_paths = {p.resolve() for p in zip_paths.rglob('*.zip') if zipfile.is_zipfile(p)}

    zips: dict[str, Sequence[DataObject]] = {}
    # A map of dump filenames (minus .zip) to parsed DataObjects.
    # A Hasheous dump can be referenced by multiple IGDB playlists,
    # so we load the ZIP files and merge the results accordingly.

    loop = asyncio.get_running_loop()
    futures = (loop.run_in_executor(executor, parse_zip, p) for p in resolved_zip_paths)
    zips = dict(await asyncio.gather(*futures))

    playlist_map: dict[PlaylistTitle, Iterable[DataObject]] = {}
    for playlist in playlists:
        dirs = tuple(playlist.hasheous_dirs) + ("Unknown Platform",)
        # Add "Unknown Platform" to the list of dump files to search,
        # since its entries still have CRCs.

        objects = itertools.chain.from_iterable(zips[d] for d in dirs if d in zips)
        playlist_map[playlist.title] = objects

    return HasheousIndex(playlist_map.items())


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

def _on_backoff(details):
    print("Retrying after backoff:", details['target'].__name__, "with args:", details['args'], "and kwargs:", details['kwargs'], file=sys.stderr)

RETRY_CODES = (
    httpx.codes.REQUEST_TIMEOUT,
    httpx.codes.TOO_MANY_REQUESTS,
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.SERVICE_UNAVAILABLE,
    httpx.codes.GATEWAY_TIMEOUT,
)

HASHEOUS_BASE_URL = "https://hasheous.org/api/v1/Dumps/platforms/"

def _giveup(e: Exception):
    print("Exception raised during query:", e, file=sys.stderr)
    if not isinstance(e, httpx.HTTPStatusError):
        # Give up if query_endpoint failed with something besides HTTPStatusError
        return True

    if e.response.status_code in RETRY_CODES:
        # Don't give up on server errors (5xx), we might just be unlucky
        # or rate-limited (429), so we should back off and retry.
        return False

    return e.response.is_error

async def handle_fetch(args: argparse.Namespace) -> None:
    outdir: Path = args.outdir
    dumps = set(args.dumps or itertools.chain.from_iterable(p.hasheous_dirs for p in PLAYLISTS))
    verbose = bool(args.verbose)

    dumps.add("Unknown Platform")
    # "Unknown Platform" entries don't identify a specific platform,
    # but a lot of them do have CRCs that can be useful.

    if verbose:
        print(f"Output directory: {outdir}")
        pprint(dumps)

    await aiofiles.os.makedirs(outdir, exist_ok=True)

    async with asyncio.TaskGroup() as group:
        @backoff.on_exception(backoff.expo, httpx.HTTPStatusError, max_tries=5, giveup=_giveup, on_backoff=_on_backoff)
        async def fetch_dump(name: str):
            dump_url = f"{HASHEOUS_BASE_URL}{name}.zip"
            if verbose:
                print(f"Fetching {dump_url}")

            async with httpx.AsyncClient() as client:
                async with client.stream("GET", dump_url, timeout=httpx.Timeout(None)) as response:
                    response.raise_for_status()
                    content_type = response.headers.get('content-type')

                    if not content_type or 'application/zip' not in content_type.lower():
                        raise ValueError(f"Expected content type 'application/zip', got {content_type} for dump {name}")

                    outpath = outdir / f"{name}.zip"
                    async with aiofiles.open(outpath, "wb") as out_file:
                        async for chunk in response.aiter_bytes():
                            await out_file.write(chunk)

            if verbose:
                print(f"Saved dump to {outpath}")

        for d in dumps:
            group.create_task(fetch_dump(d), name="fetch_dump_" + d)

async def handle_index(args: argparse.Namespace) -> None:
    input_paths: Collection[str] = args.paths
    verbose = bool(args.verbose)
    output: Path = args.output

    if verbose:
        print("Input paths:", input_paths)
        print("Output path:", output)

    zip_paths: set[Path] = set()
    for path in map(Path, input_paths):
        if zipfile.is_zipfile(path):
            zip_paths.add(path.resolve())
        elif path.is_dir():
            glob_paths = path.rglob('*.zip')
            glob_zips = filter(zipfile.is_zipfile, glob_paths)
            zip_paths.update(p.resolve() for p in glob_zips)

    if not zip_paths:
        print("No ZIP files found in the specified input paths.", file=sys.stderr)
        return

    if verbose:
        print(f"Found {len(zip_paths)} ZIP files to index.")
        pprint(zip_paths)

    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Indexing all DataObjects...")
    index_start = time.perf_counter_ns()
    with ProcessPoolExecutor() as executor:
        index = await load_dataobjects(zip_paths, PLAYLISTS, executor)
    index_finish = time.perf_counter_ns()
    print(f"Indexed all DataObjects in {(index_finish - index_start) / 1_000_000:.2f} ms")

    dump_start = time.perf_counter_ns()
    print(f"Saving index to {output}...")
    with open(output, "wb") as out_file:
        pickle.dump(index, out_file, protocol=5)
    dump_finish = time.perf_counter_ns()
    print(f"Saved index to {output} in {(dump_finish - dump_start) / 1_000_000:.2f} ms")


class MetadataMatch(TypedDict):
    source: Literal["IGDB"] # Only IGDB is supported for now
    platformId: str
    gameId: str

class FixMatchBody(TypedDict):
    mD5: Optional[str]
    shA1: Optional[str]
    metadataMatches: Sequence[MetadataMatch]

# See https://github.com/gaseous-project/hasheous/wiki/API:-Submission-%E2%80%90-FixMatch for API guidance
async def submit_matches(tsv_path: Path, api_key: str, dry_run: bool = False, verbose: bool = False) -> None:
    def read_match(row: dict[str, str]) -> MatchRecord:
        def parse_optional_int(value: str) -> Optional[int]:
            value = value.strip()
            if not value:
                return None
            return int(value)

        def parse_optional_str(value: str) -> Optional[str]:
            value = value.strip()
            if not value:
                return None
            return value

        return MatchRecord(
            name=row['name'].strip(),
            crc=parse_optional_str(row['crc']),
            md5=parse_optional_str(row['md5']),
            sha1=parse_optional_str(row['sha1']),
            serial=parse_optional_str(row['serial']),
            hasheous_id=parse_optional_int(row['hasheous_id']),
            hasheous_url=parse_optional_str(row['hasheous_url']),
            igdb_id=parse_optional_int(row['igdb_id']),
            igdb_url=parse_optional_str(row['igdb_url']),
            igdb_release_id=parse_optional_int(row['igdb_release_id']),
            igdb_platform_id=parse_optional_int(row['igdb_platform_id']),
        )

    def can_submit(match: MatchRecord) -> bool:
        return match.igdb_id is not None and \
               match.hasheous_id is not None and \
               ((match.md5 or match.sha1) is not None) and \
               match.crc is not None

    def make_body(match: MatchRecord) -> FixMatchBody:
        metadata_matches: Sequence[MetadataMatch] = [{
            "source": "IGDB",
            "platformId": str(match.igdb_platform_id),
            "gameId": str(match.igdb_release_id),
        }]

        return FixMatchBody(
            mD5=match.md5,
            shA1=match.sha1,
            metadataMatches=metadata_matches,
        )

    async with aiofiles.open(tsv_path, "r", encoding="utf-8") as tsv_file:
        lines = await tsv_file.readlines()
        reader = csv.DictReader(lines, fieldnames=MatchRecord._fields, dialect='excel-tab')
        matches = (read_match(m) for m in reader)
        valid_matches = filter(can_submit, matches)






async def handle_submit(args: argparse.Namespace) -> None:
    matchfiles: Collection[Path] = args.matchfiles
    api_key: Optional[str] = args.api_key or os.getenv("HASHEOUS_API_KEY", None)
    dry_run: bool = bool(args.dry_run)
    verbose: bool = bool(args.verbose)

    if api_key is None:
        print("Error: No Hasheous API key provided. Use --api-key or set the HASHEOUS_API_KEY environment variable.", file=sys.stderr)
        return

    if verbose:
        print("Match files to submit:", matchfiles)
        print("Dry run:", dry_run)

    async with asyncio.TaskGroup() as group:
        for matchfile in matchfiles:
            group.create_task(
                submit_matches(
                    matchfile,
                    api_key,
                    dry_run=dry_run,
                    verbose=verbose
                ),
                name="submit_matches_" + matchfile.stem
            )

def main():
    """Main entry point for the script."""

    parser = argparse.ArgumentParser(
        description="Utilities for fetching and processing data from Hasheous.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show more logging output"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # fetch subcommand
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch data from Hasheous and save it to the specified directory"
    )
    fetch_parser.add_argument(
        "--dumps",
        type=str,
        help="The names of the Hasheous dumps to fetch. Defaults to all 'hasheous' entries in metadat/igdb/igdb.toml plus 'Unknown Platform'.",
        action="extend",
        nargs="*",
        default=None
    )
    fetch_parser.add_argument(
        "outdir",
        type=Path,
        nargs="?",
        help="The output directory for the scraped JSON files",
        default="tmp/hasheous",
    )
    fetch_parser.set_defaults(func=handle_fetch)

    # index subcommand
    index_parser = subparsers.add_parser(
        "index",
        help="Create an index of DataObjects from the specified ZIP files."
    )
    index_parser.add_argument(
        "paths",
        help="A ZIP file or a directory containing Hasheous ZIP files",
        default=["tmp/hasheous"],
        action="extend",
        nargs="*",
    )
    index_parser.add_argument(
        "--output",
        type=Path,
        help="The output file to save the index to.",
        default="tmp/index/hasheous.pkl",
    )
    index_parser.set_defaults(func=handle_index)

    # `submit` subcommand
    submit_parser = subparsers.add_parser(
        "submit",
        help="Submit match data to Hasheous."
    )
    submit_parser.add_argument(
        "--api-key",
        type=str,
        help="The Hasheous API key to use for submission. Overrides the HASHEOUS_API_KEY environment variable if provided.",
    )
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't actually submit anything; just show what would be submitted."
    )
    submit_parser.add_argument(
        "matchfiles",
        type=Path,
        nargs="+",
        help="One or more TSV files containing match data to submit, as generated by match.py's `generate` subcommand. Only rows that include an IGDB ID, a Hasheous ID, a CRC, and an MD5 or SHA1 will be included.",
    )
    submit_parser.set_defaults(func=handle_submit)

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = (
    "Attribute",
    "AttributeName",
    "AttributeType",
    "DataObject",
    "DataObjectType",
    "HasheousId",
    "HasheousIndex",
    "load_dataobjects",
    "HASHEOUS_OBJECT_TYPES",
    "load_zip",
    "load_index",
    "MappingStatus",
    "MatchMethodType",
    "MatchRecord",
    "MediaType",
    "METADATA_MAP_URL",
    "MetadataItem",
    "MetadataSource",
    "RomItem",
    "RomTypeName",
    "SignatureDataObject",
    "SignatureSourceType",
)

if __name__ == "__main__":
    main()
