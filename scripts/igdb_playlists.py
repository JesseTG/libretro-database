import asyncio
import dataclasses
import datetime
from datetime import date
from functools import cache
import os.path
from pathlib import Path
import tomllib

from collections import ChainMap
from dataclasses import dataclass
from typing import Optional, Literal, NewType, TypedDict, cast
from collections.abc import Collection, Sequence, Iterable, Iterator, Mapping

import aiofiles
import typelib


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
    rating_content_descriptions: Sequence[AgeRatingContentDescriptionV2] | None = None
    rating_cover_url: str | None = None
    synopsis: str | None = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AlternativeName:
    id: IgdbId
    name: str
    comment: str | None = None

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
    name: str | None = None
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
    country: int | None = None # ISO 3166-1 code
    name: str
    slug: str
    status: CompanyStatus | None = None

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
    identifier: str | None = None
    name: str | None = None
    category: Literal['locale', 'continent'] | None = None

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
    abbreviation: str | None = None
    alternative_name: str | None = None
    generation: int | None = None
    name: str
    platform_family: PlatformFamily | None = None
    platform_type: PlatformType | None = None
    slug: str | None = None
    summary: str | None = None

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class MultiplayerMode:
    id: IgdbId
    campaigncoop: bool
    dropin: bool
    lancoop: bool
    offlinecoop: bool
    offlinecoopmax: int | None = None
    offlinemax: int | None = None
    onlinecoop: bool
    onlinecoopmax: int | None = None
    onlinemax: int | None = None
    platform: Platform | None = None
    splitscreen: bool
    splitscreenonline: bool | None = None

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
    date: int | None = None
    date_format: DateFormat
    human: str
    m: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] | None = None  # Month (1-12)
    platform: Platform
    release_region: ReleaseDateRegion
    status: ReleaseDateStatus | None = None
    y: int | None = None  # Year

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Theme:
    id: IgdbId
    name: str

@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Game:
    id: IgdbId
    age_ratings: Sequence[AgeRating] | None = None
    aggregated_rating: float | None = None
    aggregated_rating_count: int | None = None
    alternative_names: Sequence[AlternativeName] | None = None
    bundles: Sequence['Game'] | None = None # name, ID, and platform
    collections: Sequence['Game'] | None = None # name, ID, and platform
    dlcs: Sequence['Game'] | None = None # name, ID, and platform
    expanded_games: Sequence['Game'] | None = None # name, ID, and platform
    expansions: Sequence['Game'] | None = None # name, ID, and platform
    first_release_date: int | None = None # TODO: Parse with date.fromtimestamp()
    forks: Sequence['Game'] | None = None # name, ID, and platform
    franchise: Franchise | None = None
    franchises: Sequence[Franchise] | None = None
    game_engines: Sequence[GameEngine] | None = None
    game_localizations: Sequence[GameLocalization] | None = None
    game_modes: Sequence[GameMode] | None = None
    game_status: GameStatus | None = None
    game_type: GameType | None = None
    genres: Sequence[Genre] | None = None
    involved_companies: Sequence[InvolvedCompany] | None = None
    keywords: Sequence[Keyword] | None = None
    language_supports: Sequence[LanguageSupport] | None = None
    multiplayer_modes: Sequence[MultiplayerMode] | None = None
    name: str
    parent_game: 'Game | None' = None
    platforms: Sequence[Platform] | None = None
    player_perspectives: Sequence[PlayerPerspective] | None = None
    ports: Sequence['Game'] | None = None
    release_dates: Sequence[ReleaseDate] | None = None
    remakes: Sequence['Game'] | None = None
    remasters: Sequence['Game'] | None = None
    slug: str | None = None
    standalone_expansions: Sequence['Game'] | None = None
    storyline: str | None = None
    summary: str | None = None
    themes: Sequence[Theme] | None = None
    total_rating: float | None = None
    total_rating_count: int | None = None
    url: str | None = None # TODO: Parse with urllib
    version_parent: 'Game | None' = None
    version_title: str | None = None

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
    "version_title",
    "version_parent.name",
    "version_parent.platforms.name",
)

SortDirection = Literal['asc', 'desc']
DEFAULT_SORT: tuple[str, SortDirection] = ('name', 'asc')

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
            *, # Force keyword arguments for clarity
            fields: Iterable[str] | str | None = "*",
            exclude: Iterable[str] | str | None = None,
            where: Optional[str] = None,
            limit: int = 10, # IGDB's default
            offset: int = 0, # IGDB's default
            sort: tuple[str, SortDirection] | None = None,
            search: Optional[str] = None,
    ):
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
            hasheous: str | Iterable[str] | None = None,
            alts: str | Iterable[str] | None = None,
            *, # Force keyword arguments for clarity
            fields: Iterable[str] | str | None = DEFAULT_GAME_FIELD_TUPLE,
            exclude: Iterable[str] | str | None = None,
            where: str | None = None,
            limit: int = 500,
            offset: int = 0,
            sort: tuple[str, SortDirection] | None = DEFAULT_SORT,
            search: str | None = None,
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


__all__ = [
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
]