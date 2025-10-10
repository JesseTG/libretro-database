#!/usr/bin/env python3

import argparse
import asyncio
import dataclasses
import itertools
import json
import os.path
import re
import sys
import tomllib

from asyncio import TaskGroup
from collections import ChainMap
from collections.abc import Collection, Sequence, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from json import JSONDecodeError
from pathlib import Path
from typing import Never, Optional, Literal, NewType, Required, TypeAlias, TypedDict, cast, overload

import aiofiles
import aiofiles.os
import asynciolimiter
import backoff
import httpx
import typelib

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import HTTPStatusError, Response, Timeout

IgdbId = NewType('IgdbId', int)

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingOrganization:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingCategory:
    id: IgdbId
    rating: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingContentDescriptionType:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRatingContentDescriptionV2:
    id: IgdbId
    description: str
    description_type: AgeRatingContentDescriptionType


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AgeRating:
    id: IgdbId
    organization: AgeRatingOrganization
    rating_category: AgeRatingCategory
    rating_content_descriptions: Optional[Sequence[AgeRatingContentDescriptionV2]] = None
    rating_cover_url: Optional[str] = None
    synopsis: Optional[str] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AlternativeName:
    id: IgdbId
    name: str
    comment: Optional[str] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Franchise:
    id: IgdbId
    name: str
    slug: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameEngine:
    id: IgdbId
    name: str
    slug: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameLocalization:
    id: IgdbId
    name: Optional[str] = None
    region: 'Region'

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameMode:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameStatus:
    id: IgdbId
    status: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class GameType:
    id: IgdbId
    type: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Genre:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class CompanyStatus:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Company:
    id: IgdbId
    country: Optional[int] = None # ISO 3166-1 code
    name: str
    slug: str
    status: Optional[CompanyStatus] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class InvolvedCompany:
    id: IgdbId
    company: Company
    developer: bool
    porting: bool
    publisher: bool
    supporting: bool

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Region:
    id: IgdbId
    identifier: Optional[str] = None
    name: Optional[str] = None
    category: Optional[Literal['locale', 'continent']] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Keyword:
    id: IgdbId
    name: str
    slug: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Language:
    id: IgdbId
    locale: str
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class LanguageSupportType:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class LanguageSupport:
    id: IgdbId
    language: Language
    language_support_type: LanguageSupportType

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformFamily:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformType:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlatformVersion:
    id: IgdbId
    name: str
    slug: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Platform:
    id: IgdbId
    abbreviation: Optional[str] = None
    alternative_name: Optional[str] = None
    generation: Optional[int] = None
    name: str
    platform_family: Optional[PlatformFamily] = None
    platform_type: Optional[PlatformType] = None
    slug: Optional[str] = None
    summary: Optional[str] = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class MultiplayerMode:
    id: IgdbId
    campaigncoop: bool
    dropin: bool
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: Optional[int] = None
    offlinemax: Optional[int] = None
    onlinecoop: bool
    onlinecoopmax: Optional[int] = None
    onlinemax: Optional[int] = None
    platform: Optional[Platform] = None
    splitscreen: bool
    splitscreenonline: Optional[bool] = None

    @property
    def coop(self) -> bool:
        return self.campaigncoop or self.lancoop or self.offlinecoop or self.onlinecoop

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class PlayerPerspective:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class DateFormat:
    id: IgdbId
    format: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDateRegion:
    id: IgdbId
    region: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDateStatus:
    id: IgdbId
    description: str
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class ReleaseDate:
    id: IgdbId
    date: Optional[int] = None
    date_format: DateFormat
    human: str
    m: Optional[Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]] = None  # Month (1-12)
    platform: Platform
    release_region: ReleaseDateRegion
    status: Optional[ReleaseDateStatus] = None
    y: Optional[int] = None  # Year

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Theme:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Game:
    id: IgdbId
    age_ratings: Optional[Sequence[AgeRating]] = None
    aggregated_rating: Optional[float] = None
    aggregated_rating_count: Optional[int] = None
    alternative_names: Optional[Sequence[AlternativeName]] = None
    bundles: Optional[Sequence['Game']] = None # name, ID, and platform
    collections: Optional[Sequence['Game']] = None # name, ID, and platform
    dlcs: Optional[Sequence['Game']] = None # name, ID, and platform
    expanded_games: Optional[Sequence['Game']] = None # name, ID, and platform
    expansions: Optional[Sequence['Game']] = None # name, ID, and platform
    first_release_date: Optional[int] = None # TODO: Parse with date.fromtimestamp()
    forks: Optional[Sequence['Game']] = None # name, ID, and platform
    franchise: Optional[Franchise] = None
    franchises: Optional[Sequence[Franchise]] = None
    game_engines: Optional[Sequence[GameEngine]] = None
    game_localizations: Optional[Sequence[GameLocalization]] = None
    game_modes: Optional[Sequence[GameMode]] = None
    game_status: Optional[GameStatus] = None
    game_type: Optional[GameType] = None
    genres: Optional[Sequence[Genre]] = None
    involved_companies: Optional[Sequence[InvolvedCompany]] = None
    keywords: Optional[Sequence[Keyword]] = None
    language_supports: Optional[Sequence[LanguageSupport]] = None
    multiplayer_modes: Optional[Sequence[MultiplayerMode]] = None
    name: str
    parent_game: Optional['Game'] = None
    platforms: Optional[Sequence[Platform]] = None
    player_perspectives: Optional[Sequence[PlayerPerspective]] = None
    ports: Optional[Sequence['Game']] = None
    release_dates: Optional[Sequence[ReleaseDate]] = None
    remakes: Optional[Sequence['Game']] = None
    remasters: Optional[Sequence['Game']] = None
    slug: Optional[str] = None
    standalone_expansions: Optional[Sequence['Game']] = None
    storyline: Optional[str] = None
    summary: Optional[str] = None
    themes: Optional[Sequence[Theme]] = None
    total_rating: Optional[float] = None
    total_rating_count: Optional[int] = None
    url: Optional[str] = None # TODO: Parse with urllib
    version_parent: Optional['Game'] = None
    version_title: Optional[str] = None

DEFAULT_GAME_FIELD_TUPLE: tuple[str, ...] = (
    "age_ratings.organization.name",
    "age_ratings.rating_category.rating",
    "age_ratings.rating_content_descriptions.description",
    "age_ratings.rating_content_descriptions.description_type.name",
    "aggregated_rating",
    "aggregated_rating_count",
    "alternative_names.comment",
    "alternative_names.name",
    "bundles.name",
    "collections.name",
    "collections.type.name",
    "dlcs",
    "dlcs.name",
    "expanded_games",
    "expanded_games.platforms.name",
    "expanded_games.name",
    "expansions",
    "expansions.name",
    "expansions.platforms.name",
    "first_release_date",
    "forks.name",
    "forks.platforms.name",
    "franchise.name",
    "franchise.slug",
    "franchises.name",
    "franchises.slug",
    "game_engines.name",
    "game_engines.slug",
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
    "involved_companies.company.slug",
    "involved_companies.company.status.name",
    "involved_companies.developer",
    "involved_companies.porting",
    "involved_companies.publisher",
    "involved_companies.supporting",
    "keywords.name",
    "keywords.slug",
    "language_supports.language.locale",
    "language_supports.language.name",
    "language_supports.language_support_type.name",
    "multiplayer_modes.campaigncoop",
    "multiplayer_modes.dropin",
    "multiplayer_modes.lancoop",
    "multiplayer_modes.offlinecoop",
    "multiplayer_modes.offlinecoopmax",
    "multiplayer_modes.offlinemax",
    "multiplayer_modes.onlinecoop",
    "multiplayer_modes.onlinecoopmax",
    "multiplayer_modes.onlinemax",
    "multiplayer_modes.platform.name",
    "multiplayer_modes.splitscreen",
    "multiplayer_modes.splitscreenonline",
    "name",
    "parent_game.name",
    "platforms.abbreviation",
    "platforms.alternative_name",
    "platforms.generation",
    "platforms.name",
    "platforms.platform_family.name",
    "platforms.platform_type.name",
    "platforms.slug",
    "platforms.summary",
    "player_perspectives.name",
    "ports.name",
    "ports.platforms.name",
    "release_dates.date",
    "release_dates.date_format.format",
    "release_dates.human",
    "release_dates.m",
    "release_dates.platform.name",
    "release_dates.release_region.region",
    "release_dates.status.description",
    "release_dates.status.name",
    "release_dates.y",
    "remakes.name",
    "remakes.platforms.name",
    "remasters.name",
    "remasters.platforms.name",
    "slug",
    "standalone_expansions.name",
    "standalone_expansions.platforms.name",
    "storyline",
    "summary",
    "themes.name",
    "total_rating",
    "total_rating_count",
    "url",
    "version_title",
    "version_parent.name",
    "version_parent.platforms.name",
)

SortDirection = Literal['asc', 'desc']
DEFAULT_SORT: tuple[str, SortDirection] = ('name', 'asc')
QUERY_CLAUSE = r'(fields|f|exclude|x|where|w|limit|l|offset|o|sort|s|search)\s+([^;]+)\s*;'

JsonPrimitive = str | int | float | bool | None
JsonArray: TypeAlias = Sequence["JsonPrimitive | JsonObject | JsonArray"]
JsonObject: TypeAlias = Mapping[str, "JsonPrimitive | JsonArray | JsonObject"]

class GameResponse(TypedDict, total=False):
    name: Required[str]

class MultiqueryResponse(TypedDict):
    name: str
    result: Sequence[GameResponse]

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
          # Regular expression to match clauses

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
    title: str
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
    The name of zero or more directories within a Hasheous dump.
    Can have slashes for subdirectories (e.g. "Commodore Plus/4")
    '''

    def __init__(
            self,
            title: str,
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

QueryType: TypeAlias = str | Query | Multiquery

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

    async def __aenter__(self) -> 'QueryClient':
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
        print("Exception raised during query:", e, file=sys.stderr)
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
    async def query(self, endpoint: Literal["multiquery"], query: str | Multiquery) -> JsonArray: ...

    @overload
    async def query(self, endpoint: Literal["multiquery"], query: Query) -> Never: ...

    @overload
    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonArray | JsonObject: ...

    async def query(self, endpoint: str, query: str | Query | Multiquery) -> JsonArray | JsonObject:
        if endpoint == "multiquery" and isinstance(query, Query):
            raise TypeError("Expected a str or Multiquery for 'multiquery' endpoint; got Query")

        try:
            response = await self._query(endpoint, query)
            response_json = response.json()

            return response_json
        except HTTPStatusError as e:
            print(e.response.headers, file=sys.stderr)
            raise

    async def count(self, endpoint: str, query: str | Query) -> int:
        if not endpoint.endswith('/count'):
            endpoint += '/count'

        response = await self._query(endpoint, query)
        response_json = response.json()

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
        title: str
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
        return tuple(Playlist(**p) for p in playlist_objects)


dirname = os.path.dirname(__file__)
TOML_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'metadat', 'igdb', 'igdb.toml'))

PLAYLISTS = read_playlists(TOML_PATH)

PLAYLISTS_BY_TITLE = {p.title: p for p in PLAYLISTS}
PLAYLISTS_BY_TITLE_LOWER = {p.title.lower(): p for p in PLAYLISTS}
PLAYLISTS_BY_ANY: Mapping[str, Playlist] = ChainMap(
    PLAYLISTS_BY_TITLE,
    PLAYLISTS_BY_TITLE_LOWER,
)

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


GameTupleCodec: typelib.Codec[tuple[Game, ...]] = typelib.codec(tuple[Game, ...])

def get_client_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    return client_id, client_secret


async def load_games(playlists: Mapping[Path, Playlist]) -> Mapping[str, Collection[Game]]:
    """
    :param playlists: An iterable of tuples,
    where each tuple contains the path to a playlist file
    and the corresponding Playlist object.

    :return: A mapping of playlist titles to collections of the Games they represent.
    """
    async def _load_file(path: Path, playlist: Playlist) -> tuple[str, Collection[Game]]:
        async with aiofiles.open(path, mode='rb') as infile:
            json_bytes = await infile.read()
            games = GameTupleCodec.decode(json_bytes)
            return playlist.title, games
            # Including path in the return value simplifies the following list comprehension

    async with asyncio.TaskGroup() as group:
        tasks = tuple(group.create_task(_load_file(k, v), name=k.stem) for (k, v) in playlists.items())
        # Start loading each playlist file concurrently
        result = dict(await asyncio.gather(*tasks))

        return result

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
                json.dump(response, sys.stdout, indent=2)
            else:
                count_response = cast(CountResponse, await client.query(f"{args.endpoint}/count", body))
                count = count_response["count"]
                if verbose:
                    print(f"Query will return {count} total records", file=sys.stderr)

                query = Query(body)
                async with asyncio.TaskGroup() as group:
                    tasks: list[asyncio.Task[JsonArray]] = []
                    for q in itertools.batched(query.query_pages(count), MULTIQUERY_MAX):
                        if verbose:
                            print(f"Fetching records {q[0].offset} to {q[-1].offset + q[-1].limit - 1}", file=sys.stderr)

                        multiquery = Multiquery({f"{args.endpoint} ({p.offset}-{p.offset + p.limit - 1})": (args.endpoint, p) for p in q})
                        task = group.create_task(client.query("multiquery", multiquery))
                        tasks.append(task)

                    responses: Sequence[Sequence[MultiqueryResponse]] = await asyncio.gather(*tasks) # type: ignore[type-var]


                results = tuple(r['result'] for r in itertools.chain.from_iterable(responses))
                records = tuple(itertools.chain.from_iterable(results))
                json.dump(records, sys.stdout, indent=2)
        except JSONDecodeError as e:
            print(e.doc, file=sys.stderr)
            print(e, file=sys.stderr)
            raise e


async def handle_fetch(args: argparse.Namespace) -> None:
    """Handle the fetch subcommand."""

    playlist_args: Iterable[str] | None = args.playlists
    if not playlist_args:
        # If no playlists specified, use all known playlists
        playlist_args = (p.title for p in PLAYLISTS)

    # Get all playlists to scrape (filter out the Nones)
    playlists = tuple(filter(None, (get_playlist(p) for p in playlist_args)))
    if not playlists:
        raise ValueError("All listed playlists are unknown.")

    outdir: str = args.outdir

    async def fetch_playlist(client: QueryClient, playlist: Playlist, group: TaskGroup) -> Sequence[GameResponse]:
        print(f"{playlist.title}: Fetching game count in query...")
        count = await client.count("games", playlist.query)

        multiqueries: list[Multiquery] = []
        for batch in itertools.batched(playlist.query_pages(count), MULTIQUERY_MAX):
            multiqueries.append(Multiquery({f"{playlist.title} ({q.offset}-{q.offset + q.limit - 1})": ('games', q) for q in batch}))

        playlist_tasks = tuple(group.create_task(client.query("multiquery", m)) for m in multiqueries)
        print(f"{playlist.title}: Scheduled to fetch {count} games...")

        responses: Sequence[JsonArray]  = await asyncio.gather(*playlist_tasks)
        games: list[GameResponse] = []

        for r in responses:
            if not isinstance(r, Sequence):
                raise ValueError(f"Expected multiquery response for '{playlist.title}' to be a JSON array; got: {type(r)} ({r})")

            for g in cast(Sequence[MultiqueryResponse], r):
                games.extend(g['result'])
                # We're not processing the returned games except to sort them,
                # so we don't need to convert them to IgdbGame objects here.

        print(f"{playlist.title}: Fetched {len(games)} games.")
        games.sort(key=lambda g: g['name'])
        # Now that we have all the games, sort them by name

        # Create the output directory if it doesn't exist
        await aiofiles.os.makedirs(outdir, exist_ok=True)
        outpath = os.path.join(outdir, f"{playlist.title}.json")
        async with aiofiles.open(outpath, 'w', encoding='utf-8') as outfile:
            await outfile.write(json.dumps(games, indent=2, ensure_ascii=False))
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
        "--playlists",
        type=str,
        help="The title or system IDs of the playlists to scrape. If not provided, all known playlists will be scraped.",
        action="extend",
        nargs="*",
        default=PLAYLISTS_BY_TITLE.keys()  # Default to all known playlists
    )
    fetch_parser.add_argument(
        "outdir",
        type=str,
        help="The output directory for the scraped JSON files"
    )
    fetch_parser.set_defaults(func=handle_fetch)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

__all__ = (
    "AgeRatingOrganization",
    "AgeRatingContentDescriptionV2",
    "AgeRatingContentDescriptionType",
    "AgeRatingCategory",
    "AgeRating",
    "AlternativeName",
    "Franchise",
    "GameEngine",
    "GameLocalization",
    "GameMode",
    "GameStatus",
    "GameType",
    "Genre",
    "CompanyStatus",
    "Company",
    "InvolvedCompany",
    "Region",
    "Keyword",
    "Language",
    "LanguageSupportType",
    "LanguageSupport",
    "MultiplayerMode",
    "PlatformFamily",
    "PlatformType",
    "PlatformVersion",
    "Platform",
    "PlayerPerspective",
    "DateFormat",
    "ReleaseDateRegion",
    "ReleaseDateStatus",
    "ReleaseDate",
    "Theme",
    "Game",
    "SortDirection",
    "Query",
    "Playlist",
    "Multiquery",
    "get_playlist",
    "get_by_title",
    "PLAYLISTS",
    "MAX_ACTIVE_QUERIES",
    "MAX_QUERY_RATE",
    "MAX_QUERY_PERIOD",
    "MULTIQUERY_MAX",
    "PLAYLISTS_BY_TITLE",
    "DEFAULT_GAME_FIELD_TUPLE",
    "DEFAULT_SORT",
    "load_games",
    "QueryClient",
    "RUMBLE_KEYWORD_IDS",
    "ANALOG_KEYWORD_IDS",
)

if __name__ == "__main__":
    main()
