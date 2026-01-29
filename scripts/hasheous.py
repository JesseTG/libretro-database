#!/usr/bin/env python3
"""
Dictionary definitions taken from the following Hasheous source files:

- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItem.cs
- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/DataObjectItemModel.cs
- https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Models/Signatures_Games.cs
- https://github.com/gaseous-project/gaseous-signature-parser/blob/main/gaseous-signature-parser/models/RomSignatureObject.cs
"""

import asyncio
import csv
import sys
import tomllib

from abc import ABC
from collections.abc import Sequence, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, NewType, Optional, TypeAlias, TypedDict
from zipfile import ZipFile, ZipInfo

import aiofiles
import aiofiles.os
import backoff
import httpx

from more_itertools import first_true
from pydantic import AliasChoices, BaseModel, ByteSize, ConfigDict, Field, FieldSerializationInfo, FilePath, HttpUrl, PlainSerializer, PlainValidator, SerializerFunctionWrapHandler, StringConstraints, TypeAdapter, ValidationError, WrapSerializer, WrapValidator, computed_field, field_serializer
from pydantic.alias_generators import to_pascal
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict
from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.sqlite import INTEGER, JSON
from sqlalchemy.util import is_non_string_iterable

from igdb import IgdbId, PlaylistConfig
from utils import DatabaseModel, FrozenDictValidator, Hash, InsertInRowContext, EmptyStringToNone, EMPTY_DICT, Relationship

METADATA_MAP_URL = "https://hasheous.org/api/v1/Dumps/MetadataMap.zip"

HasheousId = NewType('HasheousId', int)

class HasheousObject(DatabaseModel, ABC, frozen=True):
    pass

class SignatureDataObject(HasheousObject, frozen=True, alias_generator=to_pascal):
    __tablename__: ClassVar[str] = "HasheousSignatureDataObject"
    signature_id: Annotated[int, Column(primary_key=True)]
    name: EmptyStringToNone[str] = None
    year: EmptyStringToNone[str] = None
    platform: EmptyStringToNone[str] = None
    source_id: Annotated[EmptyStringToNone[int], Column(index=True)] = None
    publisher: EmptyStringToNone[str] = None
    metadata_source: EmptyStringToNone[str] = None

MappingStatus: TypeAlias = Literal["NotMapped", "Mapped", "MappedWithErrors"]
ImageId = Annotated[str, StringConstraints(to_upper=True)]

MatchMethodType: TypeAlias = Literal[
    "NoMatch",
    "Automatic",
    "Manual",
    "AutomaticTooManyMatches",
    "ManualByAdmin",
    "Voted",
]

@dataclass(frozen=True)
class MetadataItem:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(alias_generator=to_pascal)

    id: EmptyStringToNone[str]
    immutable_id: EmptyStringToNone[str]
    status: MappingStatus
    match_method: MatchMethodType
    source: str
    link: Annotated[HttpUrl | None, WrapValidator(lambda v, h: h(v) if v else None), PlainSerializer(lambda v: v or None, str | None)]
    next_search: datetime
    winning_vote_count: int
    total_vote_count: int
    winning_vote_percent: int

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

DataObjectType: TypeAlias = Literal["None", "Company", "Platform", "Game", "ROM", "App"]

class MediaType(TypedDict, total=False):
    MediaType: str
    Media: str
    Number: int
    Count: int
    Side: str

class RomItem(HasheousObject, frozen=True, alias_generator=to_pascal):
    """
    Structure taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Schema/hasheous-1000.sql
    (specifically the Signatures_Roms table)
    """
    __tablename__: ClassVar[str] = "HasheousRomItem"
    id: Annotated[int, Column(primary_key=True)]
    name: EmptyStringToNone[str]
    attributes: Annotated[Mapping[str, str], Column(JSON), FrozenDictValidator]
    rom_type: Annotated[str, Column(index=True)]
    size: ByteSize
    crc: Annotated[EmptyStringToNone[Hash], Column(index=True)]
    md5: Annotated[EmptyStringToNone[Hash], Column(index=True)]
    sha1: Annotated[EmptyStringToNone[Hash], Column(index=True)]
    sha256: Annotated[EmptyStringToNone[Hash], Column(index=True)]
    status: EmptyStringToNone[str]

    # TODO: Represent Country with computed columns
    country: Annotated[Mapping[str, str], Column(JSON), FrozenDictValidator]

    # TODO: Represent Language with computed columns
    language: Annotated[Mapping[str, str], Column(JSON), FrozenDictValidator]
    development_status: EmptyStringToNone[str]
    rom_type_media: EmptyStringToNone[str]

    # TODO: Represent MediaDetail with computed columns
    media_detail: Annotated[MediaType, Column(JSON), FrozenDictValidator]
    media_label: EmptyStringToNone[str]
    signature_source: Annotated[EmptyStringToNone[str], Column(index=True)]

RomItemTupleAdapter = TypeAdapter(tuple[RomItem, ...])
def coerce_attribute(value: Any) -> "str | tuple[RomItem, ...] | DataObject | Mapping":
    match value:
        case {} if not value:
            return EMPTY_DICT
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
    attribute_type: Annotated[str, Field(validation_alias='attributeType')]
    attribute_name: Annotated[str, Field(validation_alias='attributeName')]
    attribute_relation_type: Annotated[str, Field(validation_alias='attributeRelationType')]

    value: Annotated["str | tuple[RomItem, ...] | DataObject | Mapping", PlainValidator(coerce_attribute), Field(validation_alias='Value')]

    id: Annotated[int | None, Field(validation_alias='Id')] = None

DataObjectAttributeColumn = Annotated[
    "DataObject | None",
    Column(ForeignKey('HasheousDataObject.id'), index=True)
]

CompanyDataObjectAttributeColumn = Annotated[
    "CompanyDataObject | None",
    Column(ForeignKey('HasheousCompanyDataObject.id'), index=True)
]

PlatformDataObjectAttributeColumn = Annotated[
    "PlatformDataObject | None",
    Column(ForeignKey('HasheousPlatformDataObject.id'), index=True)
]

def IgdbIdReference(column: str):
    return Annotated[IgdbId | None, Column(ForeignKey(column))]

class DataObject(DatabaseModel, ABC, frozen=True, alias_generator=to_pascal):
    """
    Type info for attributes taken from https://github.com/gaseous-project/hasheous/blob/main/hasheous-lib/Classes/DataObjects.cs
    """

    id: Annotated[HasheousId, Column(INTEGER, primary_key=True)]
    name: str
    signature_data_objects: tuple[SignatureDataObject, ...]
    metadata: Annotated[tuple[MetadataItem, ...], Field(exclude=True)]
    attributes: Annotated[tuple[Attribute, ...], Field(exclude=True)]
    created_date: datetime
    updated_date: datetime

    @field_serializer('platform', 'manufacturer', 'publisher', mode='wrap', check_fields=False)
    def _serialize_field(self, value: Any, handler: SerializerFunctionWrapHandler, info: FieldSerializationInfo[InsertInRowContext]):
        match (info.context, value):
            case (None, _):
                # If no context is given, serialize the field as usual
                return handler(value)
            case ('row', DataObject()):
                # If serializing for a database row, serialize nested DataObjects as their IDs
                return value.id
            case (_, _):
                # Otherwise, run the default serializer to handle other types or contexts
                return handler(value)

    def _get_igdb_id(self) -> IgdbId | None:
        """Returns the IGDB ID mapped to this DataObject, or None if there's no IGDB mapping."""
        igdb_metadata = first_true(self.metadata, pred=lambda m: m.source == "IGDB" and m.status == "Mapped")
        if not igdb_metadata:
            return None
        if not igdb_metadata.immutable_id:
            return None

        try:
            return IgdbId(int(igdb_metadata.immutable_id))
        except ValueError:
            return None

class PlatformDataObject(DataObject, frozen=True, alias_generator=to_pascal):
    __tablename__: ClassVar[str] = "HasheousPlatformDataObject"
    object_type: Annotated[Literal["Platform"], Field(exclude=True)]

    @computed_field(return_type=IgdbIdReference('IgdbPlatform.id'))
    @cached_property
    def igdb_id(self):
        """Returns the IGDB ID mapped to this DataObject, or None if there's no IGDB mapping."""
        return self._get_igdb_id()

    @computed_field(return_type=CompanyDataObjectAttributeColumn)
    @cached_property
    def manufacturer(self):
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Manufacturer")
        return attribute.value if attribute and isinstance(attribute.value, CompanyDataObject) else None

class CompanyDataObject(DataObject, frozen=True, alias_generator=to_pascal):
    __tablename__: ClassVar[str] = "HasheousCompanyDataObject"
    object_type: Annotated[Literal["Company"], Field(exclude=True)]

    @computed_field(return_type=IgdbIdReference('IgdbCompany.id'))
    @cached_property
    def igdb_id(self):
        """Returns the IGDB ID mapped to this DataObject, or None if there's no IGDB mapping."""
        return self._get_igdb_id()

class GameDataObject(DataObject, frozen=True, alias_generator=to_pascal):
    __tablename__: ClassVar[str] = "HasheousGameDataObject"
    object_type: Annotated[Literal["Game"], Field(exclude=True)]

    @computed_field
    @cached_property
    def roms(self) -> tuple[RomItem, ...]:
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "ROMs")
        return tuple(attribute.value) if attribute and is_non_string_iterable(attribute.value) else ()

    @computed_field(return_type=IgdbIdReference('IgdbGame.id'))
    @cached_property
    def igdb_id(self):
        """Returns the IGDB ID mapped to this DataObject, or None if there's no IGDB mapping."""
        return self._get_igdb_id()

    @computed_field
    @cached_property
    def manufacturer(self) -> CompanyDataObjectAttributeColumn:
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Manufacturer")
        return attribute.value if attribute and isinstance(attribute.value, CompanyDataObject) else None

    @computed_field
    @cached_property
    def publisher(self) -> CompanyDataObjectAttributeColumn:
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Publisher")
        return attribute.value if attribute and isinstance(attribute.value, CompanyDataObject) else None

    @computed_field
    @cached_property
    def platform(self) -> PlatformDataObjectAttributeColumn:
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Platform")
        return attribute.value if attribute and isinstance(attribute.value, PlatformDataObject) else None

    @computed_field
    @cached_property
    def country(self) -> tuple[str, ...]:
        # TODO: Need to provide an index on the country values for efficient querying
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Country")
        if not attribute or not isinstance(attribute.value, str):
            return ()

        return tuple(c.strip() for c in attribute.value.split(','))

    @computed_field
    @cached_property
    def language(self) -> tuple[str, ...]:
        attribute = first_true(self.attributes, pred=lambda a: a.attribute_name == "Language")
        if not attribute or not isinstance(attribute.value, str):
            return ()

        return tuple(lang.strip() for lang in attribute.value.split(','))


HASHEOUS_OBJECT_TYPES = (
    GameDataObject,
    PlatformDataObject,
    CompanyDataObject,
    SignatureDataObject,
    RomItem,
)

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


async def load_zip(path: Path) -> tuple[GameDataObject, ...]:
    async with aiofiles.open(path, "rb") as zip_file:
        with ZipFile(zip_file.raw) as zip:

            def validate(info: ZipInfo):
                byte_data = zip.read(info)
                try:
                    return GameDataObject.model_validate_json(byte_data)
                except ValidationError as ve:
                    raise

            paths = zip.infolist()
            json_infos = filter(lambda p: p.filename.endswith('.json') and p.filename != 'PlatformMapping.json', paths)
            objects = map(validate, json_infos)

            return tuple(objects)

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

class CommonArgs(BaseModel):
    verbose: bool = Field(
        default=False,
        description="Enable verbose output.",
        validation_alias=AliasChoices('v', 'verbose'),
    )

class FetchSubCommand(CommonArgs):
    config: FilePath = Field(
        default=Path(__file__).parent.parent / 'playlists.toml',
        title="Playlist Config File",
        description="Path to the config file that defines available playlists.",
        validation_alias=AliasChoices('c', 'config'),
        validate_default=True,
    )

    dumps: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices('d', 'dumps'),
        description="""
            The names of the Hasheous dumps to fetch.
            If not specified, all 'hasheous' entries in 'config' plus 'Unknown Platform' will be fetched.
        """
    )

    outdir: CliPositionalArg[Path] = Field(
        default=Path(__file__).parent.parent / 'tmp' / 'hasheous',
        description="""
            The output directory for the fetched Hasheous dumps.
            Will be created if it doesn't exist.
        """
    )

    async def cli_cmd(self):
        await aiofiles.os.makedirs(self.outdir, exist_ok=True)

        if self.dumps:
            # If specific dumps were requested, use those plus "Unknown Platform"
            dumps = set(self.dumps)
        else:
            # Otherwise, fetch all playlists that specify a Hasheous dump (but include "Unknown Platform" too)
            async with aiofiles.open(self.config, 'rb') as f:
                config = PlaylistConfig.model_validate(tomllib.load(f.raw))

            dumps = set(chain.from_iterable(p.hasheous_dirs for p in config.playlists))

        dumps.add("Unknown Platform")
        # "Unknown Platform" entries don't identify a specific platform,
        # but a lot of them do have CRCs that can be useful.

        async with asyncio.TaskGroup() as group:
            @backoff.on_exception(backoff.expo, httpx.HTTPStatusError, max_tries=5, giveup=_giveup, on_backoff=_on_backoff)
            async def fetch_dump(name: str):
                dump_url = f"{HASHEOUS_BASE_URL}{name}.zip"
                print(f"Fetching {dump_url}")

                async with httpx.AsyncClient() as client:
                    async with client.stream("GET", dump_url, timeout=httpx.Timeout(None)) as response:
                        response.raise_for_status()
                        content_type = response.headers.get('content-type')

                        if not content_type or 'application/zip' not in content_type.lower():
                            raise ValueError(f"Expected content type 'application/zip', got {content_type} for dump {name}")

                        outpath = self.outdir / f"{name}.zip"
                        async with aiofiles.open(outpath, "wb") as out_file:
                            async for chunk in response.aiter_bytes():
                                await out_file.write(chunk)

                print(f"Saved dump to {outpath}")

            for d in dumps:
                group.create_task(fetch_dump(d), name=d)
            # The task group will wait for all fetches to complete

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
        raise NotImplementedError("Submission functionality is not yet implemented.")

class SubmitSubCommand(CommonArgs):
    api_key: str  = Field(
        description="The Hasheous API key to use for submission. Overrides the HASHEOUS_API_KEY environment variable if provided.",
        validation_alias=AliasChoices('a', 'api-key'),
    )

    dry_run: bool = Field(
        default=False,
        description="Don't actually submit anything; just show what would be submitted.",
        validation_alias=AliasChoices('n', 'dry-run'),
    )

    matchfiles: CliPositionalArg[tuple[FilePath, ...]] = Field(
        description="One or more TSV files containing match data to submit, as generated by match.py's `generate` subcommand. Only rows that include an IGDB ID, a Hasheous ID, a CRC, and an MD5 or SHA1 will be included.",
    )

    async def cli_cmd(self):
        if self.verbose:
            print("Match files to submit:", self.matchfiles)
            print("Dry run:", self.dry_run)

        async with asyncio.TaskGroup() as group:
            for matchfile in self.matchfiles:
                group.create_task(
                    submit_matches(
                        matchfile,
                        self.api_key,
                        dry_run=self.dry_run,
                        verbose=self.verbose
                    ),
                    name=matchfile.stem
                )

class HasheousCommand(BaseSettings):
    fetch: CliSubCommand[FetchSubCommand]
    submit: CliSubCommand[SubmitSubCommand]
    model_config = SettingsConfigDict(
        case_sensitive=False,
        cli_avoid_json=True,
        cli_implicit_flags=True,
        cli_kebab_case=True,
        cli_parse_args=True,
        extra="ignore",
    )

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


__all__ = (
    "Attribute",
    "AttributeType",
    "DataObject",
    "DataObjectType",
    "HasheousId",
    "HASHEOUS_OBJECT_TYPES",
    "load_zip",
    "MappingStatus",
    "MatchMethodType",
    "MatchRecord",
    "MediaType",
    "METADATA_MAP_URL",
    "MetadataItem",
    "RomItem",
    "SignatureDataObject",
)

if __name__ == "__main__":
    CliApp.run(HasheousCommand)
