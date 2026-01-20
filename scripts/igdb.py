#!/usr/bin/env python3

import asyncio
import dataclasses
import os.path
import re
import sys
import tomllib

from abc import ABC
from asyncio import Task, TaskGroup
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Annotated, Any, ClassVar, Never, NotRequired, Literal, NewType, Required, Self, TypedDict, cast, overload

import aiofiles
import aiofiles.os
import asynciolimiter
import backoff
import httpx

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import HTTPStatusError, Response, Timeout
from more_itertools import batched, spy
from pydantic import AliasChoices, BaseModel, BeforeValidator, Field, FieldSerializationInfo, FilePath, SerializerFunctionWrapHandler, TypeAdapter, JsonValue, WrapValidator, computed_field, field_serializer
from pydantic_core import from_json, to_json
from pydantic_extra_types.country import CountryNumericCode
from pydantic_settings import BaseSettings, CliApp, CliPositionalArg, CliSubCommand, SettingsConfigDict
from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.sqlite import INTEGER
from sqlalchemy.util import immutabledict

from utils import CoercedHttpUrl, DatabaseModel, RelationshipTableDef

IgdbId = NewType('IgdbId', int)
IgdbPrimaryId = Annotated[
    IgdbId,
    Column(INTEGER, primary_key=True, autoincrement=False, )
]
PlaylistTitle = NewType('PlaylistTitle', str)

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
type IgdbObjectSerializeMode = Literal['row'] | None


class IgdbObject(DatabaseModel, ABC, frozen=True):
    __tablename__: ClassVar[str] # type: ignore
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
            case (_, _):
                # Otherwise, run the default serializer to handle other types or contexts
                return handler(value)

GameReference = Annotated[IgdbId, Column(ForeignKey('IgdbGame.id'))]
class AgeRatingOrganization(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingOrganization"
    id: IgdbPrimaryId
    name: str

class AgeRatingCategory(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRatingCategory"

    id: IgdbPrimaryId
    organization: Annotated[IgdbId, Column(ForeignKey('IgdbAgeRatingOrganization.id'))]
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
    organization: Annotated[IgdbId, Column(ForeignKey('IgdbAgeRatingOrganization.id'))]

class AgeRating(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAgeRating"
    id: IgdbPrimaryId
    organization: AgeRatingOrganization
    rating_category: AgeRatingCategory
    rating_content_descriptions: tuple[AgeRatingContentDescriptionV2, ...] = ()

class AlternativeName(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbAlternativeName"
    id: IgdbPrimaryId
    name: str
    comment: str | None = None
    game: GameReference

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
    game: GameReference
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
    game: GameReference
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
    game: GameReference
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
    game: GameReference
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: int | None = None
    offlinemax: int | None = None
    onlinecoop: bool
    onlinecoopmax: int | None = None
    onlinemax: int | None = None
    platform: Annotated[IgdbId | None, Column(ForeignKey('IgdbPlatform.id'))] = None
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
    game: GameReference
    human: str
    m: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] | None = None  # Month (1-12)
    platform: Annotated[IgdbId, Column(ForeignKey('IgdbPlatform.id'))]
    release_region: ReleaseDateRegion
    status: ReleaseDateStatus | None = None
    y: int | None = None  # Year

class Theme(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbTheme"
    id: IgdbPrimaryId
    name: str

GameReferenceTuple = Annotated[
    tuple[IgdbId, ...],
    RelationshipTableDef(
        related_columns=(
            Column(
                "related_game",
                ForeignKey('IgdbGame.id'),
                primary_key=True
            ),
        )
    )
]

class Game(IgdbObject, frozen=True):
    __tablename__: ClassVar[str] = "IgdbGame"
    id: IgdbPrimaryId
    age_ratings: tuple[AgeRating, ...] = ()
    aggregated_rating: float | None = None
    aggregated_rating_count: int | None = None
    alternative_names: tuple[AlternativeName, ...] = ()
    bundles: GameReferenceTuple = ()
    collections: GameReferenceTuple = ()
    dlcs: GameReferenceTuple = ()
    expanded_games: GameReferenceTuple = ()
    expansions: GameReferenceTuple = ()
    first_release_date: date | None = None
    forks: GameReferenceTuple = ()
    franchise: Franchise | None = None
    franchises: tuple[Franchise, ...] = ()
    game_engines: tuple[GameEngine, ...] = ()
    game_localizations: tuple[GameLocalization, ...] = ()
    game_modes: tuple[GameMode, ...] = ()
    game_status: GameStatus | None = None
    game_type: GameType | None = None
    genres: tuple[Genre, ...] = ()
    involved_companies: tuple[InvolvedCompany, ...] = ()
    keywords: tuple[Keyword, ...] = ()
    language_supports: tuple[LanguageSupport, ...] = ()
    multiplayer_modes: tuple[MultiplayerMode, ...] = ()
    name: str
    parent_game: Annotated[IgdbId | None, Column(ForeignKey('IgdbGame.id'))] = None
    platforms: tuple[Platform, ...] = ()
    player_perspectives: tuple[PlayerPerspective, ...] = ()
    ports: GameReferenceTuple = ()
    release_dates: tuple[ReleaseDate, ...] = ()
    remakes: GameReferenceTuple = ()
    remasters: GameReferenceTuple = ()
    standalone_expansions: GameReferenceTuple = ()
    themes: tuple[Theme, ...] = ()
    total_rating: float | None = None
    total_rating_count: int | None = None
    url: CoercedHttpUrl | None = None
    version_parent: Annotated[IgdbId | None, Column(ForeignKey('IgdbGame.id'),)] = None
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
    result: NotRequired[list[GameResponse]]

GameResponseAdapter = TypeAdapter(GameResponse)
MultiqueryResponse = list[MultiqueryResult]
MultiqueryResponseListAdapter = TypeAdapter(list[MultiqueryResponse])

class CountResponse(TypedDict):
    count: int

CountResponseAdapter = TypeAdapter(CountResponse)

@dataclass(kw_only=True, eq=True)
class Query:
    fields: tuple[str, ...] | None
    exclude: tuple[str, ...] | None
    where: str | None
    limit: int
    offset: int
    sort: tuple[str, SortDirection] | None
    search: str | None

    def __init__(
            self,
            query: str | None = None,
            *, # Force keyword arguments for clarity
            fields: Iterable[str] | str | None = "*",
            exclude: Iterable[str] | str | None = None,
            where: str | None = None,
            limit: int = 10, # IGDB's default
            offset: int = 0, # IGDB's default
            sort: tuple[str, SortDirection] | None = None,
            search: str | None = None,
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

    def expand_to_all(self, count: int, limit: int = 500) -> Iterator['Query']:
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

    @property
    def last(self) -> int:
        return self.offset + self.limit - 1

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

@dataclass()
class MultiqueryQuery(Query):
    name: str
    endpoint: str

    def __init__(self, name: str, endpoint: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'endpoint', endpoint)

def validate_igdb_query(value: Any, handler: SerializerFunctionWrapHandler) -> Query:
    match value:
        case str(s):
            return Query(query=s)
        case Query():
            return value
        case {"where": str(where), **rest}:
            kwargs = rest
            kwargs["where"] = where
            kwargs["fields"] = rest.get("fields", DEFAULT_GAME_FIELD_TUPLE)
            kwargs["exclude"] = rest.get('exclude', None)
            kwargs["limit"] = rest.get('limit', 500)
            kwargs["offset"] = rest.get('offset', 0)
            kwargs["sort"] = rest.get('sort', DEFAULT_SORT)
            kwargs["search"] = rest.get('search', None)
            return handler(kwargs)
        case _:
            return handler(value)

MAX_OBJECTS_PER_QUERY = 500

@dataclass(frozen=True)
class Playlist:
    """
    A playlist defines criteria for a set of games
    that will be aggregated into a single `.rdb` file.

    Conceptually similar to playlists in RetroArch,
    but this object doesn't list specific games;
    just criteria for aggregating their data.
    """

    title: PlaylistTitle
    '''
    The canonical title of the playlist, usually (but not necessarily)
    the name of a hardware manufacturer and platform.
    Used as the name of a generated `.dat` file and `.rdb` database.
    '''

    igdb_query: Annotated[
        Query,
        WrapValidator(validate_igdb_query),
        Field(validation_alias='igdb')
    ]
    '''
    The IGDB query to use to fetch games for this playlist.
    '''

    alts: tuple[str, ...] = ()
    '''
    Other names that may be used to address this playlist.

    Primarily used to identify `.dat` files from this repo
    that don't share the same name as the playlist title.
    '''

    hasheous_dirs: Annotated[tuple[str, ...], Field(validation_alias='hasheous')] = ()
    '''
    The names of zero or more Hasheous dump files, excluding the zip suffix.
    Passed to "https://hasheous.org/api/v1/Dumps/platforms/{name}".
    '''

    def expand_to_all(self, count: int, limit: int = MAX_OBJECTS_PER_QUERY) -> Iterator[MultiqueryQuery]:
        return map(lambda q: MultiqueryQuery(
            name=f"{self.title} ({q.offset}-{q.last})",
            endpoint="games",
            **dataclasses.asdict(q),
        ), self.igdb_query.expand_to_all(count, MAX_QUERIES_IN_MULTIQUERY))

class PlaylistConfig(BaseModel, frozen=True):
    playlists: tuple[Playlist, ...]

    @computed_field
    @cached_property
    def by_title(self) -> Mapping[PlaylistTitle, Playlist]:
        return immutabledict({pl.title: pl for pl in self.playlists})

MAX_QUERIES_IN_MULTIQUERY = 10
'''
The maximum number of queries that IGDB allows in a single multiquery.
'''

MAX_ACTIVE_QUERIES = 8

MAX_QUERY_RATE = 4
MAX_QUERY_PERIOD = 1.0 / MAX_QUERY_RATE

@dataclass
class Multiquery:
    queries: tuple[MultiqueryQuery, ...]

    def __init__(self, queries: Iterable[MultiqueryQuery]) -> None:
        head, _rest = spy(queries, MAX_QUERIES_IN_MULTIQUERY + 1)
        if len(head) > MAX_QUERIES_IN_MULTIQUERY:
            raise ValueError(f"Multiquery can only contain up to {MAX_QUERIES_IN_MULTIQUERY} queries; got more than {MAX_QUERIES_IN_MULTIQUERY}")

        setattr(self, 'queries', tuple(head))

    def __str__(self) -> str:
        return '\n'.join(f"query {q.endpoint} \"{q.name}\" {{ { q } }};" for q in self.queries)

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
            for query in multiquery.queries:
                tasks.append(group.create_task(
                    self.query(query.endpoint, query),
                    name=query.name
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

GameTupleAdapter = TypeAdapter(tuple[Game, ...])

async def load_game_file(path: Path) -> tuple[Game, ...]:
    async with aiofiles.open(path, mode='rb') as infile:
        json_bytes = await infile.read()
        games = GameTupleAdapter.validate_json(json_bytes, extra='allow')
        return games

class CommonArgs(BaseModel):
    client_id: str = Field(
        title="Twitch Client ID",
        description="""
            Your client ID for IGDB API access.
            See the IGDB API docs for more.
        """,
        validation_alias=AliasChoices('client-id', 'i'),
        min_length=1,
    )
    client_secret: str = Field(
        description="""
            Your client secret for IGDB API access.
            See the IGDB API docs for more.
        """,
        validation_alias=AliasChoices('client-secret', 's'),
        min_length=1,
    )
    verbose: bool = Field(
        default=False,
        description="Enable verbose output.",
        validation_alias=AliasChoices('v', 'verbose'),
    )

class QuerySubCommand(CommonArgs):
    """
    Execute an arbitrary Apicalypse query against the IGDB API
    and print the results as JSON to stdout.
    See https://api-docs.igdb.com for details.
    """

    all: bool = Field(
        default=False,
        description="""
            Ignore offset and limit clauses in the query,
            and fetch all matching records by issuing multiple requests.
            """
    )
    batch_size: int = Field(
        default=MAX_OBJECTS_PER_QUERY,
        description=f"""
            Fetch all objects matching the query in batches of this size.
            IGDB allows up to {MAX_OBJECTS_PER_QUERY} per request.
            """,
        ge=1,
        le=MAX_OBJECTS_PER_QUERY,
    )
    endpoint: CliPositionalArg[str] = Field(
        description="""
            The IGDB API endpoint to query.
            Baseurl is 'https://api.igdb.com/v4/'
            """,
        min_length=1,
    )
    query: CliPositionalArg[str | None] = Field(
        description="""
            The Apicalypse query to submit to IGDB.
            If 'endpoint' is 'multiquery',
            this may be a path to a query file
            or omitted to read from stdin.
            """
    )

    async def cli_cmd(self):
        """Handle the query subcommand."""

        match (self.endpoint, self.query):
            case (_, "" | None):
                # If an endpoint is given but no query, read from stdin
                body = await aiofiles.stdin.read()
            case ("multiquery", str(query_path)):
                # Read multiquery definitions from file
                async with aiofiles.open(query_path, 'r') as f:
                    body = await f.read()
            case (_, query):
                # Otherwise, send it to IGDB as-is
                body = query

        async with QueryClient(self.client_id, self.client_secret) as client:
            if not self.all:
                # If the user didn't pass the --all flag,
                # submit the query as-is and print the response.
                response = await client.query(self.endpoint, body)
                json = to_json(response, indent=2)
                await aiofiles.stdout_bytes.write(json)
            else:
                # If the user passed the --all flag,
                # ignore any offset and limit clauses in the query,
                # and fetch all matching records in batches.

                # First get the number of records this query would return
                count_response = CountResponseAdapter.validate_python(await client.query(f"{self.endpoint}/count", body), extra='allow')
                count = count_response['count']
                if self.verbose:
                    print(f"Query will return {count} total records", file=sys.stderr)

                async with asyncio.TaskGroup() as group:
                    # Expand our base query into multiple paged queries,
                    # and batch them further into multiqueries.

                    queries = map(lambda q: MultiqueryQuery(
                        name=f"{self.endpoint} ({q.offset}-{q.last})",
                        endpoint=self.endpoint,
                        **dataclasses.asdict(q),
                    ), Query(body).expand_to_all(count, self.batch_size))
                    batches = batched(queries, MAX_QUERIES_IN_MULTIQUERY)
                    multiqueries = (Multiquery(batch) for batch in batches)
                    tasks = (group.create_task(client.query("multiquery", m)) for m in multiqueries)
                    responses = await asyncio.gather(*tasks)

                # Validate the responses, concatenate all results, and print as JSON
                multiquery_responses = MultiqueryResponseListAdapter.validate_python(responses, extra='allow')
                results = tuple(chain.from_iterable(multiquery_responses))
                json = to_json(results, indent=2)
                await aiofiles.stdout_bytes.write(json)

class FetchSubCommand(CommonArgs):
    """
    Fetch game data from IGDB for one or more playlists
    and save the results as JSON files.

    Each JSON file will be named after the playlist title,
    and will contain all retrieved game objects sorted by game title.
    """

    config: FilePath = Field(
        default=Path(__file__).parent.parent / 'playlists.toml',
        title="Playlist Config File",
        description="Path to the config file that defines available playlists.",
        validation_alias=AliasChoices('c', 'config'),
        validate_default=True,
    )

    playlists: tuple[str, ...] = Field(
        default=(),
        description="""
            Query IGDB with the filters defined in playlist_config.
            Pass as -p '<playlist_title>' multiple times or once as -p '<playlist1>,<playlist2>,...'
            to fetch multiple playlists.
            If omitted, all playlists in the config that define an 'igdb.query' field will be fetched.
            Unrecognized playlist titles will be ignored.
        """,
        validation_alias=AliasChoices('p', 'playlists'),
        examples=[("Coleco - ColecoVision", "Dinothawr")]
    )

    outdir: CliPositionalArg[Path] = Field(
        default=Path(__file__).parent.parent / 'tmp' / 'igdb',
        description="""
            The output directory for the fetched JSON files.
            Will be created if it doesn't exist.
        """
    )

    async def cli_cmd(self):
        """Handle the fetch subcommand."""
        await aiofiles.os.makedirs(self.outdir, exist_ok=True)
        async with aiofiles.open(self.config, 'rb') as f:
            config = PlaylistConfig.model_validate(tomllib.load(f.raw))

        if self.playlists:
            # If specific playlists were requested, filter to those
            playlists = tuple(filter(None, (config.by_title.get(PlaylistTitle(p)) for p in self.playlists)))

            if not playlists:
                raise ValueError(f"None of the requested playlists are defined in {self.config}: {', '.join(self.playlists)}")
        else:
            # Otherwise, fetch all playlists that define an IGDB query
            playlists = tuple(p for p in config.by_title.values() if p.igdb_query)

            if not playlists:
                raise ValueError(f"No playlists in {self.config} define an IGDB query.")

        async def fetch_playlist(client: QueryClient, playlist: Playlist, group: TaskGroup):
            print(f"{playlist.title}: Fetching game count in query...")
            count = await client.count("games", playlist.igdb_query)
            print(f"{playlist.title}: Found {count} games matching query.")
            queries = playlist.expand_to_all(count, MAX_OBJECTS_PER_QUERY)
            multiqueries = (Multiquery(batch) for batch in batched(queries, MAX_QUERIES_IN_MULTIQUERY))
            fetch_tasks = (group.create_task(client.query("multiquery", m)) for m in multiqueries)
            responses = await asyncio.gather(*fetch_tasks)
            multiquery_responses = MultiqueryResponseListAdapter.validate_python(responses, extra='allow')
            results = chain.from_iterable(multiquery_responses)

            # We're not processing the returned games except to sort them,
            # so we don't need to convert them to IgdbGame objects here.
            games = chain.from_iterable(filter(None, (r.get('result') for r in results)))
            games_sorted = sorted(games, key=lambda g: g['name'])

            print(f"{playlist.title}: Fetched {len(games_sorted)} games.")

            # Create the output directory if it doesn't exist
            await aiofiles.os.makedirs(self.outdir, exist_ok=True)
            outpath = os.path.join(self.outdir, f"{playlist.title}.json")
            async with aiofiles.open(outpath, 'wb') as outfile:
                await outfile.write(to_json(games_sorted, indent=2))
                print(f"{playlist.title}: Saved {len(games_sorted)} games to {outpath}")

        async with QueryClient(self.client_id, self.client_secret) as client:
            async with asyncio.TaskGroup() as group:
                for p in playlists:
                    group.create_task(fetch_playlist(client, p, group), name=p.title)
            # The task group will wait for all fetch tasks to complete

class IgdbCommand(BaseSettings):
    fetch: CliSubCommand[FetchSubCommand]
    query: CliSubCommand[QuerySubCommand]
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
    "AgeRating",
    "AgeRatingCategory",
    "AgeRatingContentDescriptionType",
    "AgeRatingContentDescriptionV2",
    "AgeRatingOrganization",
    "AlternativeName",
    "ANALOG_KEYWORD_IDS",
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
    "IgdbId",
    "IgdbObject",
    "IGDB_OBJECT_TYPES",
    "InvolvedCompany",
    "Keyword",
    "Language",
    "LanguageSupport",
    "LanguageSupportType",
    "MAX_ACTIVE_QUERIES",
    "MAX_QUERY_PERIOD",
    "MAX_QUERY_RATE",
    "MultiplayerMode",
    "Multiquery",
    "Platform",
    "PlatformFamily",
    "PlatformType",
    "PlatformVersion",
    "PlayerPerspective",
    "Playlist",
    "PlaylistTitle",
    "Query",
    "QueryClient",
    "Region",
    "ReleaseDate",
    "ReleaseDateRegion",
    "ReleaseDateStatus",
    "RUMBLE_KEYWORD_IDS",
    "SortDirection",
    "Theme",
)

if __name__ == "__main__":
    CliApp.run(IgdbCommand)
