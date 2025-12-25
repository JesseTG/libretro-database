#!/usr/bin/env python3

import argparse
import asyncio
import os.path
import re
import sys
import tomllib

from abc import ABC
from asyncio import Task, TaskGroup
from collections import ChainMap, defaultdict
from collections.abc import Collection, Sequence, Iterable, Iterator, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache, cached_property
from itertools import chain
from json import JSONDecodeError
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, ForwardRef, Never, NotRequired, Optional, Literal, NewType, Required, Self, TypeGuard, TypedDict, cast, overload

import aiofiles
import aiofiles.os
import asynciolimiter
import backoff
import httpx
import sqlalchemy

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import HTTPStatusError, Response, Timeout
from more_itertools import batched, one, only
from pydantic import BaseModel, BeforeValidator, FieldSerializationInfo, HttpUrl, PlainSerializer, SerializerFunctionWrapHandler, TypeAdapter, JsonValue, field_serializer
from pydantic_core import from_json, to_json
from pydantic_extra_types.country import CountryNumericCode
from sqlalchemy import Column, ForeignKey, MetaData, Table
from sqlalchemy.sql.base import SchemaEventTarget
from sqlalchemy.types import TypeEngine
from sqlalchemy.util.typing import GenericProtocol, TypeAliasType, de_optionalize_union_types, includes_none, is_pep695, is_fwd_ref, is_generic, is_literal, is_newtype, flatten_newtype, eval_expression, get_args

type AnnotationScanType = type[Any] | str | ForwardRef | NewType | TypeAliasType | GenericProtocol[Any]
type TupleOf[T] = tuple[T, ...]

def unwrap_type(t: AnnotationScanType) -> type:
    """
    Strips away literals, optionals, newtypes, generics, and forward references.
    """
    if includes_none(t):
        return unwrap_type(de_optionalize_union_types(t))

    if is_pep695(t):
        # If this is a TypeAliasType as defined by PEP 695...
        return unwrap_type(t.__value__)

    if is_literal(t):
        args = get_args(t)
        literal_types = set(map(type, args))
        if len(literal_types) > 1:
            raise NotImplementedError(f"Literal {t} with multiple different argument types ({literal_types}) is not supported")

        return type(args[0])

    if is_newtype(t):
        return flatten_newtype(t)

    if is_generic(t):
        return t.__origin__

    if is_fwd_ref(t, check_generic=True, check_for_plain_string=True):
        return unwrap_type(eval_expression(t.__forward_arg__, __name__))

    if isinstance(t, str):
        return unwrap_type(eval_expression(t, __name__))

    assert isinstance(t, type), f"Unexpected type annotation: {t} ({type(t)})"
    return t


@dataclass(kw_only=True, eq=True)
class ColumnDef:
    # TODO: Write about how I tried to use SQLModel but it was a pain in the ass
    name: str | None
    coltype: type[TypeEngine] | TypeEngine | SchemaEventTarget | None
    primary_key: bool
    index: bool | None
    unique: bool | None
    nullable: bool | None
    kwargs: dict[str, Any]

    def __init__(
            self,
            /,
            name: str | None = None,
            type: type[TypeEngine] | TypeEngine | SchemaEventTarget | None = None,
            index: bool | None = None,
            unique: bool | None = None,
            nullable: bool | None = None,
            primary_key: bool = False,
            **kwargs: Any,
    ):
        self.name = name
        self.coltype = type
        self.primary_key = primary_key
        self.index = index
        self.unique = unique
        self.nullable = nullable
        self.kwargs = kwargs

    def get_schema(self, model_type: type[BaseModel], field_name: str) -> Column:
        fields = model_type.model_fields
        if not (field := fields.get(field_name)):
            raise KeyError(f"Model {model_type.__name__} has no field named {field_name!r}")

        if not (annotation := field.annotation):
            raise TypeError(f"{model_type.__name__}.{field_name!r} has no type annotation, cannot generate column definition")

        nullable = self.nullable if self.nullable is not None else includes_none(annotation)
        coltype = self.coltype or get_column_type(annotation)

        return Column(
            self.name or field_name,
            coltype,
            primary_key=self.primary_key,
            index=self.index,
            unique=self.unique,
            nullable=nullable,
            **self.kwargs,
        )

@overload
def get_column_type(annotation: None) -> Never: ...

@overload
def get_column_type(annotation: AnnotationScanType) -> type[TypeEngine] | ForeignKey: ...

def get_column_type(annotation: AnnotationScanType | None) -> type[TypeEngine] | ForeignKey:
    """
    Maps a Pydantic type annotation to a SQLAlchemy column type.
    """
    if annotation is None:
        raise TypeError("Cannot determine column type for field with no type annotation")

    field_type = unwrap_type(annotation)

    if field_type == bool:
        return sqlalchemy.Boolean

    if issubclass(field_type, (int, CountryNumericCode)):
        return sqlalchemy.Integer

    if issubclass(field_type, (str, HttpUrl)):
        return sqlalchemy.String

    if field_type == datetime:
        return sqlalchemy.DateTime

    if field_type == date:
        return sqlalchemy.Date

    if field_type == float:
        return sqlalchemy.Float

    if field_type == JsonValue:
        return sqlalchemy.JSON

    if issubclass(field_type, DatabaseModel):
        # If this field refers to a specific model, create a ForeignKey to that model's table
        colname = one(field_type.pk_columns())
        # one() raises if its argument doesn't have exactly one item

        return ForeignKey(f"{field_type.__tablename__}.{colname}")

    raise NotImplementedError(f"Unsupported field type: {field_type}")

def is_non_string_iterable_type(t: type[Any]) -> TypeGuard[type[Iterable[Any]]]:
    """
    Determines whether a type annotation represents a non-string iterable type.
    """
    if issubclass(t, Sequence) and not issubclass(t, (str, bytes, bytearray)):
        return True

    return False

@dataclass(eq=True)
class RelationshipDef:
    """
    Composite foreign keys are not yet supported.
    """

    foreign_key: ForeignKey
    foreign_colname: str

    def __init__(self,
        spec: ForeignKey | str | type["DatabaseModel"] | ForwardRef,
        foreign_colname: str | None = None,
        **kwargs
    ) -> None:
        match spec:
            case ForeignKey():
                self.foreign_key = spec
                self.foreign_colname = foreign_colname or "foreign_pk"
            case str() as s:
                self.foreign_key = ForeignKey(s, **kwargs)
                self.foreign_colname = foreign_colname or "foreign_pk"
            case ForwardRef():
                foreign_type = eval_expression(spec.__forward_arg__, __name__)
                if not issubclass(foreign_type, DatabaseModel):
                    raise TypeError(f"{spec} does not refer to a DatabaseModel subclass")

                # one() raises if its argument doesn't have exactly one item
                foreign_pk_colname = one(foreign_type.pk_columns())
                self.foreign_key = ForeignKey(f"{foreign_type.__tablename__}.{foreign_pk_colname}", **kwargs)
                self.foreign_colname = foreign_colname or f"{foreign_type.__tablename__}_{foreign_pk_colname}"
            case type() as foreign_type if issubclass(foreign_type, DatabaseModel):
                foreign_pk_colname = one(foreign_type.pk_columns())
                self.foreign_key = ForeignKey(f"{foreign_type.__tablename__}.{foreign_pk_colname}", **kwargs)
                self.foreign_colname = foreign_colname or f"{foreign_type.__tablename__}_{foreign_pk_colname}"
            case _:
                raise TypeError(f"Expected a ForeignKey, str, ForwardRef, or type[DatabaseModel]: got {type(spec)} ({spec})")

    def create_table(self, metadata: MetaData, parent_type: type["DatabaseModel"], parent_field_name: str) -> Table:
        """
        Creates a relationship table according to the following conventions:

        - Composite foreign key relationships aren't supported, an exception will be raised if attempted
        - The table is named `<parent tablename>_<field name>`
        - The parent's primary key is referenced as a foreign key column named `<parent tablename>_<parent pk column name>`
        - The child's primary key column is referenced as a foreign key column named `<child_tablename>_<field name>`
          (unless `foreign_colname` was specified in __init__(), in which case that name is used instead)
        """
        parent_pk_colname = one(parent_type.pk_columns())

        return Table(
            f"{parent_type.__tablename__}_{parent_field_name}",
            metadata,
            Column(
                f"{parent_type.__tablename__}_{parent_pk_colname}",
                ForeignKey(f"{parent_type.__tablename__}.{parent_pk_colname}"),
                primary_key=True
            ),
            Column(
                self.foreign_colname,
                self.foreign_key,
                primary_key=True
            ),
        )

SchemaDef = ColumnDef | RelationshipDef

class DatabaseModel(BaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str]

    @classmethod
    @cache
    def pk_columns(cls) -> dict[str, ColumnDef]:
        """
        Returns a dictionary of primary key columns for the model.

        :return: A dictionary mapping field names to ColumnDef instances that are primary keys.
        """

        result: dict[str, ColumnDef] = {}
        for field_name, field in cls.model_fields.items():
            defn = only(m for m in field.metadata if isinstance(m, ColumnDef))
            if defn and defn.primary_key:
                result[field_name] = defn

        return result

    @classmethod
    @cache
    def relationship_defs(cls) -> dict[str, RelationshipDef]:
        """
        Gets all RelationshipDef instances from this model type's fields,
        as defined in their Annotated metadata.
        Returns a map of field names to RelationshipDef instances,
        empty if none are found.

        :raises ValueError: if multiple RelationshipDefs are found on a single field.
        """

        result: dict[str, RelationshipDef] = {}
        for field_name, field in cls.model_fields.items():
            defn = only(m for m in field.metadata if isinstance(m, RelationshipDef))
            if defn:
                result[field_name] = defn

        return result

    @classmethod
    def create_tables(cls, metadata: MetaData) -> tuple[Table, *tuple[Table, ...]]:
        """
        Creates a SQLAlchemy Table object for this model type,
        and any associated relationship tables.

        Returns a tuple where the first item is the main table,
        and any subsequent items are relationship tables.
        """
        main_table = Table(cls.__tablename__, metadata)
        relationship_tables: list[Table] = []

        for field_name, field in cls.model_fields.items():
            annotation: type[Any] | None = field.annotation
            if annotation is None:
                raise TypeError(f"{cls.__name__}.{field_name!r} has no type annotation, cannot generate column definition")

            unwrapped_type = unwrap_type(annotation)
            match only(m for m in field.metadata if isinstance(m, SchemaDef)):
                # only() raises if its argument has more than one item
                case ColumnDef() as coldef:
                    # Create a Column with the specified ColumnDef
                    main_table.append_column(coldef.get_schema(cls, field_name))
                case RelationshipDef() as reldef:
                    # An explicit RelationshipDef was provided, so use it
                    relationship_tables.append(reldef.create_table(metadata, cls, field_name))
                case None if is_non_string_iterable_type(unwrapped_type):
                    # This field is a collection of related DatabaseModel instances
                    arg_type = get_args(unwrapped_type)[0]
                    unwrapped_arg_type = unwrap_type(arg_type)
                    reldef = RelationshipDef(unwrapped_arg_type)
                    relationship_tables.append(reldef.create_table(metadata, cls, field_name))
                case None:
                    # No SchemaDef was provided, create a ColumnDef with defaults
                    main_table.append_column(ColumnDef().get_schema(cls, field_name))
                case other:
                    raise TypeError(f"Expected zero or one SchemaDef on {cls.__name__}.{field_name!r}, got {other}")


        return (main_table, *relationship_tables)


    @cached_property
    def nested_models(self) -> frozenset["DatabaseModel"]:
        """
        Returns a set of all nested DatabaseModel instances referenced by this model's fields,
        excluding itself.
        """
        models: set[DatabaseModel] = set()
        for field_name in type(self).model_fields:
            match getattr(self, field_name):
                case DatabaseModel() as obj:
                    models.add(obj)
                    models.update(obj.nested_models)
                case [*items]:
                    objects = (i for i in items if isinstance(i, DatabaseModel))
                    for obj in objects:
                        models.add(obj)
                        models.update(obj.nested_models)

        return frozenset(models)

    @cached_property
    def relationships(self) -> Mapping[str, list[dict[str, Any]]]:
        """
        Returns a dictionary whose keys are field names representing relationships,
        and whose values are sets of dicts suitable for relationship tables.
        These dicts include the foreign key mappings for this object and the related objects.

        This property is not recursive, i.e. it does not include relationships from nested models.
        """
        results: dict[str, list[dict[str, Any]]] = defaultdict(list)
        cls = type(self)
        pk_coldefs = cls.pk_columns()
        reldefs = cls.relationship_defs()

        for field_name, field_info in cls.model_fields.items():
            if field_name not in reldefs:
                continue

            reldef = reldefs[field_name]
            foreign_colname = reldef.foreign_colname

            match getattr(self, field_name):
                case DatabaseModel() as related_obj:
                    mapping: dict[str, Any] = {}
                    for pk_field_name, pk_coldef in pk_coldefs.items():
                        mapping[f"{cls.__tablename__}_{pk_coldef.name or pk_field_name}"] = getattr(self, pk_field_name)

                    related_pk_coldefs = type(related_obj).pk_columns()
                    for related_pk_field_name, related_pk_coldef in related_pk_coldefs.items():
                        mapping[foreign_colname] = getattr(related_obj, related_pk_field_name)

                    results[field_name].append(mapping)
                case [*related_objs]:
                    for related_obj in related_objs:
                        if not isinstance(related_obj, DatabaseModel):
                            continue

                        mapping = {}
                        for pk_field_name, pk_coldef in pk_coldefs.items():
                            mapping[f"{cls.__tablename__}_{pk_coldef.name or pk_field_name}"] = getattr(self, pk_field_name)

                        related_pk_coldefs = type(related_obj).pk_columns()
                        for related_pk_field_name, related_pk_coldef in related_pk_coldefs.items():
                            mapping[foreign_colname] = getattr(related_obj, related_pk_field_name)

                        results[field_name].append(mapping)
                case _:
                    continue

        return MappingProxyType(results)

IgdbId = NewType('IgdbId', int)
IgdbPrimaryId = Annotated[IgdbId, ColumnDef(type=sqlalchemy.Integer, primary_key=True)]
PlaylistTitle = NewType('PlaylistTitle', str)
IgdbGameIds = Annotated[tuple[IgdbId, ...], ForeignKey('IgdbGame.id')]

def country_numeric_code_validator(value: Any) -> CountryNumericCode:
    """
    IGDB returns ISO 3166-1 numeric country codes as integers,
    but by default pydantic expects them to be three-digit numeric strings
    including leading zeroes.
    This validator coerces ints and strings to CountryNumericCode instances,
    accounting for padding as necessary.
    """
    match value:
        case int(i) | float(i) if 0 <= i <= 999 and i.is_integer():
            return CountryNumericCode(f"{int(i):03}")
        case int(i) | float(i):
            raise ValueError(f"Expected an int between 0 and 999 (inclusive) for CountryNumericCode; got {i}")
        case str(s) if re.fullmatch(r'^[0-9]{1,3}$', s):
            return CountryNumericCode(s.zfill(3))
        case str(s):
            raise ValueError(f"Expected a str of 1 to 3 digits for CountryNumericCode; got {s!r}")
        case CountryNumericCode():
            return value
        case _:
            raise ValueError(f"Expected an int, str, or CountryNumericCode; got {type(value).__name__}")

class RelationshipSpecifier(TypedDict):
    self_colname: str
    related_colname: str

type CoercedCountryCode = Annotated[CountryNumericCode, BeforeValidator(country_numeric_code_validator)]
type CoercedHttpUrl = Annotated[HttpUrl, PlainSerializer(str, str)]
type IgdbObjectSerializeMode = Literal['row'] | None


class IgdbObject(DatabaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str]
    id: IgdbPrimaryId

    @field_serializer('*', mode='wrap')
    def _serialize_field(self, value: Any, handler: SerializerFunctionWrapHandler, info: FieldSerializationInfo[IgdbObjectSerializeMode]):
        match (info.context, value):
            case (None | 'default', _):
                # If no context is given, serialize the field as usual
                return handler(value)
            case ('row', IgdbObject()):
                # If serializing for a database row, serialize nested IgdbObjects as their IDs
                return value.id
            case ('row', []):
                # If serializing for a database row, return empty sequences as-is
                # (common-case optimization)
                assert len(value) == 0
                return value
            case ('row', [*rest]) if all(isinstance(item, IgdbObject) for item in rest):
                # If serializing for a database row, serialize tuples of IgdbObjects as tuples of their IDs
                return tuple(item.id for item in rest)
            case ('row', _):
                # Otherwise, run the default serializer to handle other types
                return handler(value)
            case (_, _):
                raise ValueError(f"Expected a serialization context value of 'default', 'row', or None; got {info.context!r}")

class AgeRatingOrganization(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingOrganization"
    id: IgdbPrimaryId
    name: str

class AgeRatingCategory(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingCategory"
    id: IgdbPrimaryId
    organization: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbAgeRatingOrganization.id'))]
    rating: str

class AgeRatingContentDescriptionType(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingContentDescriptionType"
    id: IgdbPrimaryId
    name: str

class AgeRatingContentDescriptionV2(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingContentDescriptionV2"
    id: IgdbPrimaryId
    description: str
    description_type: AgeRatingContentDescriptionType
    organization: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbAgeRatingOrganization.id'))]

class AgeRating(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRating"
    id: IgdbPrimaryId
    organization: AgeRatingOrganization
    rating_category: AgeRatingCategory
    rating_content_descriptions: Annotated[TupleOf[AgeRatingContentDescriptionV2], RelationshipDef("IgdbAgeRatingContentDescriptionV2.id")] = ()

class AlternativeName(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAlternativeName"
    id: IgdbPrimaryId
    name: str
    comment: str | None = None
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]

class Franchise(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbFranchise"
    id: IgdbPrimaryId
    name: str

class GameEngine(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGameEngine"
    id: IgdbPrimaryId
    name: str

class GameLocalization(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGameLocalization"
    id: IgdbPrimaryId
    name: str | None = None
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]
    region: 'Region'

class GameMode(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGameMode"
    id: IgdbPrimaryId
    name: str

class GameStatus(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGameStatus"
    id: IgdbPrimaryId
    status: str

class GameType(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGameType"
    id: IgdbPrimaryId
    type: str

class Genre(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGenre"
    id: IgdbPrimaryId
    name: str

class CompanyStatus(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbCompanyStatus"
    id: IgdbPrimaryId
    name: str

class Company(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbCompany"
    id: IgdbPrimaryId
    country: CoercedCountryCode | None = None
    name: str
    status: CompanyStatus | None = None

class InvolvedCompany(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbInvolvedCompany"
    id: IgdbPrimaryId
    company: Company
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]
    developer: bool
    porting: bool
    publisher: bool
    supporting: bool

class Region(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbRegion"
    id: IgdbPrimaryId
    identifier: str
    name: str
    category: Literal['locale', 'continent']

class Keyword(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbKeyword"
    id: IgdbPrimaryId
    name: str

class Language(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbLanguage"
    id: IgdbPrimaryId
    locale: str # TODO: Represent as a tuple[LanguageAlpha2, CountryAlpha2]?
    name: str

class LanguageSupportType(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbLanguageSupportType"
    id: IgdbPrimaryId
    name: str

class LanguageSupport(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbLanguageSupport"
    id: IgdbPrimaryId
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]
    language: Language
    language_support_type: LanguageSupportType

class PlatformFamily(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbPlatformFamily"
    id: IgdbPrimaryId
    name: str

class PlatformType(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbPlatformType"
    id: IgdbPrimaryId
    name: str

class PlatformVersion(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbPlatformVersion"
    id: IgdbPrimaryId
    name: str

class Platform(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbPlatform"
    id: IgdbPrimaryId
    alternative_name: str | None = None
    generation: int | None = None
    name: str
    platform_family: PlatformFamily | None = None
    platform_type: PlatformType | None = None

class MultiplayerMode(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbMultiplayerMode"
    id: IgdbPrimaryId
    campaigncoop: bool
    dropin: bool
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: int | None = None
    offlinemax: int | None = None
    onlinecoop: bool
    onlinecoopmax: int | None = None
    onlinemax: int | None = None
    platform: Annotated[IgdbId | None, ColumnDef(type=ForeignKey('IgdbPlatform.id'))] = None
    splitscreen: bool
    splitscreenonline: bool | None = None

    @property
    def coop(self) -> bool:
        return self.campaigncoop or self.lancoop or self.offlinecoop or self.onlinecoop

class PlayerPerspective(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbPlayerPerspective"
    id: IgdbPrimaryId
    name: str

class DateFormat(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbDateFormat"
    id: IgdbPrimaryId
    format: str

class ReleaseDateRegion(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbReleaseDateRegion"
    id: IgdbPrimaryId
    region: str

class ReleaseDateStatus(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbReleaseDateStatus"
    id: IgdbPrimaryId
    description: str
    name: str

class ReleaseDate(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbReleaseDate"
    id: IgdbPrimaryId
    date: datetime | None = None
    date_format: DateFormat
    game: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbGame.id'))]
    human: str
    m: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]  | None = None  # Month (1-12)
    platform: Annotated[IgdbId, ColumnDef(type=ForeignKey('IgdbPlatform.id'))]
    release_region: ReleaseDateRegion
    status: ReleaseDateStatus | None = None
    y: int | None = None  # Year

class Theme(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbTheme"
    id: IgdbPrimaryId
    name: str

class Game(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGame"
    id: IgdbPrimaryId
    age_ratings: Annotated[TupleOf[AgeRating], RelationshipDef("IgdbAgeRating.id")] = ()
    aggregated_rating: float | None = None
    aggregated_rating_count: int | None = None
    alternative_names: Annotated[TupleOf[AlternativeName], RelationshipDef("IgdbAlternativeName.id")] = ()
    bundles: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    collections: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    dlcs: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    expanded_games: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    expansions: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    first_release_date: date | None = None
    forks: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    franchise: Franchise | None = None
    franchises: Annotated[TupleOf[Franchise], RelationshipDef("IgdbFranchise.id")] = ()
    game_engines: Annotated[TupleOf[GameEngine], RelationshipDef("IgdbGameEngine.id")] = ()
    game_localizations: Annotated[TupleOf[GameLocalization], RelationshipDef("IgdbGameLocalization.id")] = ()
    game_modes: Annotated[TupleOf[GameMode], RelationshipDef("IgdbGameMode.id")] = ()
    game_status: GameStatus | None = None
    game_type: GameType | None = None
    genres: Annotated[TupleOf[Genre], RelationshipDef("IgdbGenre.id")] = ()
    involved_companies: Annotated[TupleOf[InvolvedCompany], RelationshipDef("IgdbInvolvedCompany.id")] = ()
    keywords: Annotated[TupleOf[Keyword], RelationshipDef("IgdbKeyword.id")] = ()
    language_supports: Annotated[TupleOf[LanguageSupport], RelationshipDef("IgdbLanguageSupport.id")] = ()
    multiplayer_modes: Annotated[TupleOf[MultiplayerMode], RelationshipDef("IgdbMultiplayerMode.id")] = ()
    name: str
    parent_game: Annotated[IgdbId | None, ColumnDef(type=ForeignKey('IgdbGame.id'))] = None
    platforms: Annotated[TupleOf[Platform], RelationshipDef("IgdbPlatform.id")] = ()
    player_perspectives: Annotated[TupleOf[PlayerPerspective], RelationshipDef("IgdbPlayerPerspective.id")] = ()
    ports: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    release_dates: Annotated[TupleOf[ReleaseDate], RelationshipDef("IgdbReleaseDate.id")] = ()
    remakes: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    remasters: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    standalone_expansions: Annotated[TupleOf[IgdbId], RelationshipDef('IgdbGame.id')] = ()
    themes: Annotated[TupleOf[Theme], RelationshipDef("IgdbTheme.id")] = ()
    total_rating: float | None = None
    total_rating_count: int | None = None
    url: CoercedHttpUrl | None = None
    version_parent: Annotated[IgdbId | None, ColumnDef(type=ForeignKey('IgdbGame.id'))] = None
    version_title: str | None = None


DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_category.organization",
    "age_ratings.rating_content_descriptions.description_type.name",
    "age_ratings.rating_content_descriptions.description",
    "age_ratings.rating_content_descriptions.organization",
    "aggregated_rating_count",
    "aggregated_rating",
    "alternative_names.comment",
    "alternative_names.game",
    "alternative_names.name",
    "bundles",
    "dlcs",
    "expanded_games",
    "expansions",
    "first_release_date",
    "forks",
    "franchise.name",
    "franchises.name",
    "game_engines.name",
    "game_localizations.game",
    "game_localizations.name",
    "game_localizations.region.category",
    "game_localizations.region.identifier",
    "game_localizations.region.name",
    "game_modes.name",
    "game_status.status",
    "game_type.type",
    "genres.name",
    "involved_companies.company.country",
    "involved_companies.company.name",
    "involved_companies.company.status.name",
    "involved_companies.developer",
    "involved_companies.game",
    "involved_companies.porting",
    "involved_companies.publisher",
    "involved_companies.supporting",
    "keywords.name",
    "language_supports.language_support_type.name",
    "language_supports.game",
    "language_supports.language.locale",
    "language_supports.language.name",
    "multiplayer_modes.campaigncoop",
    "multiplayer_modes.dropin",
    "multiplayer_modes.game",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.offlinecoop",
    "multiplayer_modes.offlinecoopmax",
    "multiplayer_modes.offlinemax",
    "multiplayer_modes.onlinecoop",
    "multiplayer_modes.onlinecoopmax",
    "multiplayer_modes.onlinemax",
    "multiplayer_modes.platform",
    "multiplayer_modes.splitscreen",
    "multiplayer_modes.splitscreenonline",
    "name",
    "parent_game",
    "platforms.alternative_name",
    "platforms.generation",
    "platforms.name",
    "platforms.platform_family.name",
    "platforms.platform_type.name",
    "player_perspectives.name",
    "ports",
    "release_dates.date_format.format",
    "release_dates.date",
    "release_dates.game",
    "release_dates.human",
    "release_dates.m",
    "release_dates.platform",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "release_dates.y",
    "remakes",
    "remasters",
    "standalone_expansions",
    "themes.name",
    "url",
    "version_parent",
    "version_title",
)

IGDB_OBJECT_TYPES = (
    AgeRatingOrganization,
    AgeRatingCategory,
    AgeRatingContentDescriptionType,
    AgeRatingContentDescriptionV2,
    AgeRating,
    AlternativeName,
    Franchise,
    GameEngine,
    GameLocalization,
    GameMode,
    GameStatus,
    GameType,
    Genre,
    CompanyStatus,
    Company,
    InvolvedCompany,
    Region,
    Keyword,
    Language,
    LanguageSupportType,
    LanguageSupport,
    PlatformFamily,
    PlatformType,
    PlatformVersion,
    Platform,
    MultiplayerMode,
    PlayerPerspective,
    DateFormat,
    ReleaseDateRegion,
    ReleaseDateStatus,
    ReleaseDate,
    Theme,
    Game,
)

type SortDirection = Literal['asc', 'desc']
DEFAULT_SORT: tuple[str, SortDirection] = ('name', 'asc')
QUERY_CLAUSE = r'(fields|f|exclude|x|where|w|limit|l|offset|o|sort|s|search)\s+([^;]+)\s*;'

class GameResponse(TypedDict, total=False):
    name: Required[str]

class MultiqueryResult(TypedDict):
    name: str
    count: NotRequired[int]
    result: NotRequired[GameResponse]

type MultiqueryResponse = list[MultiqueryResult]

GameResponseAdapter = TypeAdapter(GameResponse)
MultiqueryResponseAdapter = TypeAdapter(MultiqueryResponse)
MultiqueryResponseListAdapter = TypeAdapter(list[list[dict[str, JsonValue]]])

class CountResponse(TypedDict):
    count: int

@dataclass(kw_only=True, eq=True)
class Query:
    fields: Optional[tuple[str, ...]]
    exclude: Optional[tuple[str, ...]]
    where: Optional[str]
    limit: int
    offset: int
    sort: Optional[tuple[str, SortDirection]]
    search: Optional[str]

    def __init__(
            self,
            query: Optional[str] = None,
            *, # Force keyword arguments for clarity
            fields: Optional[Iterable[str] | str] = "*",
            exclude: Optional[Iterable[str] | str] = None,
            where: Optional[str] = None,
            limit: int = 10, # IGDB's default
            offset: int = 0, # IGDB's default
            sort: Optional[tuple[str, SortDirection]] = None,
            search: Optional[str] = None,
    ) -> None:
        if query is not None:
            # If given a query string, use it to override all other parameters.
            for match in re.finditer(QUERY_CLAUSE, query.strip(), re.IGNORECASE):
                clause_name = match.group(1).lower()
                clause_value = match.group(2).strip()

                match clause_name:
                    case 'fields' | 'f':
                        fields = tuple(f.strip() for f in clause_value.split(',') if f.strip())
                    case 'exclude' | 'x':
                        exclude = tuple(f.strip() for f in clause_value.split(',') if f.strip())
                    case 'where' | 'w':
                        where = clause_value
                    case 'limit' | 'l':
                        limit = int(clause_value)
                    case 'offset' | 'o':
                        offset = int(clause_value)
                    case 'sort' | 's':
                        # Parse sort field and direction
                        sort_parts = clause_value.split()
                        if len(sort_parts) >= 1:
                            sort_field: str = sort_parts[0]
                        else:
                            raise ValueError("Sort clause must specify a field")

                        if len(sort_parts) >= 2:
                            sort_direction = cast(SortDirection, sort_parts[1].lower().strip())
                            if sort_direction not in ('asc', 'desc'):
                                raise ValueError("Sort direction must be 'asc' or 'desc'")
                        else:
                            sort_direction = 'asc' # Default to ascending if not specified

                        sort = (sort_field, sort_direction)
                    case 'search':
                        search = clause_value.strip()

        match fields:
            case str():
                self.fields = tuple(f.strip(" ;") for f in fields.split(",") if f)
            case Iterable():
                self.fields = tuple(f.strip(" ;") for f in fields if f)
            case None:
                self.fields = None
            case _:
                raise TypeError(f"Expected fields to be str, Iterable[str], or None; got {type(fields).__name__}")

        match exclude:
            case str():
                self.exclude = tuple(f.strip() for f in exclude.split(","))
            case Iterable():
                self.exclude = tuple(f.strip() for f in exclude)
            case None:
                self.exclude = None
            case _:
                raise TypeError(f"Expected exclude to be str, Iterable[str], or None; got {type(exclude).__name__}")

        # TODO: Come up with some strongly-typed way to handle the `where` clause.
        #  (Gotta handle ANDs, ORs, NOTs, operators, etc.)
        match where:
            case str():
                self.where = where.strip()
            case None:
                self.where = None
            case _:
                raise TypeError(f"Expected where to be str or None; got {type(where).__name__}")

        self.limit = limit
        self.offset = offset

        if search and sort:
            raise ValueError("Cannot specify both search and sort in a query.")

        self.search = search
        self.sort = sort

    def query_pages(self, count: int, limit: int = 500) -> Iterator['Query']:
        for i in range(0, count, limit):
            yield Query(
                fields=self.fields,
                exclude=self.exclude,
                where=self.where,
                limit=limit,
                offset=i,
                sort=self.sort,
                search=self.search,
            )

    def __str__(self) -> str:
        clauses: list[str] = []
        if self.fields:
            clauses.append(f"fields {','.join(self.fields)};")

        if self.exclude:
            clauses.append(f"exclude {','.join(self.exclude)};")

        if self.where:
            clauses.append(f"where {self.where};")

        if self.limit is not None:
            clauses.append(f"limit {self.limit};")

        if self.offset is not None:
            clauses.append(f"offset {self.offset};")

        if self.sort:
            clauses.append(f"sort {self.sort[0]} {self.sort[1]};")

        if self.search:
            clauses.append(f"search \"{self.search}\";")

        return ''.join(clauses)

@dataclass
class Playlist:
    title: PlaylistTitle
    '''
    The title of the playlist,
    which is used as the filename for the playlist file.
    Usually follows the format "Manufacturer - Platform Name".
    '''

    alts: Sequence[str]
    '''
    Other names that the playlist might be known by.
    '''

    query: Query
    '''
    The IGDB query to use to fetch games for this playlist.
    '''

    hasheous_dirs: Sequence[str]
    '''
    The names of zero or more Hasheous dump files, excluding the zip suffix.
    '''

    def __init__(
            self,
            title: PlaylistTitle,
            hasheous: Optional[str | Iterable[str]] = None,
            alts: Optional[str | Iterable[str]] = None,
            *, # Force keyword arguments for clarity
            fields: Optional[Iterable[str] | str] = DEFAULT_GAME_FIELD_TUPLE,
            exclude: Optional[Iterable[str] | str] = None,
            where: Optional[str] = None,
            limit: int = 500,
            offset: int = 0,
            sort: Optional[tuple[str, SortDirection]] = DEFAULT_SORT,
            search: Optional[str] = None,
    ):
        self.title = title

        self.query = Query(
            fields=fields,
            exclude=exclude,
            where=where,
            limit=limit,
            offset=offset,
            sort=sort,
            search=search,
        )

        match hasheous:
            case str():
                self.hasheous_dirs = (hasheous,)
            case Iterable():
                self.hasheous_dirs = tuple(hasheous)
            case None:
                self.hasheous_dirs = ()
            case _:
                raise TypeError(f"Expected hasheous to be str, Iterable[str], or None; got {type(hasheous).__name__}")

        match alts:
            case str():
                self.alts = (alts,)
            case Iterable():
                self.alts = tuple(alts)
            case None:
                self.alts = ()
            case _:
                raise TypeError(f"Expected alts to be str, Iterable[str], or None; got {type(alts).__name__}")


    def query_pages(self, count: int, limit: int = 500) -> Iterator[Query]:
        for i in range(0, count, limit):
            yield Query(
                fields=self.query.fields,
                exclude=self.query.exclude,
                where=self.query.where,
                limit=limit,
                offset=i,
                sort=self.query.sort,
                search=self.query.search,
            )

MULTIQUERY_MAX = 10
MULTIQUERY_LIMIT = MULTIQUERY_MAX
'''
The maximum number of queries that IGDB allows in a single multiquery.
'''

MAX_ACTIVE_QUERIES = 8

MAX_QUERY_RATE = 4
MAX_QUERY_PERIOD = 1.0 / MAX_QUERY_RATE


class Multiquery:
    def __init__(self, queries: Mapping[str, tuple[str, Query]]):
        if len(queries) > MULTIQUERY_LIMIT:
            raise ValueError(f"Multiquery can only contain up to {MULTIQUERY_LIMIT} queries; got {len(queries)}")

        self.queries = dict(queries)

    def __str__(self) -> str:
        queries: list[str] = []
        for name, (endpoint, query) in self.queries.items():
            queries.append(f"query {endpoint} \"{name}\" {{ {query} }};")

        return '\n'.join(queries)

RETRY_CODES = (
    httpx.codes.REQUEST_TIMEOUT,
    httpx.codes.TOO_MANY_REQUESTS,
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.SERVICE_UNAVAILABLE,
    httpx.codes.GATEWAY_TIMEOUT,
)

class QueryClient:
    def __init__(self, client_id: str, client_secret: str, max_queries: int = MAX_ACTIVE_QUERIES, max_rate: int = MAX_QUERY_RATE):
        # Limit to 8 in-flight requests
        self.request_limit = asyncio.BoundedSemaphore(max_queries)
        self.rate_limit = asynciolimiter.StrictLimiter(max_rate)
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = AsyncOAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint='https://id.twitch.tv/oauth2/token',
            token_endpoint_auth_method='client_secret_post',
            scope=['user_read', 'user_subscriptions'],
        )

    async def __aenter__(self) -> Self:
        try:
            client = await self.client.__aenter__()

            token: OAuth2Token = await self.client.fetch_token(
                grant_type='client_credentials',
                url='https://id.twitch.tv/oauth2/token',
                client_id=self.client_id,
                client_secret=self.client_secret,
            )

            if not token:
                raise RuntimeError("Failed to obtain access token from Twitch")

            return self
        except:
            await self.client.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.client.__aexit__(exc_type, exc_val, exc_tb)


    @staticmethod
    def _on_backoff(details):
        print("Retrying after backoff:", details['target'].__name__, "with args:", details['args'], "and kwargs:", details['kwargs'], file=sys.stderr)

    @staticmethod
    def _on_predicate(response: Response) -> bool:
        status = response.status_code
        if status in RETRY_CODES:
            # If the response is a retryable error, we want to retry
            print(f"Retrying due to status code {status} ({response.reason_phrase})", file=sys.stderr)
            return True

        return False


    @staticmethod
    def _giveup(e: Exception):
        if not isinstance(e, HTTPStatusError):
            # Give up if query_endpoint failed with something besides HTTPStatusError
            return True

        if e.response.status_code in RETRY_CODES:
            # Don't give up on server errors (5xx), we might just be unlucky
            # or rate-limited (429), so we should back off and retry.
            return False

        return e.response.is_error

    @backoff.on_exception(backoff.expo, HTTPStatusError, max_tries=5, giveup=_giveup, on_backoff=_on_backoff)
    @backoff.on_predicate(backoff.expo, _on_predicate)
    async def _query(self, endpoint: str, query: str | Query | Multiquery) -> Response:
        """
        Query the IGDB API with the given endpoint and query.

        Args:
            client: The authenticated AsyncOAuth2Client instance
            endpoint: The IGDB API endpoint to query
            query: The Apicalypse query string to send to the endpoint

        Returns:
            The HTTP response from the API

        Raises:
            requests.exceptions.RequestException: If the request fails
        """

        async with self.request_limit:
            url = f"https://api.igdb.com/v4/{endpoint}"
            access_token = self.client.token["access_token"]

            headers = {
                'Client-ID': self.client.client_id,
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
                'Accept-Encoding': 'gzip, deflate'
            }

            await self.rate_limit.wait()
            response = await self.client.post(url, headers=headers, content=str(query), timeout=Timeout(None))
            response.raise_for_status()

            content_type = response.headers.get("Content-Type")

            if response.headers.get('content-type') != 'application/json':
                raise ValueError(f"Expected IGDB query response to be JSON, got: {content_type} ({response.text})")

            return response

    @overload
    async def query(self, endpoint: Literal["multiquery"], query: str | Multiquery) -> JsonValue: ...

    @overload
    async def query(self, endpoint: Literal["multiquery"], query: Query) -> Never: ...

    @overload
    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonValue: ...

    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonValue:
        if endpoint == "multiquery" and isinstance(query, Query):
            raise TypeError("Expected a str or Multiquery for 'multiquery' endpoint; got Query")

        try:
            response = await self._query(endpoint, query)
            return from_json(response.content)
        except HTTPStatusError as e:
            if not (isinstance(query, Multiquery) and e.response.status_code == httpx.codes.REQUEST_ENTITY_TOO_LARGE):
                # If the error is not due to multiquery size limit, re-raise
                raise

            print(f"Multiquery too large (HTTP 413); splitting into {len(query.queries)} individual queries...", file=sys.stderr)

            return await self._split_multiquery(query)
            # MultiqueryResponse is a list[TypedDict], which is suitable as a JsonValue

    async def _split_multiquery(self, multiquery: Multiquery) -> list[JsonValue]:
        tasks: list[Task[JsonValue]] = []

        async with asyncio.TaskGroup() as group:
            for name, (query_endpoint, query_obj) in multiquery.queries.items():
                tasks.append(group.create_task(
                    self.query(query_endpoint, query_obj),
                    name=name
                ))

            # Wait for all individual queries to complete
            # This is better than sequential execution because we can still benefit from concurrency
            results = await asyncio.gather(*tasks)
            print(f"Completed {len(results)} individual queries (split from oversized multiquery)", file=sys.stderr)
            response: list[JsonValue] = []
            for task in tasks:
                response.append({
                    "name": task.get_name(),
                    "result": task.result(),
                })

            return response

    async def count(self, endpoint: str, query: str | Query) -> int:
        if not endpoint.endswith('/count'):
            endpoint += '/count'

        response = await self._query(endpoint, query)
        response_json = from_json(response.content)

        if not isinstance(response_json, Mapping):
            raise ValueError(f"Expected {endpoint} response to be a JSON object, got {type(response_json)} ({response_json})")

        if 'count' not in response_json:
            # If the response is successful yet wrong, raise a ValueError
            raise ValueError(f"Expected a 'count' attribute in response from {endpoint}, got {response_json}")

        count = response_json['count']
        if not isinstance(count, int):
            raise ValueError(f"Expected response['count'] to be a number, got {type(count)}")

        return int(count)

def read_playlists(path: str) -> tuple[Playlist, ...]:
    class TomlPlaylistEntry(TypedDict):
        title: PlaylistTitle
        hasheous: Sequence[str]
        alts: Sequence[str]
        where: str

    with open(path, "rb") as playlist_file:
        toml = tomllib.load(playlist_file)

        if not (igdb := toml.get('igdb')):
            raise KeyError(f"Missing 'igdb' section in TOML file at {path}")

        if not (playlists := igdb.get('playlists')):
            raise KeyError(f"Missing 'playlists' array in 'igdb' table of TOML file at {path}")

        if not isinstance(playlists, list):
            raise TypeError(f"Expected 'playlists' to be a list; got {type(playlists).__name__}")

        playlist_objects = cast(Sequence[TomlPlaylistEntry], playlists)

        def load_playlist(entry: TomlPlaylistEntry) -> Playlist:
            # We use a separate function so that the Playlist is hashable
            # (as tomllib loads into mutable dicts and lists)
            return Playlist(
                title=entry['title'],
                hasheous=tuple(entry.get('hasheous', ())),
                alts=tuple(entry.get('alts', ())),
                where=entry['where'],
            )
        return tuple(load_playlist(p) for p in playlist_objects)


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

PLAYLISTS = read_playlists(TOML_PATH)

PLAYLISTS_BY_TITLE = {str(p.title): p for p in PLAYLISTS}
PLAYLISTS_BY_TITLE_LOWER = {p.title.lower(): p for p in PLAYLISTS}
PLAYLISTS_BY_ANY: Mapping[str, Playlist] = ChainMap(
    PLAYLISTS_BY_TITLE,
    PLAYLISTS_BY_TITLE_LOWER,
)
PLAYLIST_TITLES = tuple(p.title for p in PLAYLISTS)

ANALOG_KEYWORD_IDS = (
    4965, # circle pad pro support
    10740, # gamecube
    11394, # gamecube controller support on wii
    45794, # n64 controller supported
    48530, # input type - dial controls
)

KEYWORD_OVERRIDES = {
    1173: 27627, # "james bond" -> "007"
    42455: 893, # "1500s" -> "16th Century"
    3552: 535, # "1990's" -> "1990s"
    41008: 50517, # "1bit" -> "1-bit"
    44939: 589, # "25d" -> "2.5d"
    3696: 25392, # "2d fighter" -> "2d fighting"
    47978: 3014, # "2d-side-scroller" -> "2d platformer"

    18448: 2231, # "3d platform" -> "3d platformer"
    16964: 128, # "80s" -> "1980s"
    7294: 128, # "the 1980s" -> "1980s"

    50684: 3079, # "8bit" -> "8-bit"

}

RUMBLE_KEYWORD_IDS = (
    8156, # contextual controller rumble
    50485, # game boy player rumble support
    46206, # nintendo ds rumble pak
    10564, # rumble cartridge
    27048, # rumble pak
    38907, # rumble support
)

@cache
def get_playlist(identifier: str | Path) -> Optional[Playlist]:
    """
    Look up a playlist in `PLAYLISTS` by its title, path, or alternative name.
    """

    if isinstance(identifier, Path):
        playlist_id = identifier.stem.lower()
    else:
        playlist_id = identifier.strip().lower()

    for playlist in PLAYLISTS:
        if playlist_id == playlist.title.lower():
            return playlist

        if any(playlist_id == alt.lower() for alt in playlist.alts):
            return playlist

        if any(playlist_id == h.lower() for h in playlist.hasheous_dirs):
            return playlist

    return None


def get_by_title(title: str) -> Optional[Playlist]:
    """
    Get a playlist by its title.

    :param title: The title of the playlist to search for.
    :return: The Playlist object if found, otherwise None.
    """

    for playlist in PLAYLISTS:
        if playlist.title.lower() == title.lower():
            return playlist

    return None

def get_client_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    return client_id, client_secret


def load_file(path: Path, playlist: Playlist) -> tuple[PlaylistTitle, Collection[Game]]:
    with open(path, mode='rb') as infile:
        json_bytes = infile.read()
        games = GameTupleAdapter.validate_json(json_bytes, extra='allow')
        return playlist.title, games

class IgdbIndex:
    def __init__(self, games: Iterable[tuple[PlaylistTitle, Iterable[Game]]]):
        playlists_iterators = dict(games)
        playlists = {title: tuple(obj_iter) for title, obj_iter in playlists_iterators.items()}
        self.by_playlist = playlists
        self.by_id: dict[IgdbId, Game] = {}

        for game in chain.from_iterable(playlists.values()):
            self.by_id[game.id] = game

    @property
    def by_igdb_id(self):
        return self.by_id

async def load_games(playlists: Mapping[Path, Playlist], executor: Executor) -> IgdbIndex:
    """
    :param playlists: An iterable of tuples,
    where each tuple contains the path to a playlist file
    and the corresponding Playlist object.

    :return: A mapping of playlist titles to collections of the Games they represent.
    """

    loop = asyncio.get_running_loop()
    futures = (loop.run_in_executor(executor, load_file, k, v) for (k, v) in playlists.items())

    return IgdbIndex(await asyncio.gather(*futures))


GameTupleAdapter = TypeAdapter(tuple[Game, ...])

async def load_game_file(path: Path, playlist: Playlist) -> tuple[PlaylistTitle, Collection[Game]]:
    async with aiofiles.open(path, mode='rb') as infile:
        json_bytes = await infile.read()
        games = GameTupleAdapter.validate_json(json_bytes, extra='allow')
        return playlist.title, games

async def handle_query(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""

    all_records = bool(args.all)
    verbose = bool(args.verbose)

    if args.endpoint == "multiquery":
        # Read multiquery definitions from file or stdin
        if args.query == '-':
            body = sys.stdin.read()
        else:
            with open(args.query, 'r') as f:
                body = f.read()
    else:
        body = args.query

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        try:
            if not all_records:
                # If the user didn't pass the --all flag...
                response = await client.query(args.endpoint, body)
                json = to_json(response, indent=2)
                await aiofiles.stdout_bytes.write(json)
            else:
                count_response = cast(CountResponse, await client.query(f"{args.endpoint}/count", body))
                count = count_response["count"]
                if verbose:
                    print(f"Query will return {count} total records", file=sys.stderr)

                query = Query(body)
                async with asyncio.TaskGroup() as group:
                    tasks: list[Task[JsonValue]] = []
                    for q in batched(query.query_pages(count), MULTIQUERY_MAX):
                        if verbose:
                            print(f"Fetching records {q[0].offset} to {q[-1].offset + q[-1].limit - 1}", file=sys.stderr)

                        multiquery = Multiquery({f"{args.endpoint} ({p.offset}-{p.offset + p.limit - 1})": (args.endpoint, p) for p in q})
                        task = group.create_task(client.query("multiquery", multiquery))
                        tasks.append(task)

                    responses = await asyncio.gather(*tasks)

                multiquery_responses = MultiqueryResponseListAdapter.validate_python(responses, extra='allow')
                results = tuple(chain.from_iterable(multiquery_responses))
                json = to_json(results, indent=2)
                await aiofiles.stdout_bytes.write(json)
        except JSONDecodeError as e:
            print(e.doc, file=sys.stderr)
            print(e, file=sys.stderr)
            raise e


async def handle_fetch(args: argparse.Namespace) -> None:
    """Handle the fetch subcommand."""

    playlist_args: Iterable[str] | None = args.playlist
    if not playlist_args:
        # If no playlists specified, use all known playlists
        playlist_args = (p.title for p in PLAYLISTS)

    # Get all playlists to scrape (filter out the Nones)
    playlists = tuple(filter(None, (get_playlist(p) for p in playlist_args)))
    if not playlists:
        raise ValueError("All listed playlists are unknown.")

    outdir: str = args.outdir

    await aiofiles.os.makedirs(outdir, exist_ok=True)

    async def fetch_playlist(client: QueryClient, playlist: Playlist, group: TaskGroup) -> Sequence[GameResponse]:
        print(f"{playlist.title}: Fetching game count in query...")
        count = await client.count("games", playlist.query)

        multiqueries: list[Multiquery] = []
        for batch in batched(playlist.query_pages(count), MULTIQUERY_MAX):
            multiqueries.append(Multiquery({f"{playlist.title} ({q.offset}-{q.offset + q.limit - 1})": ('games', q) for q in batch}))

        playlist_tasks = tuple(group.create_task(client.query("multiquery", m)) for m in multiqueries)
        print(f"{playlist.title}: Scheduled to fetch {count} games...")

        responses = await asyncio.gather(*playlist_tasks)
        multiquery_responses = MultiqueryResponseListAdapter.validate_python(responses, extra='allow')

        games: list[GameResponse] = []
        for r in chain.from_iterable(multiquery_responses):
            if 'result' in r:
                games.extend(r['result']) # type: ignore (because we're checking for the result key)
                # We're not processing the returned games except to sort them,
                # so we don't need to convert them to IgdbGame objects here.

        print(f"{playlist.title}: Fetched {len(games)} games.")
        games.sort(key=lambda g: g['name'])
        # Now that we have all the games, sort them by name

        # Create the output directory if it doesn't exist
        await aiofiles.os.makedirs(outdir, exist_ok=True)
        outpath = os.path.join(outdir, f"{playlist.title}.json")
        async with aiofiles.open(outpath, 'wb') as outfile:
            json = to_json(games, indent=2)
            await outfile.write(json)
            print(f"{playlist.title}: Saved {len(games)} games to {outpath}")

        return games

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        async with asyncio.TaskGroup() as group:
            tasks = tuple(group.create_task(fetch_playlist(client, p, group), name=p.title) for p in playlists)

def main():
    """Main entry point for the script."""

    parser = argparse.ArgumentParser(
        description="Utilities for fetching and processing data from IGDB.",
        epilog="See https://api-docs.igdb.com for more information about the IGDB API and its query syntax."
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show more logging output"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # Query subcommand
    query_parser = subparsers.add_parser(
        "query",
        help="Make a request to an IGDB API endpoint and print the response to stdout."
    )
    query_parser.add_argument(
        "--client-id",
        type=str,
        help="Your IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided.",
        default=None,
    )
    query_parser.add_argument(
        "--client-secret",
        type=str,
        help="Your IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    query_parser.add_argument(
        "--all",
        action="store_true",
        help="Use this query, but ignore the 'offset'/'limit' clauses and fetch all results."
    )
    query_parser.add_argument(
        "endpoint",
        type=str,
        help="The IGDB API endpoint to query."
    )
    query_parser.add_argument(
        "query",
        type=str,
        help="The Apicalypse query to query data from. If 'endpoint' is 'multiquery', this should be a path to a query file or '-' to read from stdin."
    )
    query_parser.set_defaults(func=handle_query)

    # fetch subcommand
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch data from IGDB and save it to the specified directory"
    )
    fetch_parser.add_argument(
        "--client-id",
        type=str,
        help="The IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided."
    )
    fetch_parser.add_argument(
        "--client-secret",
        type=str,
        help="The IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    fetch_parser.add_argument(
        "--playlist",
        type=str,
        help="The titles of the playlists to scrape. If not provided, all known playlists will be scraped.",
        action="extend",
        nargs="*",
        default=PLAYLISTS_BY_TITLE.keys()  # Default to all known playlists
    )
    fetch_parser.add_argument(
        "outdir",
        type=str,
        help="The output directory for the scraped JSON files",
        default="tmp/igdb",
    )
    fetch_parser.set_defaults(func=handle_fetch)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = (
    "AgeRating",
    "AgeRatingCategory",
    "AgeRatingContentDescriptionType",
    "AgeRatingContentDescriptionV2",
    "AgeRatingOrganization",
    "AlternativeName",
    "ANALOG_KEYWORD_IDS",
    "ColumnDef",
    "Company",
    "CompanyStatus",
    "DateFormat",
    "DEFAULT_GAME_FIELD_TUPLE",
    "DEFAULT_SORT",
    "Franchise",
    "Game",
    "GameEngine",
    "GameLocalization",
    "GameMode",
    "GameStatus",
    "GameType",
    "Genre",
    "get_by_title",
    "get_playlist",
    "IgdbIndex",
    "IgdbId",
    "IgdbObject",
    "IGDB_OBJECT_TYPES",
    "InvolvedCompany",
    "Keyword",
    "Language",
    "LanguageSupport",
    "LanguageSupportType",
    "load_games",
    "MAX_ACTIVE_QUERIES",
    "MAX_QUERY_PERIOD",
    "MAX_QUERY_RATE",
    "MultiplayerMode",
    "MULTIQUERY_MAX",
    "Multiquery",
    "Platform",
    "PlatformFamily",
    "PlatformType",
    "PlatformVersion",
    "PlayerPerspective",
    "PLAYLIST_TITLES",
    "Playlist",
    "PLAYLISTS_BY_TITLE",
    "PLAYLISTS",
    "PlaylistTitle",
    "Query",
    "QueryClient",
    "Region",
    "RelationshipDef",
    "ReleaseDate",
    "ReleaseDateRegion",
    "ReleaseDateStatus",
    "RUMBLE_KEYWORD_IDS",
    "SortDirection",
    "SchemaDef",
    "Theme",
)

if __name__ == "__main__":
    main()
